"""Tests for the SDK Protocol reconciliation.

Covers three independent contract changes that prepare the codebase for
LocalHeuristicEvaluator:

(a) `Verdict.mode: Literal["heuristic", "cope"] | None` field — provenance
    so adapters can surface "[DEMO]" framing on heuristic verdicts and
    dashboards can split CoPE vs heuristic flows.

(b) `policy_slug: str | None` parameter added to `Evaluator.evaluate`
    Protocol. Existing four evaluators (zentropi, anthropic, gemini,
    openai) accept and ignore it; the heuristic evaluator dispatches
    on it (and will raise ValueError if None — tested when that lands).

(c) `before_tool_call` payload shape — `extract_content` (claude_code.py)
    and the Claude Agent SDK PreToolUse hook now emit a JSON object
    `{"tool_name": ..., "args": ...}` rather than the call-style string
    `tool_name(<json>)`. Lets structural evaluators dispatch on fields
    without re-parsing.
"""

from __future__ import annotations

import json

import pytest

from reflexes import core
from reflexes.core import Verdict


# -----------------------------------------------------------------------
# (a) Verdict.mode field
# -----------------------------------------------------------------------


def test_verdict_mode_defaults_to_none():
    """Existing call sites that don't pass `mode` keep working — None
    is the default for third-party LLM evaluators (anthropic/gemini/openai)
    and any pre-PR call sites."""
    v = Verdict(label=0, confidence=0.9)
    assert v.mode is None


def test_verdict_mode_accepts_cope():
    """Zentropi evaluator sets mode="cope" so dashboards can split it
    from heuristic verdicts."""
    v = Verdict(label=1, confidence=0.95, mode="cope")
    assert v.mode == "cope"


def test_verdict_mode_accepts_heuristic():
    """LocalHeuristicEvaluator (when implemented) sets mode="heuristic"
    so adapters can prefix '[DEMO]' framing in the explanation."""
    v = Verdict(label=1, confidence=0.8, mode="heuristic")
    assert v.mode == "heuristic"


# -----------------------------------------------------------------------
# (b) policy_slug parameter on Evaluator.evaluate Protocol
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acheck_forwards_policy_slug_to_evaluator(monkeypatch):
    """acheck must pass `policy_slug` through to the evaluator. Heuristic
    evaluator depends on this for dispatch."""
    received = {}

    class _CapturingEvaluator:
        async def evaluate(self, *, policy, content, reason=False, policy_slug=None):
            received["policy_slug"] = policy_slug
            return Verdict(label=0, confidence=1.0, mode="heuristic")

    monkeypatch.setattr(core, "_evaluator", _CapturingEvaluator())
    await core.acheck(policy="dummy", content="dummy", policy_slug="safe-delete")
    assert received["policy_slug"] == "safe-delete"


@pytest.mark.asyncio
async def test_acheck_omits_policy_slug_defaults_to_none(monkeypatch):
    """When the caller doesn't supply policy_slug (existing call sites),
    the evaluator receives None and continues working."""
    received = {}

    class _CapturingEvaluator:
        async def evaluate(self, *, policy, content, reason=False, policy_slug=None):
            received["policy_slug"] = policy_slug
            return Verdict(label=0, confidence=1.0)

    monkeypatch.setattr(core, "_evaluator", _CapturingEvaluator())
    await core.acheck(policy="dummy", content="dummy")
    assert received["policy_slug"] is None


@pytest.mark.asyncio
async def test_safe_acheck_forwards_policy_slug(monkeypatch):
    """safe_acheck wraps acheck — confirm policy_slug threads through
    the wrapper so adapters can pass it via the safe path."""
    received = {}

    class _CapturingEvaluator:
        async def evaluate(self, *, policy, content, reason=False, policy_slug=None):
            received["policy_slug"] = policy_slug
            return Verdict(label=0, confidence=1.0)

    monkeypatch.setattr(core, "_evaluator", _CapturingEvaluator())
    await core.safe_acheck(
        policy="dummy", content="dummy",
        policy_name="test", policy_slug="prompt-injection",
    )
    assert received["policy_slug"] == "prompt-injection"


def test_existing_evaluators_accept_policy_slug_kwarg():
    """All four shipped evaluators must accept `policy_slug` per the
    updated Protocol. They ignore the value (they evaluate from policy
    text directly), but the kwarg has to exist or callers break."""
    import inspect
    from reflexes.evaluators.zentropi import ZentropiEvaluator
    from reflexes.evaluators.anthropic import AnthropicEvaluator
    from reflexes.evaluators.gemini import GeminiEvaluator
    from reflexes.evaluators.openai_compatible import OpenAICompatibleEvaluator

    for cls in (
        ZentropiEvaluator, AnthropicEvaluator,
        GeminiEvaluator, OpenAICompatibleEvaluator,
    ):
        sig = inspect.signature(cls.evaluate)
        assert "policy_slug" in sig.parameters, (
            f"{cls.__name__}.evaluate must accept policy_slug kwarg "
            f"(got params: {list(sig.parameters)})"
        )
        # Default must be None so existing callers keep working.
        assert sig.parameters["policy_slug"].default is None, (
            f"{cls.__name__}.evaluate policy_slug default must be None"
        )


# -----------------------------------------------------------------------
# (c) before_tool_call payload shape — JSON object, not call-style string
# -----------------------------------------------------------------------


def test_claude_code_pre_tool_use_extract_returns_json_object():
    """PreToolUse content is JSON object so structural evaluators can
    dispatch on `tool_name` / `args` without re-parsing a call-style
    string."""
    from reflexes.integrations.claude_code import extract_content

    content = extract_content(
        {"tool_name": "Bash", "tool_input": {"command": "rm /tmp/foo"}},
        "PreToolUse",
    )
    parsed = json.loads(content)
    assert parsed == {
        "tool_name": "Bash",
        "args": {"command": "rm /tmp/foo"},
    }


def test_claude_code_pre_tool_use_handles_empty_tool_input():
    """Tools with no args still produce a parseable JSON object."""
    from reflexes.integrations.claude_code import extract_content

    content = extract_content({"tool_name": "Foo", "tool_input": {}}, "PreToolUse")
    assert json.loads(content) == {"tool_name": "Foo", "args": {}}


def test_claude_code_pre_tool_use_no_longer_emits_call_style_string():
    """Regression guard: structural evaluators were tripping on the
    call-style `Bash({"command":"..."})` shape because it required
    string-parsing. The JSON-object shape eliminates that."""
    from reflexes.integrations.claude_code import extract_content

    content = extract_content(
        {"tool_name": "Bash", "tool_input": {"command": "x"}},
        "PreToolUse",
    )
    # Must NOT start with the bare tool name followed by a paren —
    # that's the old call-style format.
    assert not content.startswith("Bash("), (
        f"PreToolUse content reverted to call-style format: {content!r}"
    )
    # Should be valid JSON.
    json.loads(content)
