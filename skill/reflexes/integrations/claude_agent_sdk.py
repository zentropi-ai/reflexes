"""Anthropic Claude Agent SDK integration for Reflexes.

Provides functions that return hooks dict for use with ClaudeAgentOptions.
Uses the SDK's in-process Python async callbacks (not CLI shell hooks).

Context enrichment (SPEC §5.1): the hook payload the CLI sends is the raw
hook JSON, which carries a ``session_id`` and (on UserPromptSubmit) the
user's ``prompt``. All hook callbacks in one ``create_hooks()`` call share a
closure, so we capture the user's most-recent + initial prompt per session
(``_SessionMemory``) on UserPromptSubmit and feed it back as "Current
request:" / "Original task:" task context to the tool-call and response
checks. Standing instructions live in ClaudeAgentOptions' system prompt,
outside the hook payload, so callers who want them in the evaluator's view
pass ``system_prompt=`` to ``create_hooks()`` (mirror it from their
ClaudeAgentOptions). All three feed reflexes._context.assemble_task_context.

Trigger mapping:
  on_input         -> UserPromptSubmit hook
  before_tool_call -> PreToolUse hook
  after_tool_call  -> PostToolUse hook
  before_response  -> Stop hook (reads last_assistant_message)

Actions:
  deny  -> block prompt (decision: block) or deny tool (permissionDecision: deny)
  flag  -> inject additionalContext so the agent is aware
  log   -> log only

Usage:
    from claude_agent_sdk import query, ClaudeAgentOptions
    from reflexes.integrations.claude_agent_sdk import create_hooks

    hooks = create_hooks(reflexes=["safety", "tool-checks"], config_file="./reflexes.yaml")

    async for msg in query(
        prompt="...",
        options=ClaudeAgentOptions(hooks=hooks),
    ):
        print(msg)
"""

from __future__ import annotations

import asyncio
import json
import logging

MAX_CONCURRENT = 10

from reflexes.core import (
    safe_acheck, build_feedback, EvaluatorError,
    format_upgrade_notice, should_surface_upgrade,
)
from reflexes.config import load_config, ReflexBinding, configure_from_file
from reflexes._context import assemble_task_context

logger = logging.getLogger("reflexes.claude_agent_sdk")

TRIGGER_TO_HOOK = {
    "on_input": "UserPromptSubmit",
    "before_tool_call": "PreToolUse",
    "after_tool_call": "PostToolUse",
}


class _SessionMemory:
    """Per-session capture of the user's most-recent + initial prompt, shared
    across the hook callbacks created in one ``create_hooks()`` call.

    UserPromptSubmit records the prompt; the tool-call and response hooks read
    it back as task context (SPEC §5.1) so the evaluator sees "Current request:"
    / "Original task:" alongside the tool call or response it is judging. Keyed
    by the payload's ``session_id`` (falls back to a shared key when absent, so
    single-session use still works). Bounded so a long-lived process that serves
    many sessions doesn't grow without limit.
    """

    _MAX_SESSIONS = 128

    def __init__(self, standing_instructions: str | None = None):
        self._standing = (standing_instructions or "").strip() or None
        self._by_session: dict[str, dict[str, str]] = {}

    def record_prompt(self, session_id: str, prompt: str) -> None:
        if not prompt:
            return
        slot = self._by_session.get(session_id)
        if slot is None:
            if len(self._by_session) >= self._MAX_SESSIONS:
                self._by_session.pop(next(iter(self._by_session)))  # drop oldest
            self._by_session[session_id] = {"initial": prompt, "current": prompt}
        else:
            slot["current"] = prompt

    def context_for(self, session_id: str) -> str | None:
        slot = self._by_session.get(session_id) or {}
        return assemble_task_context(
            primary_user_prompt=slot.get("current"),
            initial_user_prompt=slot.get("initial"),
            standing_instructions=self._standing,
        ) or None


async def _evaluate_parallel(bindings: list[ReflexBinding], content: str,
                              context: str | None = None, check_type: str = "Content"):
    """Evaluate all bindings in parallel. Returns ``(matches, upgrade)`` where
    matches is a list of (binding, verdict) and upgrade is the first
    tier/capability upgrade condition seen (or None) — so the caller can surface
    the fail-open note into the agent's context."""
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _eval_one(binding):
        async with sem:
            verdict = await safe_acheck(
                policy=binding.policy.criteria, policy_name=binding.policy.name,
                content=content, context=context, check_type=check_type,
            )
            return binding, verdict

    results = await asyncio.gather(*[_eval_one(b) for b in bindings])

    matches = []
    upgrade = None
    for binding, verdict in results:
        if isinstance(verdict, EvaluatorError):
            if upgrade is None and verdict.upgrade is not None:
                upgrade = verdict.upgrade
            logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
            continue
        if verdict.label == 1:
            matches.append((binding, verdict))
    return matches, upgrade


# Claude Agent SDK hook payloads carry no session_id (see module note above),
# so the upgrade notice dedups once per process via a fixed key. When the SDK
# exposes a session id, swap the key for a per-session one.
def _upgrade_output(hook_event: str, upgrade) -> dict:
    """Return a hookSpecificOutput dict injecting the fail-open upgrade note
    into the agent's context, or {} if there's nothing to surface (no upgrade,
    or already surfaced this process)."""
    if upgrade is None or not should_surface_upgrade("claude_agent_sdk"):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": hook_event,
            "additionalContext": format_upgrade_notice(upgrade),
        }
    }


def _make_user_prompt_hook(bindings: list[ReflexBinding], memory: _SessionMemory):
    """Create a UserPromptSubmit hook callback."""
    async def _hook(input_data, tool_use_id, context):
        prompt = input_data.get("prompt", "")
        # Capture the prompt for downstream tool-call/response task context,
        # even when there are no on_input bindings to evaluate here.
        memory.record_prompt(input_data.get("session_id", ""), prompt)
        if not prompt:
            return {}

        # on_input evaluates the prompt itself; the prompt IS the current
        # request, so task context would only echo it back — pass None.
        matches, upgrade = await _evaluate_parallel(bindings, prompt)
        for binding, verdict in matches:
                if binding.action == "deny":
                    return {
                        "decision": "block",
                        "reason": build_feedback("on_input", binding.policy.criteria),
                    }
                elif binding.action == "flag":
                    logger.warning("Reflex %s flagged (confidence=%.2f)", binding.policy.name, verdict.confidence)
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": f"[Reflex: {binding.policy.name} flagged (confidence: {verdict.confidence:.2f})]",
                        }
                    }
                elif binding.action == "log":
                    logger.warning("Reflex %s matched (confidence=%.2f)", binding.policy.name, verdict.confidence)
        return _upgrade_output("UserPromptSubmit", upgrade)

    return _hook


def _make_pre_tool_use_hook(bindings: list[ReflexBinding], memory: _SessionMemory):
    """Create a PreToolUse hook callback."""
    async def _hook(input_data, tool_use_id, context):
        # Per SPEC §3.3: emit JSON object payload so structural evaluators
        # can dispatch on fields without re-parsing a call-style string.
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        content = json.dumps({"tool_name": tool_name, "args": tool_input})
        task_context = memory.context_for(input_data.get("session_id", ""))

        matches, upgrade = await _evaluate_parallel(bindings, content, context=task_context, check_type="Tool call")
        for binding, verdict in matches:
                if binding.action == "deny":
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": build_feedback("before_tool_call", binding.policy.criteria),
                        }
                    }
                elif binding.action == "flag":
                    logger.warning("Reflex %s flagged tool %s (confidence=%.2f)", binding.policy.name, tool_name, verdict.confidence)
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "additionalContext": f"[Reflex: {binding.policy.name} flagged (confidence: {verdict.confidence:.2f})]",
                        }
                    }
                elif binding.action == "log":
                    logger.warning("Reflex %s matched tool %s (confidence=%.2f)", binding.policy.name, tool_name, verdict.confidence)
        return _upgrade_output("PreToolUse", upgrade)

    return _hook


def _make_post_tool_use_hook(bindings: list[ReflexBinding], memory: _SessionMemory):
    """Create a PostToolUse hook callback."""
    async def _hook(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "")
        tool_response = input_data.get("tool_response", "")
        if isinstance(tool_response, dict):
            tool_response = json.dumps(tool_response)
        content = f"{tool_name} returned: {tool_response}"
        task_context = memory.context_for(input_data.get("session_id", ""))

        matches, upgrade = await _evaluate_parallel(bindings, content, context=task_context, check_type="Tool result")
        for binding, verdict in matches:
            if binding.action == "deny":
                return {
                    "decision": "block",
                    "reason": build_feedback("after_tool_call", binding.policy.criteria),
                }
            elif binding.action == "flag":
                logger.warning("Reflex %s flagged tool output from %s (confidence=%.2f)", binding.policy.name, tool_name, verdict.confidence)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": f"[Reflex: {binding.policy.name} flagged (confidence: {verdict.confidence:.2f})]",
                    }
                }
            elif binding.action == "log":
                logger.warning("Reflex %s matched tool output from %s (confidence=%.2f)", binding.policy.name, tool_name, verdict.confidence)
        return _upgrade_output("PostToolUse", upgrade)

    return _hook


def _make_stop_hook(bindings: list[ReflexBinding], memory: _SessionMemory):
    """Create a Stop hook callback for before_response bindings.

    Note: Claude streams responses — the user has already seen the
    response by the time Stop fires. For all actions (deny, flag, log),
    we inject context informing the agent. What happens next is up to
    the agent designer.
    """
    async def _hook(input_data, tool_use_id, context):
        content = input_data.get("last_assistant_message", "")
        if not content:
            return {}
        task_context = memory.context_for(input_data.get("session_id", ""))

        matches, upgrade = await _evaluate_parallel(bindings, content, context=task_context, check_type="Agent response")
        for binding, verdict in matches:
            context_msg = f"[Reflex: {binding.policy.name} flagged (confidence: {verdict.confidence:.2f})]"
            if binding.action in ("deny", "flag"):
                logger.warning("Reflex %s matched response (confidence=%.2f)", binding.policy.name, verdict.confidence)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": context_msg,
                    }
                }
            elif binding.action == "log":
                logger.warning("Reflex %s matched response (confidence=%.2f)", binding.policy.name, verdict.confidence)
        return _upgrade_output("Stop", upgrade)

    return _hook


def create_hooks(
    reflexes: list[str] | None = None,
    *,
    config_file: str = "reflexes.yaml",
    system_prompt: str | None = None,
) -> dict:
    """Create a hooks dict for ClaudeAgentOptions from reflexes config.

    Usage:
        from claude_agent_sdk import ClaudeAgentOptions
        from reflexes.integrations.claude_agent_sdk import create_hooks

        options = ClaudeAgentOptions(
            hooks=create_hooks(reflexes=["safety", "tool-checks"]),
        )

    Args:
        reflexes: Config group names from reflexes.yaml. 'default' is always included.
        config_file: Path to reflexes.yaml.
        system_prompt: Optional standing instructions to include in the task
            context the evaluator sees for tool-call and response checks (SPEC
            §5.1). The SDK keeps the system prompt in ClaudeAgentOptions, outside
            the hook payload, so mirror it here if you want it evaluator-visible.
            The user's most-recent + initial prompt are captured automatically.

    Returns:
        Dict mapping hook event names to lists of HookMatcher objects.
    """
    from claude_agent_sdk import HookMatcher

    configure_from_file(config_file)

    reflexes = reflexes or []
    bindings = load_config(reflexes=reflexes, config_file=config_file)

    # Shared across all hook callbacks so UserPromptSubmit's capture reaches
    # the tool-call/response checks as task context (SPEC §5.1).
    memory = _SessionMemory(standing_instructions=system_prompt)

    # Group bindings by trigger
    by_trigger: dict[str, list[ReflexBinding]] = {}
    for binding in bindings:
        by_trigger.setdefault(binding.trigger, []).append(binding)

    hooks = {}

    # Always install a UserPromptSubmit hook so the prompt is captured for
    # downstream task context, even when no on_input bindings exist.
    hooks["UserPromptSubmit"] = [
        HookMatcher(hooks=[_make_user_prompt_hook(by_trigger.get("on_input", []), memory)])
    ]

    if "before_tool_call" in by_trigger:
        hooks["PreToolUse"] = [
            HookMatcher(hooks=[_make_pre_tool_use_hook(by_trigger["before_tool_call"], memory)])
        ]

    if "after_tool_call" in by_trigger:
        hooks["PostToolUse"] = [
            HookMatcher(hooks=[_make_post_tool_use_hook(by_trigger["after_tool_call"], memory)])
        ]

    if "before_response" in by_trigger:
        deny_bindings = [b for b in by_trigger["before_response"] if b.action == "deny"]
        if deny_bindings:
            names = ", ".join(b.policy.name for b in deny_bindings)
            logger.warning(
                "Action 'deny' on before_response in Claude Agent SDK cannot retract "
                "a streamed response. Will inject context instead of blocking. "
                "Affected: %s", names,
            )
        hooks["Stop"] = [
            HookMatcher(hooks=[_make_stop_hook(by_trigger["before_response"], memory)])
        ]

    return hooks
