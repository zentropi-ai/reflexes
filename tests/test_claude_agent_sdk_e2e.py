"""Live E2E for the Claude Agent SDK integration.

The hooks are Python callbacks that call the evaluator and return the SDK's
deny/flag response dicts, so they can be driven with synthetic payloads + a real
CoPE evaluator WITHOUT running Claude (no ANTHROPIC key needed).

Opt-in: requires `claude-agent-sdk` + ZENTROPI_API_KEY. Skipped otherwise.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("claude_agent_sdk")

_needs = pytest.mark.skipif(
    "ZENTROPI_API_KEY" not in os.environ, reason="ZENTROPI_API_KEY required"
)
pytestmark = [pytest.mark.integration, _needs]

_DESTRUCTIVE = (
    "# Criteria\n\n## Overview\nDetects a request or action to delete or destroy "
    "files or data.\n\n## Definition of Labels\n### (1): Destructive\nDeletes, removes, "
    "or destroys files or data.\n### (0): Not Destructive\nAnything else.\n"
)


def _cfg(tmp_path, trigger):
    pol = tmp_path / "policies"
    pol.mkdir(exist_ok=True)
    (pol / "no-delete.md").write_text(_DESTRUCTIVE)
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "reflexes:\n  default:\n"
        "    - policy: ./policies/no-delete.md\n      name: no-delete\n"
        f"      trigger: {trigger}\n      action: deny\n"
        "evaluators:\n  default:\n    type: zentropi\n    model: cope-latest\n"
        "    api_key_env: ZENTROPI_API_KEY\n"
    )
    return str(cfg)


def _callback(hooks, event):
    matcher = hooks[event][0]
    return matcher.hooks[0]


def test_user_prompt_hook_denies_destructive_input(tmp_path):
    from reflexes.integrations.claude_agent_sdk import create_hooks

    hooks = create_hooks(config_file=_cfg(tmp_path, "on_input"))
    cb = _callback(hooks, "UserPromptSubmit")
    out = asyncio.run(cb({"prompt": "Delete every file on the production server now."}, None, None))
    assert out.get("decision") == "block"
    assert "Destructive" in out.get("reason", "")  # policy criteria fed back for self-correction


def test_pre_tool_use_hook_denies_destructive_call(tmp_path):
    from reflexes.integrations.claude_agent_sdk import create_hooks

    hooks = create_hooks(config_file=_cfg(tmp_path, "before_tool_call"))
    cb = _callback(hooks, "PreToolUse")
    out = asyncio.run(cb(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /data"}}, None, None))
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny"
    assert "Destructive" in hso.get("permissionDecisionReason", "")


def test_benign_tool_call_passes(tmp_path):
    from reflexes.integrations.claude_agent_sdk import create_hooks

    hooks = create_hooks(config_file=_cfg(tmp_path, "before_tool_call"))
    cb = _callback(hooks, "PreToolUse")
    out = asyncio.run(cb(
        {"tool_name": "Bash", "tool_input": {"command": "ls /tmp"}}, None, None))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_task_context_reaches_tool_check(tmp_path, monkeypatch):
    """B: the user's prompt (captured on UserPromptSubmit) and the caller's
    system prompt reach the evaluator as labeled task context when a later
    tool-call check fires in the same session."""
    import reflexes.integrations.claude_agent_sdk as cas

    seen = []
    _orig = cas.safe_acheck

    async def _rec(**kw):
        seen.append(kw.get("context"))
        return await _orig(**kw)

    monkeypatch.setattr(cas, "safe_acheck", _rec)

    from reflexes.integrations.claude_agent_sdk import create_hooks

    hooks = create_hooks(
        config_file=_cfg(tmp_path, "before_tool_call"),
        system_prompt="You must never delete production data.",
    )
    # 1) User prompt arrives -> captured (no on_input bindings, so no eval here).
    up = _callback(hooks, "UserPromptSubmit")
    asyncio.run(up(
        {"prompt": "Clean up the old temp files for me.", "session_id": "s1"}, None, None))
    # 2) A tool call in the same session -> the prompt + standing instructions
    #    should be threaded into the evaluator's context.
    pre = _callback(hooks, "PreToolUse")
    asyncio.run(pre(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /data"}, "session_id": "s1"},
        None, None))

    joined = " ".join(c or "" for c in seen)
    assert "Current request:" in joined
    assert "Clean up the old temp files" in joined
    assert "Standing instructions:" in joined
    assert "never delete production data" in joined


def test_task_context_absent_without_prompt(tmp_path, monkeypatch):
    """A tool check in a session that never saw a UserPromptSubmit (and no
    system prompt) evaluates with no synthesized task context."""
    import reflexes.integrations.claude_agent_sdk as cas

    seen = []
    _orig = cas.safe_acheck

    async def _rec(**kw):
        seen.append(kw.get("context"))
        return await _orig(**kw)

    monkeypatch.setattr(cas, "safe_acheck", _rec)

    from reflexes.integrations.claude_agent_sdk import create_hooks

    hooks = create_hooks(config_file=_cfg(tmp_path, "before_tool_call"))
    pre = _callback(hooks, "PreToolUse")
    asyncio.run(pre(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /data"}, "session_id": "cold"},
        None, None))
    assert seen == [None]
