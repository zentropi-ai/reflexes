"""Tests for the System One evaluator (cope-b-s1 / Jev / self-host).

Covers request shaping to the System One spec, response parsing and
thresholding of P(true), multi-label batching in one call, the 26-question
chunk cap, keyless-local vs remote-key auth, and transient retry / hard-fail.

Uses httpx.MockTransport to stub the network — no live calls. (The default
OpenRouter /v1/systemone endpoint is not live yet; these pin the client
contract so a one-line live check suffices once it is.)
"""
from __future__ import annotations

import json

import httpx
import pytest

from reflexes.core import Verdict
from reflexes.evaluators import systemone as s1


def _patch(monkeypatch, transport: httpx.MockTransport):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(s1.httpx, "AsyncClient", factory)


def _answers(*probs: float) -> dict:
    return {
        "model": "cope-b-s1",
        "answers": {f"q{i}": {"type": "noul", "noul": p} for i, p in enumerate(probs)},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }


def _evaluator(monkeypatch, handler, **kw):
    _patch(monkeypatch, httpx.MockTransport(handler))
    kw.setdefault("api_key", "test-key")
    return s1.SystemOneEvaluator(**kw)


@pytest.mark.asyncio
async def test_single_request_shape_and_parse(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=_answers(0.98))

    ev = _evaluator(monkeypatch, handler)
    v = await ev.evaluate(policy="No launch-date commitments.", content="Ships March 15.")

    assert isinstance(v, Verdict)
    assert v.label == 1 and v.mode == "cope"
    assert v.confidence == pytest.approx(0.98)
    body = seen["body"]
    assert body["model"] == "cope-b-s1"
    assert body["state"] == {"content": "Ships March 15."}
    q = body["questions"]["q0"]
    assert q["type"] == "noul"
    assert q["instructions"].startswith("Does the content meet the criteria")
    assert "No launch-date commitments." in q["instructions"]
    assert q["criteria"]["true"] and q["criteria"]["false"]
    # questions ordered before state in the body (prefix-caching guidance)
    assert list(body.keys()).index("questions") < list(body.keys()).index("state")
    assert seen["auth"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_threshold_maps_probability_to_label(monkeypatch):
    handler = lambda req: httpx.Response(200, json=_answers(0.30))
    ev = _evaluator(monkeypatch, handler, threshold=0.5)
    v = await ev.evaluate(policy="p", content="c")
    # below threshold → label 0; confidence is the mass behind the 0 call
    assert v.label == 0
    assert v.confidence == pytest.approx(0.70)

    ev2 = _evaluator(monkeypatch, handler, threshold=0.25)
    v2 = await ev2.evaluate(policy="p", content="c")
    assert v2.label == 1  # same 0.30 now clears a lower bar
    assert v2.confidence == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_batch_multi_label_single_call(monkeypatch):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(req.content)
        assert len(body["questions"]) == 3  # all three in ONE call
        return httpx.Response(200, json=_answers(0.9, 0.1, 0.6))

    ev = _evaluator(monkeypatch, handler)
    verdicts = await ev.evaluate_batch(
        policies=[("hate", "h"), ("drugs", "d"), ("self-harm", "s")],
        content="some content",
    )
    assert calls["n"] == 1
    assert [v.label for v in verdicts] == [1, 0, 1]  # order preserved


@pytest.mark.asyncio
async def test_chunks_above_26(monkeypatch):
    calls = {"sizes": []}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        n = len(body["questions"])
        calls["sizes"].append(n)
        return httpx.Response(200, json=_answers(*([0.9] * n)))

    ev = _evaluator(monkeypatch, handler)
    policies = [(f"p{i}", f"text{i}") for i in range(30)]
    verdicts = await ev.evaluate_batch(policies=policies, content="c")
    assert len(verdicts) == 30
    assert calls["sizes"] == [26, 4]  # chunked at the 26-question cap


@pytest.mark.asyncio
async def test_retry_then_success(monkeypatch):
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json=_answers(0.8))

    ev = _evaluator(monkeypatch, handler)
    v = await ev.evaluate(policy="p", content="c")
    assert v.label == 1 and state["n"] == 2


@pytest.mark.asyncio
async def test_hard_400_raises(monkeypatch):
    handler = lambda req: httpx.Response(400, text="bad request")
    ev = _evaluator(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="400"):
        await ev.evaluate(policy="p", content="c")


def test_remote_endpoint_requires_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key required"):
        s1.SystemOneEvaluator(api_key=None)  # default endpoint is remote


def test_local_endpoint_keyless_ok(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ev = s1.SystemOneEvaluator(api_key=None, endpoint="http://localhost:8000/v1/systemone")
    assert ev.api_key is None and ev.model == "cope-b-s1"
