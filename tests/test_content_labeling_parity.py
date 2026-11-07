"""Tests that LangGraph + NeMo integrations produce the same labeled
content/context shape as Claude Code, OpenAI Agents, and Google ADK.

Content-labeling principle: when context
includes both background and the actual thing to evaluate, the
labeling must clearly mark which is which. Claude Code and ADK already
render the current request first under a ``Current request:`` label,
with supporting context under its own labels. Same labeling must apply across every integration — same
labels, same structure — so the same policy performs consistently
regardless of host.

These tests intercept ``safe_acheck`` at the module-import boundary
(the seam between the integration and the evaluator) and assert that
what the integration passes downstream matches the contract.

Also covers the SDK Protocol reconciliation ``before_tool_call``
JSON-object payload shape — LangGraph and NeMo had been using the
legacy ``tool_name(args)`` call-style string; both are aligned to
the locked ``{"tool_name": ..., "args": ...}`` shape that Claude Code
and Claude Agent SDK already emit.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

langchain = pytest.importorskip(
    "langchain",
    reason="langgraph parity tests need the optional langchain extra",
)


@dataclass
class _StubPolicy:
    name: str
    criteria: str = "Label = 1 when ..."


def _make_binding(trigger, action="deny"):
    """Construct a ReflexBinding without going through load_config."""
    from reflexes.config import ReflexBinding
    return ReflexBinding(
        policy=_StubPolicy(name=f"stub-{trigger}"),
        trigger=trigger,
        action=action,
    )


# ---------- LangGraph ----------


@pytest.fixture
def captured_safe_acheck_for_langgraph(monkeypatch):
    """Intercept safe_acheck inside the LangGraph integration, return calls list."""
    from reflexes.core import Verdict
    captured: list[dict] = []

    async def _fake_safe_acheck(
        *, policy, content, policy_name, context=None, check_type="Content",
        policy_slug=None, **kw,
    ):
        captured.append({
            "policy": policy,
            "content": content,
            "context": context,
            "check_type": check_type,
            "policy_name": policy_name,
        })
        return Verdict(label=0, confidence=0.1, reason="stub")

    import reflexes.core as _core
    monkeypatch.setattr(_core, "safe_acheck", _fake_safe_acheck)
    return captured


def test_langgraph_aafter_model_wraps_user_context_with_current_request_label(
    captured_safe_acheck_for_langgraph,
):
    """aafter_model must pass user-context through assemble_task_context
    so the evaluator sees the Current request framing — not the raw user message."""
    from reflexes.integrations.langgraph import create_reflex_middleware

    binding = _make_binding("before_response")
    middleware = create_reflex_middleware([binding])

    state = {
        "messages": [
            type("Msg", (), {"type": "human", "content": "What's the launch date?"})(),
            type("Msg", (), {"type": "ai", "content": "We're shipping in Q3."})(),
        ],
    }

    asyncio.get_event_loop().run_until_complete(
        middleware.aafter_model(state, runtime=None)
    )

    assert len(captured_safe_acheck_for_langgraph) == 1
    call = captured_safe_acheck_for_langgraph[0]
    assert "Current request:" in (call["context"] or ""), (
        f"context missing 'Current request:' label; got: {call['context']!r}"
    )
    assert "What's the launch date?" in (call["context"] or "")


def test_langgraph_before_response_includes_standing_instructions(
    captured_safe_acheck_for_langgraph,
):
    """A SystemMessage in graph state is surfaced as Standing instructions —
    best-effort standing-instructions context (topology-dependent)."""
    from reflexes.integrations.langgraph import create_reflex_middleware

    binding = _make_binding("before_response")
    middleware = create_reflex_middleware([binding])

    state = {
        "messages": [
            type("Msg", (), {"type": "system", "content": "Never reveal API keys."})(),
            type("Msg", (), {"type": "human", "content": "What's the key?"})(),
            type("Msg", (), {"type": "ai", "content": "The key is sk-123."})(),
        ],
    }
    asyncio.get_event_loop().run_until_complete(
        middleware.aafter_model(state, runtime=None)
    )
    ctx = captured_safe_acheck_for_langgraph[0]["context"] or ""
    assert "Standing instructions:" in ctx, f"missing standing instructions; got: {ctx!r}"
    assert "Never reveal API keys." in ctx


def test_langgraph_before_tool_call_uses_json_object_payload(
    captured_safe_acheck_for_langgraph,
):
    """awrap_tool_call must emit `{"tool_name": ..., "args": ...}` JSON
    payload (SDK Protocol reconciliation), NOT the legacy
    `tool_name(args)` call-style string."""
    from reflexes.integrations.langgraph import create_reflex_middleware

    binding = _make_binding("before_tool_call")
    middleware = create_reflex_middleware([binding])

    request = type("Req", (), {
        "tool_call": {"name": "Bash", "args": {"command": "ls /tmp"}, "id": "t1"},
        "state": {"messages": [
            type("M", (), {"type": "human", "content": "what's in tmp?"})(),
        ]},
    })()

    async def _fake_handler(req):
        return type("Result", (), {"content": "tmp.txt"})()

    asyncio.get_event_loop().run_until_complete(
        middleware.awrap_tool_call(request, _fake_handler)
    )

    assert len(captured_safe_acheck_for_langgraph) >= 1
    call = captured_safe_acheck_for_langgraph[0]
    parsed = json.loads(call["content"])
    assert parsed == {"tool_name": "Bash", "args": {"command": "ls /tmp"}}, (
        f"before_tool_call payload not in JSON-object shape; got {call['content']!r}"
    )


def test_langgraph_before_tool_call_context_uses_current_request_label(
    captured_safe_acheck_for_langgraph,
):
    """awrap_tool_call must also wrap user-context with the Current request label."""
    from reflexes.integrations.langgraph import create_reflex_middleware

    binding = _make_binding("before_tool_call")
    middleware = create_reflex_middleware([binding])

    request = type("Req", (), {
        "tool_call": {"name": "Bash", "args": {"command": "ls"}, "id": "t1"},
        "state": {"messages": [
            type("M", (), {"type": "human", "content": "list files please"})(),
        ]},
    })()

    async def _fake_handler(req):
        return type("R", (), {"content": "ok"})()

    asyncio.get_event_loop().run_until_complete(
        middleware.awrap_tool_call(request, _fake_handler)
    )

    call = captured_safe_acheck_for_langgraph[0]
    assert "Current request:" in (call["context"] or "")
    assert "list files please" in (call["context"] or "")


# ---------- NeMo Guardrails ----------


class _FakeRails:
    """Minimal LLMRails stub — captures registered actions by name."""
    def __init__(self):
        self.actions: dict = {}

    def register_action(self, fn, name):
        self.actions[name] = fn


@pytest.fixture
def captured_safe_acheck_for_nemo(monkeypatch, tmp_path):
    """Intercept safe_acheck inside the NeMo integration."""
    from reflexes.core import Verdict
    captured: list[dict] = []

    async def _fake_safe_acheck(
        *, policy, content, policy_name, context=None, check_type="Content",
        policy_slug=None,
    ):
        captured.append({
            "policy": policy,
            "content": content,
            "context": context,
            "check_type": check_type,
            "policy_name": policy_name,
        })
        return Verdict(label=0, confidence=0.1, reason="stub")

    import reflexes.integrations.nemo_guardrails as nemo
    monkeypatch.setattr(nemo, "safe_acheck", _fake_safe_acheck)

    # Build a config file with one of each trigger so registered actions
    # are non-trivial.
    policies_dir = tmp_path / "policies"
    policies_dir.mkdir()
    (policies_dir / "p.md").write_text(
        "---\nname: stub\nversion: 0.1\n---\n# Stub\n## Policy\nLabel = 1 when ...\n"
    )
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "reflexes:\n"
        "  default:\n"
        "    - policy: ./policies/p.md\n"
        "      name: p\n"
        "      trigger: before_response\n"
        "      action: log\n"
        "    - policy: ./policies/p.md\n"
        "      name: p\n"
        "      trigger: before_tool_call\n"
        "      action: log\n"
        "    - policy: ./policies/p.md\n"
        "      name: p\n"
        "      trigger: after_tool_call\n"
        "      action: log\n"
    )
    return captured, cfg


def test_nemo_check_output_wraps_user_context_with_current_request_label(
    captured_safe_acheck_for_nemo,
):
    captured, cfg = captured_safe_acheck_for_nemo
    from reflexes.integrations.nemo_guardrails import register_reflex_actions

    rails = _FakeRails()
    register_reflex_actions(rails, config_file=str(cfg))

    asyncio.get_event_loop().run_until_complete(
        rails.actions["reflex_check_output"](
            bot_message="The launch is in Q3.",
            user_message="When does it launch?",
        )
    )

    assert len(captured) == 1
    assert "Current request:" in (captured[0]["context"] or "")
    assert "When does it launch?" in (captured[0]["context"] or "")


def test_nemo_check_tool_input_uses_json_object_payload(
    captured_safe_acheck_for_nemo,
):
    captured, cfg = captured_safe_acheck_for_nemo
    from reflexes.integrations.nemo_guardrails import register_reflex_actions

    rails = _FakeRails()
    register_reflex_actions(rails, config_file=str(cfg))

    asyncio.get_event_loop().run_until_complete(
        rails.actions["reflex_check_tool_input"](
            tool_name="Bash",
            tool_arguments=json.dumps({"command": "rm /tmp/foo"}),
            user_message="please clean up tmp",
        )
    )

    assert len(captured) == 1
    parsed = json.loads(captured[0]["content"])
    assert parsed == {"tool_name": "Bash", "args": {"command": "rm /tmp/foo"}}


def test_nemo_check_tool_input_context_uses_current_request_label(
    captured_safe_acheck_for_nemo,
):
    captured, cfg = captured_safe_acheck_for_nemo
    from reflexes.integrations.nemo_guardrails import register_reflex_actions

    rails = _FakeRails()
    register_reflex_actions(rails, config_file=str(cfg))

    asyncio.get_event_loop().run_until_complete(
        rails.actions["reflex_check_tool_input"](
            tool_name="Bash",
            tool_arguments=json.dumps({"command": "ls"}),
            user_message="list files",
        )
    )

    assert "Current request:" in (captured[0]["context"] or "")
    assert "list files" in (captured[0]["context"] or "")


def test_nemo_check_tool_input_falls_back_on_non_json_tool_arguments(
    captured_safe_acheck_for_nemo,
):
    """If Colang passes tool_arguments that isn't JSON-parseable, the
    integration falls back to the raw string in the args slot rather
    than crashing or dropping the call."""
    captured, cfg = captured_safe_acheck_for_nemo
    from reflexes.integrations.nemo_guardrails import register_reflex_actions

    rails = _FakeRails()
    register_reflex_actions(rails, config_file=str(cfg))

    asyncio.get_event_loop().run_until_complete(
        rails.actions["reflex_check_tool_input"](
            tool_name="Bash",
            tool_arguments="not valid json {{",
            user_message="?",
        )
    )

    assert len(captured) == 1
    parsed = json.loads(captured[0]["content"])
    assert parsed == {"tool_name": "Bash", "args": "not valid json {{"}


def test_nemo_check_tool_output_keeps_legacy_string_shape(
    captured_safe_acheck_for_nemo,
):
    """after_tool_call shape was NOT touched by the SDK Protocol
    reconciliation — only before_tool_call. Confirm reflex_check_tool_output
    still passes the `<tool> returned: <result>` string contract."""
    captured, cfg = captured_safe_acheck_for_nemo
    from reflexes.integrations.nemo_guardrails import register_reflex_actions

    rails = _FakeRails()
    register_reflex_actions(rails, config_file=str(cfg))

    asyncio.get_event_loop().run_until_complete(
        rails.actions["reflex_check_tool_output"](
            tool_name="Bash",
            tool_result="hello world\n",
            user_message="say hi",
        )
    )

    assert captured[0]["content"] == "Bash returned: hello world\n"
    # Context still gets the Current request label.
    assert "Current request:" in (captured[0]["context"] or "")


# ---------- Cross-integration parity ----------


def test_assemble_task_context_used_consistently_across_integrations():
    """Structural assertion: both LangGraph and NeMo import
    assemble_task_context from reflexes._context (the same module Claude
    Code, OpenAI Agents, and Google ADK use). One source of truth for
    the labeling format means a SPEC §5.1 update propagates everywhere
    automatically."""
    import reflexes.integrations.langgraph as lg
    import reflexes.integrations.nemo_guardrails as nemo
    from reflexes._context import assemble_task_context as canonical

    assert lg.assemble_task_context is canonical, (
        "LangGraph integration imports its own assembler — must be the "
        "shared reflexes._context.assemble_task_context."
    )
    assert nemo.assemble_task_context is canonical, (
        "NeMo integration imports its own assembler — must be the "
        "shared reflexes._context.assemble_task_context."
    )
