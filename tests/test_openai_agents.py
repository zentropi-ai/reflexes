"""Regression guards for the OpenAI Agents SDK integration (SDK-free).

The integration's helpers are importable without the `agents` package (it's
imported lazily inside the guardrail factories), so these run in the normal
suite without the optional dependency.
"""
from __future__ import annotations


def test_build_flag_info_does_not_read_nonexistent_policy_version():
    """Regression: _build_flag_info used to read binding.policy.version, which
    Policy has never had — it crashed on EVERY deny/flag (caught by the Beta
    live E2E, 2026-07-14)."""
    from reflexes.integrations.openai_agents import _build_flag_info
    from reflexes.core import Verdict

    class _Policy:
        name = "no-timeline-commitments"
        criteria = "Label = 1 when ..."

    class _Binding:
        policy = _Policy()
        trigger = "before_response"
        action = "deny"

    info = _build_flag_info(_Binding(), Verdict(label=1, confidence=0.9))
    assert info["reflex"] == "no-timeline-commitments"
    assert info["policy_criteria"] == "Label = 1 when ..."
    assert info["action"] == "deny"
    assert "policy_version" not in info  # dead field removed


def test_handle_verdict_deny_blocks_and_returns_info():
    from reflexes.integrations.openai_agents import _handle_verdict
    from reflexes.core import Verdict

    class _Policy:
        name = "p"
        criteria = "crit"

    class _Binding:
        policy = _Policy()
        trigger = "before_response"
        action = "deny"

    should_block, info = _handle_verdict(_Binding(), Verdict(label=1, confidence=0.9))
    assert should_block is True
    assert info["policy_criteria"] == "crit"
