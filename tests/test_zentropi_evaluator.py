"""Tests for the Zentropi evaluator's Tier 0 / 1 / 2 error handling.

Covers:
- Tier 0 keyless init (api_key=None) — no Authorization header sent.
- 401 with auth_required envelope raises ReflexesUpgradeRequired (no retry).
- 429 with rate_limit_exceeded envelope raises ReflexesUpgradeRequired (no retry).
- 429 without envelope: retries, then (if sustained) surfaces a synthesized
  rate_limit_exceeded ReflexesUpgradeRequired; a transient 429 that clears does not.
- 403 without envelope (image/video capability): synthesized subscription_required
  ReflexesUpgradeRequired, no retry.
- 500/502/503 retry behavior unchanged.
- notice stderr print is deduplicated by (tier, error) within one process.
- Resolved-policy header logged at DEBUG when present on 200.

Tests use httpx.MockTransport to stub the network layer — no live calls.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from reflexes.core import ReflexesUpgradeRequired, Verdict
from reflexes.evaluators import zentropi as zmod


@pytest.fixture(autouse=True)
def reset_upgrade_notice_dedup_set():
    """Ensure each test starts with a clean notice-dedup set."""
    zmod._UPGRADE_NOTICE_PRINTED.clear()
    yield
    zmod._UPGRADE_NOTICE_PRINTED.clear()


def _patched_evaluator(monkeypatch, transport: httpx.MockTransport):
    """Patch httpx.AsyncClient to use the given MockTransport."""
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(zmod.httpx, "AsyncClient", factory)


def _envelope(tier: int, error: str, reason: str, action: str) -> dict:
    return {
        "error": error,
        "tier": tier,
        "reason": reason,
        "message": f"test {error} message",
        "upgrade_url": f"https://zentropi.ai/{action}",
        "upgrade_action": action,
    }


def test_missing_api_key_raises(monkeypatch):
    """The Zentropi API has no keyless tier — constructing without a key must
    fail fast with a clear message, not send an unauthenticated request."""
    monkeypatch.delenv("ZENTROPI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ZENTROPI_API_KEY required"):
        zmod.ZentropiEvaluator()  # no api_key, no env var


@pytest.mark.asyncio
async def test_tier_one_sends_authorization_header(monkeypatch):
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"label": 1, "confidence": 0.95})

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="test-key-123")
    await ev.evaluate(policy="p", content="c")

    assert seen_headers.get("authorization") == "Bearer test-key-123"


@pytest.mark.asyncio
async def test_401_auth_required_raises_without_retry(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            401,
            json=_envelope(0, "auth_required", "criteria_not_recognized", "signup"),
        )

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="bad-key")

    with pytest.raises(ReflexesUpgradeRequired) as exc_info:
        await ev.evaluate(policy="custom policy text", content="c")

    assert call_count["n"] == 1, "auth_required must not be retried"
    assert exc_info.value.tier == 0
    assert exc_info.value.error == "auth_required"
    assert exc_info.value.reason == "criteria_not_recognized"
    assert exc_info.value.upgrade_action == "signup"
    assert "zentropi.ai/signup" in exc_info.value.upgrade_url


@pytest.mark.asyncio
async def test_tier_zero_429_envelope_raises_without_retry(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json=_envelope(0, "rate_limit_exceeded", "per_ip_throttle", "signup"),
        )

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="test-key")

    with pytest.raises(ReflexesUpgradeRequired) as exc_info:
        await ev.evaluate(policy="p", content="c")

    assert call_count["n"] == 1, "tier-aware 429 must not be retried"
    assert exc_info.value.tier == 0
    assert exc_info.value.reason == "per_ip_throttle"
    assert exc_info.value.retry_after == 60


@pytest.mark.asyncio
async def test_tier_one_429_envelope_raises_subscribe_action(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json=_envelope(1, "rate_limit_exceeded", "per_key_throttle", "subscribe"),
        )

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="real-key")

    with pytest.raises(ReflexesUpgradeRequired) as exc_info:
        await ev.evaluate(policy="p", content="c")

    assert exc_info.value.tier == 1
    assert exc_info.value.upgrade_action == "subscribe"


@pytest.mark.asyncio
async def test_persistent_429_without_envelope_synthesizes_ratelimit_upgrade(monkeypatch):
    """A plain 429 (no envelope) that survives every retry is sustained
    throttling — retry as before, then surface a synthesized rate-limit
    ReflexesUpgradeRequired (fail-open + notice), not a silent generic error."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            429, json={"error": "Rate limit exceeded - too many requests",
                       "detail": "Please slow down your request rate"},
        )

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr(zmod, "RETRY_DELAYS", [0.0, 0.0])
    ev = zmod.ZentropiEvaluator(api_key="k")

    with pytest.raises(ReflexesUpgradeRequired) as exc_info:
        await ev.evaluate(policy="p", content="c")
    assert call_count["n"] == zmod.MAX_RETRIES + 1, "must retry before surfacing"
    assert exc_info.value.error == "rate_limit_exceeded"
    assert exc_info.value.upgrade_url == "https://zentropi.ai/subscription"
    # Rate-limit message offers the self-host escape hatch (open CoPE model).
    assert "huggingface.co/zentropi-ai/cope-b-a4b" in exc_info.value.message


@pytest.mark.asyncio
async def test_transient_429_recovers_no_upgrade(monkeypatch):
    """A 429 that clears on retry returns a Verdict — no upgrade signal."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 2:
            return httpx.Response(429, json={"error": "Rate limit exceeded - too many requests"})
        return httpx.Response(200, json={"label": 1, "confidence": 0.9})

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr(zmod, "RETRY_DELAYS", [0.0, 0.0])
    ev = zmod.ZentropiEvaluator(api_key="k")

    verdict = await ev.evaluate(policy="p", content="c")
    assert verdict.label == 1
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_403_image_subscription_synthesizes_upgrade(monkeypatch):
    """The live API returns a plain 403 for image/video inference on a
    non-subscriber key. Surface it as a synthesized subscription
    ReflexesUpgradeRequired immediately (no retry), carrying the server's
    detail + the upgrade URL."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            403, json={"detail": "Image inference requires a subscription. "
                                 "Please upgrade to use this feature."},
        )

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="k")

    with pytest.raises(ReflexesUpgradeRequired) as exc_info:
        await ev.evaluate(policy="p", content="c")
    assert call_count["n"] == 1, "403 capability error must not be retried"
    assert exc_info.value.error == "subscription_required"
    assert "Image inference requires a subscription" in exc_info.value.message
    assert exc_info.value.upgrade_url == "https://zentropi.ai/subscription"
    assert exc_info.value.upgrade_action == "subscribe"


@pytest.mark.asyncio
async def test_500_retries_unchanged(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(500, text="server boom")
        return httpx.Response(200, json={"label": 0, "confidence": 0.5})

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr(zmod, "RETRY_DELAYS", [0.0, 0.0])
    ev = zmod.ZentropiEvaluator(api_key="k")

    verdict = await ev.evaluate(policy="p", content="c")
    assert verdict.label == 0
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_upgrade_notice_stderr_print_deduplicated(monkeypatch, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json=_envelope(0, "auth_required", "criteria_not_recognized", "signup"),
        )

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="test-key")

    for _ in range(3):
        with pytest.raises(ReflexesUpgradeRequired):
            await ev.evaluate(policy="p", content="c")

    captured = capsys.readouterr()
    # First raise prints the notice; subsequent same-(tier,error) raises don't.
    assert captured.err.count("zentropi.ai/signup") == 1
    assert captured.err.count("[Reflexes]") == 2  # message line + url line, single block


@pytest.mark.asyncio
async def test_upgrade_notice_stderr_print_separate_keys_print_independently(
    monkeypatch, capsys,
):
    """A different (tier, error) tuple should print fresh — dedup is per-key, not global."""
    responses = [
        httpx.Response(
            401,
            json=_envelope(0, "auth_required", "criteria_not_recognized", "signup"),
        ),
        httpx.Response(
            429,
            json=_envelope(1, "rate_limit_exceeded", "per_key_throttle", "subscribe"),
        ),
    ]
    idx = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        r = responses[idx["i"]]
        idx["i"] += 1
        return r

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="k")

    for _ in range(2):
        with pytest.raises(ReflexesUpgradeRequired):
            await ev.evaluate(policy="p", content="c")

    captured = capsys.readouterr()
    assert "zentropi.ai/signup" in captured.err
    assert "zentropi.ai/subscribe" in captured.err


@pytest.mark.asyncio
async def test_resolved_policy_header_logged_at_debug(monkeypatch, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"label": 1, "confidence": 0.92},
            headers={"X-Reflexes-Policy-Resolved": "safe-delete@1.0.2"},
        )

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="test-key")

    with caplog.at_level(logging.DEBUG, logger="reflexes.evaluators.zentropi"):
        await ev.evaluate(policy="p", content="c")

    assert any("safe-delete@1.0.2" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_envelope_with_missing_optional_fields_does_not_crash(monkeypatch):
    """The server might omit optional envelope fields. Parser must not crash;
    upgrade exception still raises with empty defaults so the developer at
    least sees the (tier, error) signal."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"tier": 0, "error": "auth_required"})

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    ev = zmod.ZentropiEvaluator(api_key="test-key")

    with pytest.raises(ReflexesUpgradeRequired) as exc_info:
        await ev.evaluate(policy="p", content="c")

    assert exc_info.value.tier == 0
    assert exc_info.value.error == "auth_required"
    # Optional fields default to empty strings without crashing.
    assert exc_info.value.upgrade_url == ""
    assert exc_info.value.upgrade_action == ""


@pytest.mark.asyncio
async def test_401_without_envelope_falls_through_to_generic_error(monkeypatch):
    """A bare 401 (e.g., generic upstream auth failure) without the structured
    envelope must not be misclassified as upgrade-required. Falls through to
    the existing RuntimeError path."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    _patched_evaluator(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr(zmod, "RETRY_DELAYS", [0.0, 0.0])
    ev = zmod.ZentropiEvaluator(api_key="k")

    with pytest.raises(RuntimeError):
        await ev.evaluate(policy="p", content="c")
