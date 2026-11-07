"""Registration-shape tests: prove each SDK integration attaches our callbacks
where the SDK/host will actually look for them.

This is the layer *between* "the callback enforces correctly" (covered by the
e2e + unit tests) and "a real agent fires the callback" (covered only by a true
live run). These assert the WIRING: after calling the integration's registration
entry point, the agent object holds our callbacks at the SDK's documented slots.

They are hermetic — no network, no model key, no evaluator call. Registration
only attaches closures; evaluation happens later. Tests needing a real SDK use
``importorskip`` so the default suite stays installable without heavy deps;
google_adk's registration is SDK-import-free, so it always runs.
"""
from __future__ import annotations

import pytest

from reflexes.config import load_config

_CRITERIA = "# Criteria\n\n## Overview\nAlways fires.\n"


def _bindings(tmp_path, specs):
    """specs: list of (name, trigger, action). Writes policy files + a yaml and
    returns parsed ReflexBindings (no evaluator configured — registration only)."""
    pol = tmp_path / "policies"
    pol.mkdir(exist_ok=True)
    lines = ["reflexes:", "  default:"]
    for name, trigger, action in specs:
        (pol / f"{name}.md").write_text(_CRITERIA)
        lines += [
            f"    - policy: ./policies/{name}.md",
            f"      name: {name}",
            f"      trigger: {trigger}",
            f"      action: {action}",
        ]
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text("\n".join(lines) + "\n")
    return load_config(reflexes=["default"], config_file=str(cfg)), str(cfg)


_ALL_TRIGGERS = [
    ("on-input", "on_input", "deny"),
    ("before-resp", "before_response", "deny"),
    ("before-tool", "before_tool_call", "deny"),
    ("after-tool", "after_tool_call", "flag"),
]


# ---------------------------------------------------------------------------
# Google ADK — SDK-import-free registration, so this always runs in CI.
# ---------------------------------------------------------------------------
def test_google_adk_attaches_one_callback_per_trigger(tmp_path):
    import types as _t
    from reflexes.integrations.google_adk import apply_reflexes

    bindings, _ = _bindings(tmp_path, _ALL_TRIGGERS)
    agent = _t.SimpleNamespace(
        before_model_callback=None, after_model_callback=None,
        before_tool_callback=None, after_tool_callback=None,
        tools=[], instruction="You are helpful.",
    )
    apply_reflexes(agent, bindings)

    # on_input → before_model, before_response → after_model, and the two tool
    # callbacks — each wired to a single callable (one batched callback/trigger).
    assert callable(agent.before_model_callback), "on_input not wired to before_model_callback"
    assert callable(agent.after_model_callback), "before_response not wired to after_model_callback"
    assert callable(agent.before_tool_callback), "before_tool_call not wired to before_tool_callback"
    assert callable(agent.after_tool_callback), "after_tool_call not wired to after_tool_callback"


def test_google_adk_preserves_existing_callback(tmp_path):
    import types as _t
    from reflexes.integrations.google_adk import apply_reflexes

    def _user_cb(*a, **k):
        return None

    bindings, _ = _bindings(tmp_path, [("on-input", "on_input", "deny")])
    agent = _t.SimpleNamespace(
        before_model_callback=_user_cb, after_model_callback=None,
        before_tool_callback=None, after_tool_callback=None,
        tools=[], instruction=None,
    )
    apply_reflexes(agent, bindings)
    # The user's pre-existing callback must not be dropped — ours is added alongside.
    cbs = agent.before_model_callback
    cbs = cbs if isinstance(cbs, list) else [cbs]
    assert _user_cb in cbs, "existing before_model_callback was clobbered"
    assert len(cbs) >= 2, "reflex callback not added alongside the existing one"


# ---------------------------------------------------------------------------
# OpenAI Agents SDK — real Agent/tool objects (needs the `agents` package).
# ---------------------------------------------------------------------------
def test_openai_agents_attaches_guardrails_per_trigger(tmp_path):
    pytest.importorskip("agents")
    from agents import Agent, function_tool
    from reflexes.integrations.openai_agents import apply_reflexes

    @function_tool
    def do_thing(path: str) -> str:
        "A tool."
        return path

    bindings, _ = _bindings(tmp_path, _ALL_TRIGGERS)
    agent = Agent(name="t", instructions="be helpful", model="gpt-4o-mini", tools=[do_thing])
    apply_reflexes(agent, bindings)

    in_names = [g.name for g in agent.input_guardrails]
    out_names = [g.name for g in agent.output_guardrails]
    assert in_names == ["reflex:on_input"], f"on_input guardrail missing: {in_names}"
    assert out_names == ["reflex:before_response"], f"before_response guardrail missing: {out_names}"

    # before_tool_call / after_tool_call are injected onto every tool.
    tool = agent.tools[0]
    ti = [g.name for g in (tool.tool_input_guardrails or [])]
    to = [g.name for g in (tool.tool_output_guardrails or [])]
    assert ti == ["reflex:before_tool_call"], f"tool input guardrail missing: {ti}"
    assert to == ["reflex:after_tool_call"], f"tool output guardrail missing: {to}"


def test_openai_agents_one_guardrail_batches_all_policies_on_a_trigger(tmp_path):
    """Three on_input policies must produce ONE guardrail (the batch seam), not
    three — otherwise the SDK fans out to one evaluator call per policy."""
    pytest.importorskip("agents")
    from agents import Agent
    from reflexes.integrations.openai_agents import apply_reflexes

    specs = [(f"p{i}", "on_input", "deny") for i in range(3)]
    bindings, _ = _bindings(tmp_path, specs)
    agent = Agent(name="t", instructions="x", model="gpt-4o-mini")
    apply_reflexes(agent, bindings)
    assert len(agent.input_guardrails) == 1, "batch seam broken: expected 1 guardrail for 3 policies"


# ---------------------------------------------------------------------------
# Claude Agent SDK — HookMatcher dict keyed by SDK event names.
# ---------------------------------------------------------------------------
def test_claude_agent_sdk_create_hooks_keys_match_configured_triggers(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import HookMatcher
    from reflexes.integrations.claude_agent_sdk import create_hooks

    _, cfg = _bindings(tmp_path, _ALL_TRIGGERS)
    hooks = create_hooks(config_file=cfg)

    # Trigger → SDK hook-event name mapping the SDK dispatches on.
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}, (
        f"hook event keys don't match configured triggers: {sorted(hooks)}"
    )
    for event, matchers in hooks.items():
        assert matchers and all(isinstance(m, HookMatcher) for m in matchers), (
            f"{event} is not a list of HookMatcher"
        )


def test_claude_agent_sdk_only_wires_configured_triggers(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from reflexes.integrations.claude_agent_sdk import create_hooks

    _, cfg = _bindings(tmp_path, [("only-input", "on_input", "deny")])
    hooks = create_hooks(config_file=cfg)
    assert set(hooks) == {"UserPromptSubmit"}, (
        f"only on_input configured, but wired: {sorted(hooks)}"
    )


# ---------------------------------------------------------------------------
# LangGraph — LangChain AgentMiddleware instance with the expected hook methods.
# ---------------------------------------------------------------------------
def test_langgraph_middleware_exposes_expected_hooks(tmp_path):
    pytest.importorskip("langchain.agents.factory")
    from langchain.agents.factory import AgentMiddleware
    from reflexes.integrations.langgraph import create_reflex_middleware

    bindings, _ = _bindings(tmp_path, _ALL_TRIGGERS)
    mw = create_reflex_middleware(bindings)

    assert isinstance(mw, AgentMiddleware), "middleware is not a LangChain AgentMiddleware"
    # The middleware must implement the agent-loop hook points LangGraph invokes.
    for hook in ("abefore_agent", "abefore_model", "aafter_model"):
        assert hasattr(mw, hook), f"middleware missing {hook} hook"
