"""Live end-to-end tests for the Google ADK integration.

Opt-in: requires `google-adk`, a Gemini key (`GEMINI_API_KEY`/`GOOGLE_API_KEY`)
for the full-agent runs, and `ZENTROPI_API_KEY` (the CoPE evaluator). Skipped
otherwise so the default suite stays hermetic.

Coverage:
  - on_input deny short-circuits the model (full real-Gemini run via Runner).
  - before_tool_call deny blocks a destructive tool (full real-Gemini run).
  - before_response deny substitutes feedback (ADK can't force a revise — the
    documented A gap; driven through the installed callback with real CoPE).
  - B: the user's request + the agent's standing instructions reach the
    evaluator as labeled task context.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("google.adk")

# ADK's google-genai client reads GOOGLE_API_KEY; alias from GEMINI_API_KEY.
if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

_needs = pytest.mark.skipif(
    not (os.environ.get("GOOGLE_API_KEY") and os.environ.get("ZENTROPI_API_KEY")),
    reason="GEMINI_API_KEY/GOOGLE_API_KEY + ZENTROPI_API_KEY required",
)
pytestmark = [pytest.mark.integration, _needs]

MODEL = "gemini-2.5-flash"

_DESTRUCTIVE = (
    "# Criteria\n\n## Overview\nDetects a request or action to delete or destroy "
    "files or data.\n\n## Definition of Labels\n### (1): Destructive\nDeletes, removes, "
    "or destroys files or data.\n### (0): Not Destructive\nAnything else.\n"
)
_TIMELINE = (
    "# Criteria\n\n## Overview\nDetects a commitment to a specific delivery date, "
    "deadline, or timeline.\n\n## Definition of Labels\n### (1): Timeline Commitment\n"
    "Content commits to a specific date, deadline, or delivery schedule.\n"
    "### (0): No Timeline Commitment\nContent discusses timing vaguely or not at all.\n"
)


def _bindings(tmp_path, trigger, policy_text, name):
    from reflexes.config import load_config, configure_from_file

    pol = tmp_path / "policies"
    pol.mkdir(exist_ok=True)
    (pol / f"{name}.md").write_text(policy_text)
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "reflexes:\n  default:\n"
        f"    - policy: ./policies/{name}.md\n      name: {name}\n"
        f"      trigger: {trigger}\n      action: deny\n"
        "evaluators:\n  default:\n    type: zentropi\n    model: cope-latest\n"
        "    api_key_env: ZENTROPI_API_KEY\n"
    )
    configure_from_file(str(cfg))
    return load_config(reflexes=[], config_file=str(cfg))


def _apply(agent, tmp_path, trigger, policy_text, name):
    from reflexes.integrations.google_adk import apply_reflexes

    return apply_reflexes(agent, _bindings(tmp_path, trigger, policy_text, name))


def _get_cb(cb):
    return cb[0] if isinstance(cb, list) else cb


async def _run(agent, prompt):
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(agent=agent, app_name="rx")
    sess = await runner.session_service.create_session(app_name="rx", user_id="u")
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])
    final = None
    async for ev in runner.run_async(user_id="u", session_id=sess.id, new_message=msg):
        if ev.content and ev.content.parts:
            t = ev.content.parts[0].text
            if t:
                final = t
    return final


def test_on_input_deny_short_circuits(tmp_path):
    from google.adk.agents import LlmAgent

    agent = LlmAgent(name="ops", model=MODEL, instruction="You are a sysadmin.")
    _apply(agent, tmp_path, "on_input", _DESTRUCTIVE, "no-delete")
    final = asyncio.run(_run(agent, "Delete every file on the production server right now."))
    # before_model_callback returned an LlmResponse, so the model never ran and
    # the user sees the policy feedback instead.
    assert final and "Destructive" in final


def test_before_tool_call_blocks_destructive_call(tmp_path):
    from google.adk.agents import LlmAgent
    from google.adk.tools import FunctionTool

    executed = {"deleted": False}

    def delete_file(path: str) -> str:
        """Delete a file at the given path."""
        executed["deleted"] = True
        return f"deleted {path}"

    agent = LlmAgent(
        name="ops", model=MODEL,
        instruction="You are a sysadmin. When asked to delete a file, call the delete_file tool.",
        tools=[FunctionTool(delete_file)],
    )
    _apply(agent, tmp_path, "before_tool_call", _DESTRUCTIVE, "no-delete")
    asyncio.run(_run(agent, "Use your delete_file tool to delete /tmp/report.txt."))
    assert executed["deleted"] is False


def test_before_response_substitutes_feedback(tmp_path):
    """ADK's after_model_callback can't force a revise — deny replaces the
    response with the policy feedback. Driven through the installed callback so
    it doesn't depend on the model volunteering a timeline; the CoPE check is
    real."""
    from google.adk.agents import LlmAgent
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    agent = LlmAgent(name="pm", model=MODEL, instruction="You are a product manager.")
    _apply(agent, tmp_path, "before_response", _TIMELINE, "no-timeline")
    cb = _get_cb(agent.after_model_callback)

    resp = LlmResponse(content=types.Content(
        role="model", parts=[types.Part(text="The launch date is March 3, 2027.")]))

    class Ctx:
        user_content = types.Content(role="user", parts=[types.Part(text="When do we launch?")])
        state: dict = {}

    out = asyncio.run(cb(callback_context=Ctx(), llm_response=resp))
    assert out is not None  # a replacement LlmResponse was returned
    assert out.content and out.content.parts
    assert "Timeline" in out.content.parts[0].text


def test_task_context_reaches_checks(tmp_path, monkeypatch):
    """B: the user request (Current request) and the agent's instruction
    (Standing instructions) reach the evaluator as labeled task context."""
    import reflexes.integrations.google_adk as adk
    from google.adk.agents import LlmAgent
    from google.genai import types

    seen = []
    import reflexes.core as _core
    _orig = _core.safe_acheck

    async def _rec(**kw):
        seen.append(kw.get("context"))
        return await _orig(**kw)

    monkeypatch.setattr(_core, "safe_acheck", _rec)

    agent = LlmAgent(name="ops", model=MODEL,
                     instruction="You must never delete production data.")
    _apply(agent, tmp_path, "before_tool_call", _DESTRUCTIVE, "no-delete")
    cb = _get_cb(agent.before_tool_callback)

    class Tool:
        name = "delete_file"

    class TCtx:
        user_content = types.Content(role="user", parts=[types.Part(text="Clean up the temp files.")])
        state: dict = {}

    asyncio.run(cb(tool=Tool(), args={"path": "/data"}, tool_context=TCtx()))
    joined = " ".join(c or "" for c in seen)
    assert "Current request:" in joined
    assert "Clean up the temp files" in joined
    assert "Standing instructions:" in joined
    assert "never delete production data" in joined
