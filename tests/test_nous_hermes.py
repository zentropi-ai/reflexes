"""Tests for the NousResearch Hermes integration.

Hermes' hook events are session/agent-loop level (`agent:step` fires
after tool execution + LLM response), so this integration is observer-
shape: deny still evaluates and surfaces structured findings, but the
actual block is one round-trip late ("deny-on-next-turn"). Tests cover:

- handle() routes only `agent:step` to evaluation; other events are no-op
- Defensive context extraction tolerates Hermes' under-documented schema
  (multiple plausible shapes for messages, tool_calls, response text)
- Findings carry the asymmetry_note for deny actions, None for flag/log
- Cache-warming on repeated agent:step calls (no re-parse of yaml)
- Crash-isolation: any exception inside evaluation returns None instead
  of bubbling up to Hermes (honors "callback crashes are logged + skipped")
- before_tool_call evaluator content is the JSON-object shape
- user-context wrapped through assemble_task_context (Current request label)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolated reflexes config wired to one binding per relevant trigger."""
    policies = tmp_path / "policies"
    policies.mkdir()
    # Policy name = filename stem ("stub"); the whole document is the criteria.
    (policies / "stub.md").write_text(
        "# Criteria\n\n## Overview\nStub policy — Label = 1 when ...\n"
    )
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "reflexes:\n"
        "  default:\n"
        "    - policy: ./policies/stub.md\n"
        "      name: stub\n"
        "      trigger: before_tool_call\n"
        "      action: deny\n"
        "    - policy: ./policies/stub.md\n"
        "      name: stub\n"
        "      trigger: after_tool_call\n"
        "      action: log\n"
        "    - policy: ./policies/stub.md\n"
        "      name: stub\n"
        "      trigger: before_response\n"
        "      action: flag\n"
    )
    monkeypatch.setenv("REFLEXES_CONFIG_FILE", str(cfg))
    # Clear the module-level cache so each test sees fresh load behavior.
    import reflexes.integrations.nous_hermes as nh
    nh._BINDINGS_CACHE.clear()
    return cfg


@pytest.fixture
def captured_safe_acheck(monkeypatch):
    """Intercept safe_acheck inside the Hermes integration."""
    from reflexes.core import Verdict
    captured: list[dict] = []

    async def _fake(*, policy, content, policy_name, context=None,
                    check_type="Content", policy_slug=None):
        captured.append({
            "policy_name": policy_name, "content": content,
            "context": context, "check_type": check_type,
        })
        # Return label=1 for every call — exercises the matched-policy path.
        return Verdict(label=1, confidence=0.95, reason="stub-match")

    import reflexes.integrations.nous_hermes as nh
    monkeypatch.setattr(nh, "safe_acheck", _fake)
    return captured


# ---------- Event routing ----------


def test_handle_returns_none_on_unrelated_events(hermes_env):
    from reflexes.integrations.nous_hermes import handle
    for ev in ("gateway:startup", "command:reset", "session:end", "unknown"):
        assert handle(ev, {}) is None, f"event {ev!r} should be no-op"


def test_handle_returns_none_on_agent_lifecycle_events(hermes_env):
    """agent:start and agent:end are accepted (forward-compat) but no-op
    in v0.1 — only agent:step has observable per-turn content."""
    from reflexes.integrations.nous_hermes import handle
    assert handle("agent:start", {}) is None
    assert handle("agent:end", {}) is None


def test_handle_no_op_when_no_bindings(tmp_path, monkeypatch):
    """If reflexes.yaml is missing or unparseable, integration returns
    None (Hermes contract: callback crashes are logged + skipped)."""
    monkeypatch.setenv("REFLEXES_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    import reflexes.integrations.nous_hermes as nh
    nh._BINDINGS_CACHE.clear()
    assert nh.handle("agent:step", {"messages": []}) is None


# ---------- Context extraction defensiveness ----------


def test_extract_user_message_explicit_field(hermes_env):
    from reflexes.integrations.nous_hermes import _extract_user_message
    assert _extract_user_message({"user_message": "hi"}) == "hi"
    assert _extract_user_message({"last_user_message": "hi"}) == "hi"


def test_extract_user_message_from_messages_list(hermes_env):
    from reflexes.integrations.nous_hermes import _extract_user_message
    ctx = {"messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "second"},
    ]}
    assert _extract_user_message(ctx) == "second"


def test_extract_user_message_handles_human_role_alias(hermes_env):
    from reflexes.integrations.nous_hermes import _extract_user_message
    ctx = {"messages": [{"role": "human", "content": "hello"}]}
    assert _extract_user_message(ctx) == "hello"


def test_extract_user_message_returns_none_when_absent(hermes_env):
    from reflexes.integrations.nous_hermes import _extract_user_message
    assert _extract_user_message({}) is None
    assert _extract_user_message({"messages": []}) is None
    assert _extract_user_message(None) is None


def test_extract_assistant_message_explicit_fields(hermes_env):
    from reflexes.integrations.nous_hermes import _extract_assistant_message
    assert _extract_assistant_message({"response": "ok"}) == "ok"
    assert _extract_assistant_message({"assistant_message": "ok"}) == "ok"
    assert _extract_assistant_message({"text": "ok"}) == "ok"


def test_extract_tool_calls_handles_string_args(hermes_env):
    """Hermes adapters that proxy OpenAI-style calls may emit args as
    a JSON string — integration must parse defensively."""
    from reflexes.integrations.nous_hermes import _extract_tool_calls
    ctx = {"tool_calls": [
        {"name": "Bash", "args": json.dumps({"command": "ls"}), "result": "x"},
    ]}
    out = _extract_tool_calls(ctx)
    assert out == [{"name": "Bash", "args": {"command": "ls"}, "result": "x"}]


def test_extract_tool_calls_falls_back_on_unparseable_args(hermes_env):
    from reflexes.integrations.nous_hermes import _extract_tool_calls
    ctx = {"tool_calls": [
        {"name": "Bash", "args": "not-json{{", "result": None},
    ]}
    out = _extract_tool_calls(ctx)
    assert out == [{"name": "Bash", "args": "not-json{{", "result": None}]


# ---------- Evaluator wiring ----------


def test_handle_evaluates_assistant_message_for_before_response(
    hermes_env, captured_safe_acheck,
):
    from reflexes.integrations.nous_hermes import handle
    ctx = {
        "user_message": "What's the launch date?",
        "response": "We're shipping in Q3.",
    }
    result = handle("agent:step", ctx)
    # Find the before_response evaluation among captured calls.
    response_calls = [c for c in captured_safe_acheck if c["check_type"] == "Agent response"]
    assert len(response_calls) == 1
    assert response_calls[0]["content"] == "We're shipping in Q3."
    assert "Current request:" in (response_calls[0]["context"] or "")
    # And findings include the flag finding.
    assert result is not None
    assert any(f["policy"] == "stub" and f["action"] == "flag"
               for f in result["reflex_findings"])


def test_handle_evaluates_tool_calls_with_json_object_payload(
    hermes_env, captured_safe_acheck,
):
    from reflexes.integrations.nous_hermes import handle
    ctx = {
        "user_message": "delete /tmp/x",
        "tool_calls": [{
            "name": "Bash",
            "args": {"command": "rm /tmp/x"},
            "result": "removed",
        }],
    }
    result = handle("agent:step", ctx)
    tool_calls = [c for c in captured_safe_acheck if c["check_type"] == "Tool call"]
    assert len(tool_calls) == 1
    parsed = json.loads(tool_calls[0]["content"])
    assert parsed == {"tool_name": "Bash", "args": {"command": "rm /tmp/x"}}


def test_handle_deny_findings_carry_asymmetry_note(
    hermes_env, captured_safe_acheck,
):
    """Deny actions on Hermes are observer-shape — finding must carry the
    documented one-turn-late annotation so consumers know it's not a
    same-turn block."""
    from reflexes.integrations.nous_hermes import handle
    ctx = {
        "user_message": "delete /tmp/x",
        "tool_calls": [{
            "name": "Bash", "args": {"command": "rm /tmp/x"}, "result": "ok",
        }],
    }
    result = handle("agent:step", ctx)
    deny_findings = [f for f in result["reflex_findings"] if f["action"] == "deny"]
    assert deny_findings, "expected a deny finding for the tool call"
    assert deny_findings[0]["asymmetry_note"] is not None
    assert "deny-on-next-turn" in deny_findings[0]["asymmetry_note"]


def test_handle_flag_findings_have_no_asymmetry_note(
    hermes_env, captured_safe_acheck,
):
    """Flag (and log) work fully on the same turn — no asymmetry."""
    from reflexes.integrations.nous_hermes import handle
    result = handle("agent:step", {"response": "anything"})
    flag_findings = [f for f in result["reflex_findings"] if f["action"] == "flag"]
    assert flag_findings
    assert flag_findings[0]["asymmetry_note"] is None


def test_handle_no_findings_returns_none(hermes_env, monkeypatch):
    """When safe_acheck always returns label=0, handle returns None."""
    from reflexes.core import Verdict
    import reflexes.integrations.nous_hermes as nh

    async def _no_match(*, policy, content, policy_name, context=None,
                        check_type="Content", policy_slug=None):
        return Verdict(label=0, confidence=0.05, reason="no match")
    monkeypatch.setattr(nh, "safe_acheck", _no_match)

    result = nh.handle("agent:step", {"response": "anything"})
    assert result is None


# ---------- Crash-isolation ----------


def test_handle_swallows_exceptions_returns_none(hermes_env, monkeypatch):
    """If anything inside _evaluate_step raises, handle returns None
    instead of crashing Hermes."""
    import reflexes.integrations.nous_hermes as nh

    async def _explode(*args, **kwargs):
        raise RuntimeError("simulated evaluator failure")
    monkeypatch.setattr(nh, "safe_acheck", _explode)

    # Should not raise; should not return findings.
    assert nh.handle("agent:step", {"response": "x"}) is None


# ---------- Caching ----------


def test_bindings_are_cached_across_calls(hermes_env, captured_safe_acheck):
    """Repeated agent:step calls hit the same parsed bindings — yaml is
    not re-loaded per turn."""
    import reflexes.integrations.nous_hermes as nh
    nh._BINDINGS_CACHE.clear()
    nh.handle("agent:step", {"response": "first"})
    cache_size_after_first = len(nh._BINDINGS_CACHE)
    nh.handle("agent:step", {"response": "second"})
    assert len(nh._BINDINGS_CACHE) == cache_size_after_first
    assert cache_size_after_first == 1


# ---------- Structural ----------


def test_imports_canonical_assemble_task_context():
    """Hermes integration must use the same context-labeling assembler
    as the other integrations — otherwise the labeled task-context
    framing drifts and policies perform inconsistently across hosts."""
    import reflexes.integrations.nous_hermes as nh
    from reflexes._context import assemble_task_context as canonical
    assert nh.assemble_task_context is canonical
