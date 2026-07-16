"""Microsoft AutoGen integration for Reflexes.

Provides a ReflexInterventionHandler that plugs into AutoGen's runtime
via the intervention_handlers parameter.

Trigger mapping:
  on_input         -> on_send (checks user-sourced messages)
  before_response  -> on_response (checks agent responses)
  before_tool_call -> on_send (checks FunctionCall messages)
  after_tool_call  -> on_send (checks FunctionExecutionResult messages)

Actions:
  deny  -> returns DropMessage (message is silently dropped)
  flag  -> log only (AutoGen has no state injection mechanism)
  log   -> log only

Usage:
    from autogen_core import SingleThreadedAgentRuntime
    from reflexes.integrations.autogen import create_intervention_handler

    handler = create_intervention_handler(
        reflexes=["safety", "tool-checks"],
        config_file="./reflexes.yaml",
    )
    runtime = SingleThreadedAgentRuntime(intervention_handlers=[handler])
"""

from __future__ import annotations

import asyncio
import json
import logging

from reflexes.core import safe_acheck, build_feedback, EvaluatorError
from reflexes._context import assemble_task_context

MAX_CONCURRENT = 10
from reflexes.config import load_config, ReflexBinding, configure_from_file

logger = logging.getLogger("reflexes.autogen")


def _extract_content(message) -> tuple[str | None, str]:
    """Extract text content from a message and identify its type.

    Returns (content, message_type) where message_type is one of:
    'user_input', 'agent_response', 'tool_call', 'tool_result', 'unknown'
    """
    msg_type_name = type(message).__name__

    # agentchat-level user messages
    if msg_type_name == "UserMessage":
        content = message.content if isinstance(message.content, str) else str(message.content)
        return content, "user_input"

    # agentchat text messages — check source to determine direction
    if msg_type_name == "TextMessage":
        return message.content, "agent_response"

    # Tool call summary (agent produced tool calls + results)
    if msg_type_name == "ToolCallSummaryMessage":
        parts = []
        for tc, res in zip(message.tool_calls, message.results):
            parts.append(f"{tc.name}({tc.arguments}) returned: {res.content}")
        return " | ".join(parts), "tool_result"

    # Core-level messages
    if hasattr(message, "content"):
        content = message.content if isinstance(message.content, str) else str(message.content)

        # FunctionCall detection
        if msg_type_name == "FunctionCall" or hasattr(message, "arguments"):
            name = getattr(message, "name", "unknown")
            args = getattr(message, "arguments", "")
            return f"{name}({args})", "tool_call"

        # FunctionExecutionResult detection
        if msg_type_name == "FunctionExecutionResult" or hasattr(message, "call_id"):
            name = getattr(message, "name", "unknown")
            return f"{name} returned: {content}", "tool_result"

        return content, "unknown"

    # Fallback
    return str(message), "unknown"


# Map message types to triggers
_TYPE_TO_TRIGGER = {
    "user_input": "on_input",
    "agent_response": "before_response",
    "tool_call": "before_tool_call",
    "tool_result": "after_tool_call",
}


def create_intervention_handler(
    reflexes: list[str] | None = None,
    *,
    config_file: str = "reflexes.yaml",
):
    """Create an AutoGen InterventionHandler from reflexes config.

    Usage:
        from autogen_core import SingleThreadedAgentRuntime
        from reflexes.integrations.autogen import create_intervention_handler

        handler = create_intervention_handler(reflexes=["safety"])
        runtime = SingleThreadedAgentRuntime(intervention_handlers=[handler])

    Args:
        reflexes: Config group names. 'default' is always included.
        config_file: Path to reflexes.yaml.

    Returns:
        A DefaultInterventionHandler subclass instance.
    """
    from autogen_core import DefaultInterventionHandler, DropMessage

    configure_from_file(config_file)

    reflexes = reflexes or []
    bindings = load_config(reflexes=reflexes, config_file=config_file)

    # Group bindings by trigger
    by_trigger: dict[str, list[ReflexBinding]] = {}
    for binding in bindings:
        by_trigger.setdefault(binding.trigger, []).append(binding)

    flag_bindings = [b for b in bindings if b.action == "flag"]
    if flag_bindings:
        names = ", ".join(b.policy.name for b in flag_bindings)
        logger.warning(
            "Action 'flag' on AutoGen degrades to log-only "
            "(no state injection mechanism). Affected: %s", names,
        )

    async def _evaluate(content: str, trigger_bindings: list[ReflexBinding],
                        context: str | None = None, check_type: str | None = None):
        """Evaluate content against bindings in parallel. Returns DropMessage class if denied, else None."""
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def _eval_one(binding):
            async with sem:
                verdict = await safe_acheck(policy=binding.policy.criteria, policy_name=binding.policy.name, content=content,
                                       context=context, check_type=check_type or "Content")
                return binding, verdict

        results = await asyncio.gather(*[_eval_one(b) for b in trigger_bindings])

        for binding, verdict in results:
            if isinstance(verdict, EvaluatorError):
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                continue
            if verdict.label == 1:
                if binding.action == "deny":
                    logger.warning(
                        "Reflex %s denied (confidence=%.2f). AutoGen DropMessage "
                        "does not support self-correction feedback.",
                        binding.policy.name, verdict.confidence,
                    )
                    return DropMessage
                elif binding.action == "flag":
                    logger.warning(
                        "Reflex %s flagged (confidence=%.2f): %s",
                        binding.policy.name, verdict.confidence, content[:80],
                    )
                elif binding.action == "log":
                    logger.warning(
                        "Reflex %s matched (confidence=%.2f): %s",
                        binding.policy.name, verdict.confidence, content[:80],
                    )
        return None

    class ReflexInterventionHandler(DefaultInterventionHandler):
        _last_user_input: str | None = None

        async def on_send(self, message, *, message_context, recipient):
            content, msg_type = _extract_content(message)
            if content:
                # Track the last user input for context
                if msg_type == "user_input":
                    self._last_user_input = content

                trigger = _TYPE_TO_TRIGGER.get(msg_type)
                if trigger and trigger in by_trigger:
                    # Frame the originating request as "Current request:" task
                    # context (SPEC §5.1); no standing-instructions channel at
                    # the runtime intervention layer.
                    raw = self._last_user_input if msg_type != "user_input" else None
                    ctx = assemble_task_context(primary_user_prompt=raw) or None
                    ct = {"tool_call": "Tool call", "tool_result": "Tool result"}.get(msg_type)
                    result = await _evaluate(content, by_trigger[trigger], context=ctx, check_type=ct)
                    if result is DropMessage:
                        return DropMessage

                # Also check on_input for unknown messages (best effort)
                if msg_type == "unknown" and "on_input" in by_trigger:
                    result = await _evaluate(content, by_trigger["on_input"])
                    if result is DropMessage:
                        return DropMessage

            return message

        async def on_response(self, message, *, sender, recipient):
            content, msg_type = _extract_content(message)
            if content and "before_response" in by_trigger:
                ctx = assemble_task_context(primary_user_prompt=self._last_user_input) or None
                result = await _evaluate(content, by_trigger["before_response"],
                                         context=ctx, check_type="Agent response")
                if result is DropMessage:
                    return DropMessage
            return message

    return ReflexInterventionHandler()
