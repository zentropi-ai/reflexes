"""Tests for the OpenAI-compatible evaluator's request compatibility.

Newer OpenAI reasoning models (e.g. gpt-5-nano) reject `max_tokens` and a
non-default `temperature`; they require `max_completion_tokens`. Most
OpenAI-compatible endpoints (Groq, Ollama, vLLM, older OpenAI) take the
`max_tokens` + `temperature` form. The evaluator tries the widely-supported
form first and falls back on the specific 400 that names the replacement.
"""
import asyncio
import json

import pytest

import reflexes.evaluators.openai_compatible as oc


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class _FakeClient:
    calls: list = []
    responses: list = []
    header_calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls.append(json)
        _FakeClient.header_calls.append(headers)
        return _FakeClient.responses[len(_FakeClient.calls) - 1]


_ERR_400 = {
    "error": {
        "message": "Unsupported parameter: 'max_tokens' is not supported "
        "with this model. Use 'max_completion_tokens' instead.",
        "code": "unsupported_parameter",
    }
}
_OK = {"choices": [{"message": {"content": '{"label": 1, "confidence": 0.9}'}}]}


def test_falls_back_to_max_completion_tokens_and_drops_temperature(monkeypatch):
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.responses = [_FakeResp(400, _ERR_400), _FakeResp(200, _OK)]

    ev = oc.OpenAICompatibleEvaluator(api_key="x", model="gpt-5-nano")
    verdict = asyncio.run(ev.evaluate(policy="p", content="c"))

    assert verdict.label == 1
    assert len(_FakeClient.calls) == 2, "should retry once on the 400"
    first, retry = _FakeClient.calls
    # First attempt: the widely-supported form.
    assert first["max_tokens"] == 512
    assert first["temperature"] == 0
    # Retry: reasoning-model form — max_completion_tokens, no temperature,
    # no max_tokens.
    assert retry["max_completion_tokens"] == 512
    assert "temperature" not in retry
    assert "max_tokens" not in retry


def test_standard_endpoint_uses_max_tokens_without_retry(monkeypatch):
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.responses = [_FakeResp(200, _OK)]

    ev = oc.OpenAICompatibleEvaluator(api_key="x", model="gpt-4o-mini")
    verdict = asyncio.run(ev.evaluate(policy="p", content="c"))

    assert verdict.label == 1
    assert len(_FakeClient.calls) == 1, "no retry when the first call succeeds"
    assert _FakeClient.calls[0]["max_tokens"] == 512
    assert _FakeClient.calls[0]["temperature"] == 0


# --- keyless local endpoints vs. key-required remote endpoints ----------------


@pytest.mark.parametrize("endpoint,is_local", [
    ("http://localhost:11434/v1/chat/completions", True),
    ("http://127.0.0.1:8000/v1/chat/completions", True),
    ("http://[::1]:8000/v1/chat/completions", True),
    ("http://192.168.1.50:8000/v1/chat/completions", True),
    ("http://10.0.0.5:8000/v1/chat/completions", True),
    ("http://my-box.local:8000/v1/chat/completions", True),
    ("https://api.openai.com/v1/chat/completions", False),
    ("https://api.groq.com/openai/v1/chat/completions", False),
])
def test_is_local_endpoint(endpoint, is_local):
    assert oc._is_local_endpoint(endpoint) is is_local


def test_local_endpoint_needs_no_api_key(monkeypatch):
    """A self-hosted local server (Ollama, vLLM, local CoPE) needs no key —
    construction must succeed and no Authorization header is sent."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.header_calls = []
    _FakeClient.responses = [_FakeResp(200, _OK)]

    ev = oc.OpenAICompatibleEvaluator(
        endpoint="http://localhost:11434/v1/chat/completions", model="llama3.1"
    )
    assert ev.api_key is None
    asyncio.run(ev.evaluate(policy="p", content="c"))
    assert "Authorization" not in _FakeClient.header_calls[0], (
        "keyless local requests must not send an Authorization header"
    )


def test_remote_endpoint_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key required"):
        oc.OpenAICompatibleEvaluator(
            endpoint="https://api.groq.com/openai/v1/chat/completions"
        )


def test_default_endpoint_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key required"):
        oc.OpenAICompatibleEvaluator()


# --- parse-error retry (transient non-JSON body) ------------------------------

def _resp_content(text):
    return _FakeResp(200, {"choices": [{"message": {"content": text}}]})


def test_parse_error_retries_then_succeeds(monkeypatch):
    """A transient non-JSON body is retried once; the good second response wins."""
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.header_calls = []
    _FakeClient.responses = [
        _resp_content("sorry, I can't do that"),          # unparseable
        _resp_content('{"label": 1, "confidence": 0.9}'),  # good on retry
    ]
    ev = oc.OpenAICompatibleEvaluator(api_key="x")
    verdict = asyncio.run(ev.evaluate(policy="p", content="c"))
    assert verdict.label == 1
    assert len(_FakeClient.calls) == 2, "should have retried once"


def test_parse_error_persists_raises(monkeypatch):
    """If every attempt is unparseable, raise (visible fail-open) — never label=0."""
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.header_calls = []
    _FakeClient.responses = [_resp_content("nope"), _resp_content("still"), _resp_content("nope")]
    ev = oc.OpenAICompatibleEvaluator(api_key="x")
    with pytest.raises(RuntimeError, match="Could not parse evaluator response"):
        asyncio.run(ev.evaluate(policy="p", content="c"))
    assert len(_FakeClient.calls) == oc.MAX_ATTEMPTS


# --- transient HTTP retry (429/5xx overload) ----------------------------------

def test_transient_http_retries_then_succeeds(monkeypatch):
    """A retryable status (e.g. 503) is retried; the good response wins."""
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.header_calls = []
    _FakeClient.responses = [
        _FakeResp(503, {"error": "overloaded"}),          # transient
        _resp_content('{"label": 1, "confidence": 0.9}'),  # good on retry
    ]
    ev = oc.OpenAICompatibleEvaluator(api_key="x")
    verdict = asyncio.run(ev.evaluate(policy="p", content="c"))
    assert verdict.label == 1
    assert len(_FakeClient.calls) == 2, "should have retried the 503"


def test_transient_http_persists_raises(monkeypatch):
    """If a retryable status persists across all attempts, raise (fail-open visible)."""
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.header_calls = []
    _FakeClient.responses = [_FakeResp(529, {}) for _ in range(oc.MAX_ATTEMPTS)]
    ev = oc.OpenAICompatibleEvaluator(api_key="x")
    with pytest.raises(RuntimeError, match="evaluation failed"):
        asyncio.run(ev.evaluate(policy="p", content="c"))
    assert len(_FakeClient.calls) == oc.MAX_ATTEMPTS


def test_non_retryable_http_raises_immediately(monkeypatch):
    """A hard error (401/403/400) is NOT retried — surface it at once."""
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.header_calls = []
    _FakeClient.responses = [_FakeResp(401, {"error": "bad key"}), _resp_content('{"label":1,"confidence":1}')]
    ev = oc.OpenAICompatibleEvaluator(api_key="x")
    with pytest.raises(RuntimeError, match="evaluation failed"):
        asyncio.run(ev.evaluate(policy="p", content="c"))
    assert len(_FakeClient.calls) == 1, "no retry on a non-retryable status"
