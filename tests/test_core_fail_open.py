"""Unit tests for the fail-open evaluator-config contract (SPEC §4.3).

Guards a regression where the original
behavior was that ``configure()`` propagated ``ValueError("API key
required")`` through the hook subprocess, crashing every Claude Code
tool call when ZENTROPI_API_KEY was unset. The intermediate fix made
``configure()`` catch and log, leaving ``_evaluator = None`` so adapters
fail open via ``safe_acheck`` → ``EvaluatorError``.

Subsequent architectural shift (Tier 0 keyless flow): missing api_key
is no longer a configuration failure — it's the
entry point to the Tier 0 path. ``ZentropiEvaluator`` instantiates
without a key and the server gates Tier 0 limits via tier-aware 401/429
envelopes (raised as ``ReflexesUpgradeRequired``, swallowed by
``safe_acheck`` into ``EvaluatorError`` for the same fail-open
semantics).

These tests verify both the original no-host-crash guarantee AND the
Tier 0 behavior: no-key install instantiates a working evaluator;
genuine instantiation failures (e.g., explicit unknown evaluator type)
still surface loudly; transient evaluator failures still fail open via
``safe_acheck``.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def fresh_core(monkeypatch):
    """Reset the module-level _evaluator and ensure no Zentropi key is in env.

    Each test gets a clean slate so configure-failure side effects don't
    leak. Reloading the module is overkill; setting _evaluator = None
    directly is enough.
    """
    monkeypatch.delenv("ZENTROPI_API_KEY", raising=False)
    # The zero-config default is CoPE via Parasail; clear its key (and the
    # systemone OpenRouter fallback key) so the lazy/default paths fail open.
    monkeypatch.delenv("PARASAIL_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import reflexes.core as core
    core._evaluator = None
    core._default_evaluator_active = False
    return core


def test_configure_with_unknown_evaluator_raises(fresh_core):
    """Unknown type is a config error, not an auth issue — surface loud."""
    with pytest.raises(ValueError, match="Unknown evaluator"):
        fresh_core.configure(evaluator="not-a-real-evaluator")


def test_configure_zentropi_no_key_fails_open(fresh_core):
    """Missing API key MUST NOT crash the host. The Zentropi API has no keyless
    tier, so with no key configure() fails OPEN: _evaluator is None and the
    stashed error explains the missing key (acheck→safe_acheck then returns an
    EvaluatorError, host continues)."""
    fresh_core.configure(evaluator="zentropi")  # no api_key, no env
    assert fresh_core._evaluator is None
    assert "ZENTROPI_API_KEY required" in (fresh_core._evaluator_error or "")


def test_get_evaluator_lazy_default_fails_open_without_key(fresh_core):
    """Lazy default (CoPE via Parasail) instantiation with no key returns None
    (fail open), not a dead keyless client."""
    assert fresh_core._get_evaluator() is None
    assert fresh_core._evaluator is None
    # The default path still ran (and marked itself), it just couldn't authenticate.
    assert fresh_core._default_evaluator_active is True


@pytest.mark.asyncio
async def test_acheck_raises_clear_error_when_no_evaluator(fresh_core, monkeypatch):
    """Direct ``acheck`` callers get a RuntimeError pointing at safe_acheck.

    Internal callers MUST use safe_acheck; this assertion guards against
    silent verdict fabrication when the evaluator isn't available. We
    simulate the no-evaluator condition by forcing the lazy-instantiation
    path to fail (the realistic case is a genuine import / config error,
    not a missing key — which is now Tier 0).
    """
    def _fail_init(*args, **kwargs):
        raise RuntimeError("simulated init failure")

    import reflexes.evaluators.systemone as smod
    monkeypatch.setattr(smod, "SystemOneEvaluator", _fail_init)

    with pytest.raises(RuntimeError, match="No evaluator configured"):
        await fresh_core.acheck(policy="dummy", content="dummy")


@pytest.mark.asyncio
async def test_safe_acheck_returns_evaluator_error_when_no_evaluator(fresh_core):
    """Adapters use safe_acheck; it MUST return EvaluatorError, not raise."""
    result = await fresh_core.safe_acheck(
        policy="dummy",
        content="dummy",
        policy_name="test-policy",
    )
    assert isinstance(result, fresh_core.EvaluatorError)
    assert result.policy_name == "test-policy"


def test_configure_zentropi_with_explicit_bad_endpoint_does_not_raise(fresh_core, monkeypatch):
    """Even with a key but unreachable endpoint, configure must fail open at
    instantiation level rather than crashing the host."""
    monkeypatch.setenv("ZENTROPI_API_KEY", "test-key")
    # Explicit bad endpoint shouldn't raise at __init__ time (httpx defers
    # the connection). This test guards that future evaluator __init__
    # changes don't introduce eager failures that bypass our safety net.
    fresh_core.configure(
        evaluator="zentropi",
        endpoint="https://invalid-endpoint-that-does-not-exist.zentropi.invalid",
    )
    # Evaluator instantiates fine; failures happen later in evaluate().
    assert fresh_core._evaluator is not None


# --- ReflexesUpgradeRequired dispatch (SPEC §4.3) --------------------------
#
# Two-tier contract:
#   - auth_required → fail-CLOSED (let exception propagate). User
#     authored a policy and is trusting the framework to enforce it;
#     silent fail-open on a config-shaped error breaks the security
#     mental model.
#   - rate_limit_exceeded → fail-open + notice stderr + counter split.
#     Transient-shape limit hit; preserve fail-open per SPEC §4.3.
#   - Generic exceptions (network, 5xx, etc.) → fail-open with `error`
#     counter label.
#
# All three paths increment the `upgrade_required` / `error` counter so
# dashboards split product-flow signal (people hitting Tier 0 limits,
# drives signup) from infra failures.


def _make_upgrade_evaluator(error: str, reason: str = "_test"):
    """Build a stub evaluator that always raises ReflexesUpgradeRequired
    with the given (error, reason). Lets each test pick the dispatch path."""
    from reflexes.core import ReflexesUpgradeRequired

    class _UpgradeRequiredEvaluator:
        api_key = None

        async def evaluate(self, *, policy, content, reason=False, policy_slug=None):
            raise ReflexesUpgradeRequired(
                error=error,
                tier=0,
                reason=reason,
                message=f"test {error}",
                upgrade_url="https://zentropi.ai/test",
                upgrade_action="signup",
                retry_after=None,
            )

    return _UpgradeRequiredEvaluator()


class _GenericFailureEvaluator:
    api_key = None

    async def evaluate(self, *, policy, content, reason=False, policy_slug=None):
        raise RuntimeError("transient network failure")


@pytest.mark.asyncio
async def test_safe_acheck_auth_required_propagates(fresh_core, monkeypatch):
    """auth_required is fail-CLOSED — the exception propagates from
    safe_acheck so framework integrations can surface the upgrade notice
    rather than silently fail-open on a config-shaped error.

    Counter still increments — dashboards see the upgrade signal regardless
    of whether the exception is swallowed or propagated.
    """
    from reflexes.core import ReflexesUpgradeRequired
    fresh_core._evaluator = _make_upgrade_evaluator("auth_required")

    import reflexes.observability as obs
    seen: list[str] = []
    monkeypatch.setattr(obs, "increment_verdict_counter", lambda action: seen.append(action))

    with pytest.raises(ReflexesUpgradeRequired):
        await fresh_core.safe_acheck(
            policy="dummy", content="dummy", policy_name="test-policy",
        )
    assert seen == ["upgrade_required"], seen


@pytest.mark.asyncio
async def test_safe_acheck_rate_limit_exceeded_swallows_to_evaluator_error(
    fresh_core, monkeypatch,
):
    """rate_limit_exceeded is fail-OPEN — wrapped as EvaluatorError so
    adapters continue uninterrupted. notice stderr print already fired at
    the evaluator layer (covered in test_zentropi_evaluator.py)."""
    fresh_core._evaluator = _make_upgrade_evaluator("rate_limit_exceeded")

    import reflexes.observability as obs
    seen: list[str] = []
    monkeypatch.setattr(obs, "increment_verdict_counter", lambda action: seen.append(action))

    result = await fresh_core.safe_acheck(
        policy="dummy", content="dummy", policy_name="test-policy",
    )
    assert isinstance(result, fresh_core.EvaluatorError)
    assert seen == ["upgrade_required"], seen
    # The upgrade exception is attached so integrations can surface the notice
    # to the agent (not just treat it as a generic outage).
    assert result.upgrade is not None
    assert result.upgrade.error == "rate_limit_exceeded"
    assert result.upgrade.upgrade_url == "https://zentropi.ai/test"


@pytest.mark.asyncio
async def test_safe_acheck_unknown_upgrade_error_class_defaults_to_fail_open(
    fresh_core, monkeypatch,
):
    """Future error classes default to fail-open until categorized — the
    safe-fallback. If an `auth_required`-shaped error sneaks in under a
    new name, the counter still increments so the new class is visible
    to dashboards before its dispatch is decided."""
    fresh_core._evaluator = _make_upgrade_evaluator("not_yet_categorized")

    import reflexes.observability as obs
    seen: list[str] = []
    monkeypatch.setattr(obs, "increment_verdict_counter", lambda action: seen.append(action))

    result = await fresh_core.safe_acheck(
        policy="dummy", content="dummy", policy_name="test-policy",
    )
    assert isinstance(result, fresh_core.EvaluatorError)
    assert seen == ["upgrade_required"], seen


@pytest.mark.asyncio
async def test_safe_acheck_generic_failure_increments_error_label(fresh_core, monkeypatch):
    """Non-upgrade exceptions still flow to the `error` counter label."""
    fresh_core._evaluator = _GenericFailureEvaluator()

    import reflexes.observability as obs
    seen: list[str] = []
    monkeypatch.setattr(obs, "increment_verdict_counter", lambda action: seen.append(action))

    result = await fresh_core.safe_acheck(
        policy="dummy", content="dummy", policy_name="test-policy",
    )
    assert isinstance(result, fresh_core.EvaluatorError)
    assert seen == ["error"], seen


def test_reflexes_upgrade_required_envelope_fields():
    """ReflexesUpgradeRequired exposes the full server wire envelope so
    downstream tooling can pattern-match without re-shaping."""
    from reflexes.core import ReflexesUpgradeRequired
    e = ReflexesUpgradeRequired(
        error="rate_limit_exceeded",
        tier=0,
        reason="ip_throttle",
        message="Tier 0 IP throttle hit.",
        upgrade_url="https://zentropi.ai/upgrade",
        upgrade_action="upgrade_tier",
        retry_after=60,
    )
    assert e.error == "rate_limit_exceeded"
    assert e.tier == 0
    assert e.reason == "ip_throttle"
    assert e.message == "Tier 0 IP throttle hit."
    assert e.upgrade_url == "https://zentropi.ai/upgrade"
    assert e.upgrade_action == "upgrade_tier"
    assert e.retry_after == 60
