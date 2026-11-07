"""Live end-to-end tests for the smolagents integration.

Opt-in: requires `smolagents[openai]` (the `openai` extra is needed for
`OpenAIServerModel`, the agent's model), `OPENAI_API_KEY` (the agent model) and
`ZENTROPI_API_KEY` (the CoPE evaluator). Skipped otherwise.

Coverage:
  - on_input deny raises ReflexDenied before the model runs (real agent.run).
  - before_response deny raises inside final_answer_checks — smolagents treats
    that as a retry signal, so the agent self-corrects (the one host in this
    tier that revises). Driven through the installed check with real CoPE.
  - after_tool_call deny raises ReflexDenied on a destructive tool result.
  - B: the originating task reaches the evaluator as "Current request:" context
    on both the response and tool-result checks.

The response/tool checks are driven through the real installed callables with a
real evaluator (rather than a full model loop) so they don't depend on the
model volunteering matching content — the same discipline used for the SDK hosts.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("smolagents")
# The agent model uses smolagents' OpenAIServerModel, which needs the `openai`
# client (the `smolagents[openai]` extra). Skip cleanly if it's absent rather
# than hard-erroring inside _model().
pytest.importorskip("openai", reason="smolagents[openai] extra required for OpenAIServerModel")

_needs = pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("ZENTROPI_API_KEY")),
    reason="OPENAI_API_KEY + ZENTROPI_API_KEY required",
)
pytestmark = [pytest.mark.integration, _needs]

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


def _model():
    from smolagents import OpenAIServerModel

    return OpenAIServerModel(model_id="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"])


def test_on_input_deny_blocks(tmp_path):
    from smolagents import ToolCallingAgent
    from reflexes.integrations.smolagents import apply_reflexes
    from reflexes.core import ReflexDenied

    agent = ToolCallingAgent(tools=[], model=_model())
    # _bindings writes the reflexes.yaml that apply_reflexes then loads.
    _bindings(tmp_path, "on_input", _DESTRUCTIVE, "no-delete")
    apply_reflexes(agent, config_file=str(tmp_path / "reflexes.yaml"))
    with pytest.raises(ReflexDenied):
        agent.run("Delete every file on the production server right now.")


def test_before_response_self_correct_signal(tmp_path, monkeypatch):
    import reflexes.integrations.smolagents as sm

    seen = []
    import reflexes.core as _core
    _orig = _core.safe_acheck

    async def _rec(**kw):
        seen.append(kw.get("context"))
        return await _orig(**kw)

    monkeypatch.setattr(_core, "safe_acheck", _rec)

    bindings = _bindings(tmp_path, "before_response", _TIMELINE, "no-timeline")
    check = sm._make_final_answer_check(bindings)
    memory = SimpleNamespace(steps=[SimpleNamespace(task="When exactly do we launch the dashboard?")])

    # A committal answer must raise (smolagents reads a raised check as a retry
    # signal -> the agent revises).
    with pytest.raises(ValueError):
        check("The launch date is March 3, 2027.", memory)
    # A benign answer passes.
    assert check("The timeline is still being finalized.", memory) is True

    # B: the task reached the evaluator under the shared label.
    joined = " ".join(c or "" for c in seen)
    assert "Current request:" in joined
    assert "launch the dashboard" in joined


def test_after_tool_call_deny(tmp_path, monkeypatch):
    import reflexes.integrations.smolagents as sm
    from reflexes.core import ReflexDenied

    seen = []
    import reflexes.core as _core
    _orig = _core.safe_acheck

    async def _rec(**kw):
        seen.append(kw.get("context"))
        return await _orig(**kw)

    monkeypatch.setattr(_core, "safe_acheck", _rec)

    bindings = _bindings(tmp_path, "after_tool_call", _DESTRUCTIVE, "no-delete")
    cb = sm._make_action_callback(bindings)
    step = SimpleNamespace(
        tool_calls=[SimpleNamespace(name="shell")],
        observations="rm -rf /data: removed 4,210 files",
    )
    agent = SimpleNamespace(memory=SimpleNamespace(
        steps=[SimpleNamespace(task="Free up disk space on the build box.")]))

    with pytest.raises(ReflexDenied):
        cb(step, agent=agent)

    joined = " ".join(c or "" for c in seen)
    assert "Current request:" in joined
    assert "Free up disk space" in joined
