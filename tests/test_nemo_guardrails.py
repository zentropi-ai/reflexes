"""NeMo Guardrails integration — the actions must actually RUN the checks.

Regression for the bug live-testing found: NeMo v1.0 only auto-populates action
params named `context` / `events` / `__context__*`, so plain `bot_message` /
`user_message` params stayed empty and the check silently short-circuited
(`if not bot_message: return True`) — the policy was never evaluated. The
actions now take the rails `context` dict and read the vars from it.
"""
import asyncio
from types import SimpleNamespace

import reflexes.integrations.nemo_guardrails as nemo
from reflexes.core import Verdict


class _FakeRails:
    def __init__(self):
        self.actions = {}

    def register_action(self, fn, name):
        self.actions[name] = fn


def _wire(monkeypatch, bindings, safe_acheck):
    monkeypatch.setattr(nemo, "configure_from_file", lambda *a, **k: None)
    monkeypatch.setattr(nemo, "load_config", lambda **k: bindings)
    monkeypatch.setattr(nemo, "safe_acheck", safe_acheck)
    rails = _FakeRails()
    nemo.register_reflex_actions(rails, config_file="x.yaml")
    return rails


def _binding(trigger, action="deny"):
    return SimpleNamespace(trigger=trigger, action=action,
                           policy=SimpleNamespace(name="P", criteria="crit"))


def test_output_action_reads_bot_message_from_context(monkeypatch):
    seen = {}
    async def _stub(policy, policy_name, content, context=None, check_type=None):
        seen["content"] = content
        seen["check_type"] = check_type
        return Verdict(label=0, confidence=0.9)  # safe
    rails = _wire(monkeypatch, [_binding("before_response")], _stub)

    result = asyncio.run(rails.actions["reflex_check_output"](
        context={"bot_message": "THE ANSWER", "user_message": "hi"}))
    assert result is True  # label 0 → allowed
    assert seen.get("content") == "THE ANSWER", "the policy actually ran on the bot message from context"
    assert seen.get("check_type") == "Agent response"


def test_output_action_deny_blocks(monkeypatch):
    async def _match(policy, policy_name, content, context=None, check_type=None):
        return Verdict(label=1, confidence=0.99)  # matches → deny
    rails = _wire(monkeypatch, [_binding("before_response")], _match)
    result = asyncio.run(rails.actions["reflex_check_output"](
        context={"bot_message": "bad", "user_message": "hi"}))
    assert result is False, "deny → not safe → NeMo refuses"


def test_input_action_reads_user_message_from_context(monkeypatch):
    seen = {}
    async def _stub(policy, policy_name, content, context=None, check_type=None):
        seen["content"] = content
        return Verdict(label=0, confidence=0.9)
    rails = _wire(monkeypatch, [_binding("on_input")], _stub)
    result = asyncio.run(rails.actions["reflex_check_input"](context={"user_message": "hello there"}))
    assert result is True
    assert seen.get("content") == "hello there"


def test_explicit_arg_still_works_as_fallback(monkeypatch):
    seen = {}
    async def _stub(policy, policy_name, content, context=None, check_type=None):
        seen["content"] = content
        return Verdict(label=0, confidence=0.9)
    rails = _wire(monkeypatch, [_binding("before_response")], _stub)
    # No context dict, but an explicit bot_message arg (Colang author passed it).
    result = asyncio.run(rails.actions["reflex_check_output"](bot_message="EXPLICIT"))
    assert result is True
    assert seen.get("content") == "EXPLICIT"


def test_no_content_short_circuits_without_calling_evaluator(monkeypatch):
    called = {"n": 0}
    async def _stub(*a, **k):
        called["n"] += 1
        return Verdict(label=0, confidence=0.9)
    rails = _wire(monkeypatch, [_binding("before_response")], _stub)
    # Neither context nor an explicit arg → nothing to check → allow, no eval.
    result = asyncio.run(rails.actions["reflex_check_output"]())
    assert result is True
    assert called["n"] == 0


def test_inline_check_fails_open_on_evaluator_error(monkeypatch):
    from reflexes.core import EvaluatorError, ReflexesUpgradeRequired
    up = ReflexesUpgradeRequired(error="rate_limit_exceeded", tier=0, reason="x",
                                 message="rl", upgrade_url="u", upgrade_action="s")
    async def _err(policy, policy_name, content, **k):
        return EvaluatorError(policy_name="inline", error="boom", upgrade=up)
    rails = _wire(monkeypatch, [], _err)
    # reflex_check takes explicit policy+content; an evaluator failure must
    # fail open (return True), not raise (regression: it referenced removed
    # helpers on this branch).
    result = asyncio.run(rails.actions["reflex_check"](policy="p", content="c"))
    assert result is True
