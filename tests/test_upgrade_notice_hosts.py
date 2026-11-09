"""Cross-host surfacing of the fail-open upgrade notice (Phase 1 + Phase 2).

When a policy can't be evaluated for a tier/capability reason (rate limit /
subscriber-only), the check fails open and every host that has an agent-context
channel should annotate the agent once, telling it enforcement is degraded.

The shared text + dedup live in ``reflexes.core`` (Phase 1). Each in-process
framework wires its own context channel (Phase 2): Claude Agent SDK →
additionalContext, LangGraph → reflex_flags state, Google ADK → context.state.
The framework libs aren't installed here, so we drive the module-level seams
directly with a stubbed ``safe_acheck``.
"""
from __future__ import annotations

import pytest

from reflexes.core import (
    ReflexesUpgradeRequired, EvaluatorError,
    format_upgrade_notice, should_surface_upgrade, upgrade_from_results,
)
import reflexes.core as core


def _make_upgrade() -> ReflexesUpgradeRequired:
    return ReflexesUpgradeRequired(
        error="rate_limit_exceeded", tier=0, reason="synthesized",
        message="Rate limit reached. Self-host https://huggingface.co/zentropi-ai/cope-b-a4b",
        upgrade_url="https://zentropi.ai/subscription", upgrade_action="subscribe",
    )


class _Policy:
    def __init__(self, name):
        self.name = name
        self.criteria = "# Criteria\n..."


class _Binding:
    def __init__(self, name="Sycophancy", action="deny", trigger="before_tool_call"):
        self.policy = _Policy(name)
        self.action = action
        self.trigger = trigger


@pytest.fixture(autouse=True)
def _reset_surfaced():
    """Clear the in-process dedup set so process-keyed hosts (Claude Agent SDK)
    surface fresh in each test."""
    core._UPGRADE_SURFACED_SESSIONS.clear()
    yield
    core._UPGRADE_SURFACED_SESSIONS.clear()


# --- Phase 1: shared core helpers ------------------------------------------

def test_format_upgrade_notice_carries_message_fail_open_and_url():
    note = format_upgrade_notice(_make_upgrade())
    assert "could not be evaluated" in note
    assert "fail-open" in note.lower()
    assert "huggingface.co/zentropi-ai/cope-b-a4b" in note  # self-host escape hatch
    assert "https://zentropi.ai/subscription" in note


def test_should_surface_upgrade_dedups_per_key():
    assert should_surface_upgrade("s1") is True
    assert should_surface_upgrade("s1") is False
    assert should_surface_upgrade("s2") is True
    assert should_surface_upgrade("") is True  # empty key can't dedup
    assert should_surface_upgrade("") is True


def test_upgrade_from_results_finds_first_upgrade():
    up = _make_upgrade()
    results = [
        ("b0", EvaluatorError("p0", "generic")),          # no upgrade
        ("b1", EvaluatorError("p1", "rl", upgrade=up)),   # carries upgrade
    ]
    assert upgrade_from_results(results) is up
    assert upgrade_from_results([EvaluatorError("p", "e")]) is None


# --- Phase 2: Claude Agent SDK (additionalContext) -------------------------

@pytest.mark.asyncio
async def test_claude_agent_sdk_injects_upgrade_note_once(monkeypatch):
    import reflexes.integrations.claude_agent_sdk as cas
    up = _make_upgrade()

    async def _stub_safe_acheck(**kwargs):
        return EvaluatorError(policy_name="Sycophancy", error=str(up), upgrade=up)

    import reflexes.core as _core2
    monkeypatch.setattr(_core2, "safe_acheck", _stub_safe_acheck)
    hook = cas._make_pre_tool_use_hook([_Binding()], cas._SessionMemory())

    out1 = await hook({"tool_name": "Bash", "tool_input": {"cmd": "ls"}}, "id", {})
    ctx = out1["hookSpecificOutput"]["additionalContext"]
    assert out1["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "could not be evaluated" in ctx
    assert "fail-open" in ctx.lower()

    # Second fire in the same process → deduped, no note re-injected.
    out2 = await hook({"tool_name": "Bash", "tool_input": {"cmd": "ls"}}, "id", {})
    assert out2 == {}


# --- Phase 2: LangGraph (reflex_flags state) -------------------------------

@pytest.mark.asyncio
def test_langgraph_extract_system_message():
    """Standing-instructions extraction (langchain-free — pure function)."""
    from reflexes.integrations.langgraph import _extract_system_message
    assert _extract_system_message({"messages": [{"role": "system", "content": "SYS"}]}) == "SYS"
    assert _extract_system_message({"messages": [{"role": "user", "content": "hi"}]}) is None
    assert _extract_system_message({}) is None
    assert _extract_system_message(None) is None
    obj = type("M", (), {"type": "system", "content": "OBJSYS"})()
    assert _extract_system_message({"messages": [obj]}) == "OBJSYS"


async def test_langgraph_evaluate_bindings_returns_upgrade(monkeypatch):
    import reflexes.integrations.langgraph as lg
    up = _make_upgrade()

    async def _stub_safe_acheck(**kwargs):
        return EvaluatorError(policy_name="Sycophancy", error=str(up), upgrade=up)

    import reflexes.core as _core
    monkeypatch.setattr(_core, "safe_acheck", _stub_safe_acheck)
    matches, upgrade = await lg._evaluate_bindings([_Binding()], "content")
    assert matches == []
    assert upgrade is up


# --- Phase 2: Google ADK (context.state) -----------------------------------

def test_google_adk_surface_upgrade_appends_once():
    import reflexes.integrations.google_adk as adk

    class _Ctx:
        def __init__(self):
            self.state = {}

    ctx = _Ctx()
    up = _make_upgrade()
    adk._surface_upgrade(ctx, up)
    flags = ctx.state["reflex_flags"]
    assert len(flags) == 1
    assert "could not be evaluated" in flags[0]["notice"]
    assert ctx.state["_reflex_upgrade_surfaced"] is True

    # Idempotent per session (state marker dedups).
    adk._surface_upgrade(ctx, up)
    assert len(ctx.state["reflex_flags"]) == 1

    # No-op when there's no upgrade.
    ctx2 = _Ctx()
    adk._surface_upgrade(ctx2, None)
    assert "reflex_flags" not in ctx2.state
