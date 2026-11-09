"""LangGraph / LangChain integration for Reflexes.

Provides a ReflexMiddleware class that plugs into langchain's
create_agent(middleware=[...]) system.

Context enrichment: the middleware wraps the most-recent user message
in ``assemble_task_context(primary_user_prompt=...)`` so the evaluator
sees the same self-describing task-context framing that Claude
Code, OpenAI Agents SDK, and Google ADK produce. The standing-
instructions slot (e.g. system message in graph state) is deferred to
v0.2 — LangGraph's system-message location isn't standardized across
graph topologies, so we'd need either a user-supplied resolver or a
heuristic walk of the messages list. PRIMARY backbone is in parity now.

Trigger mapping:
  on_input         -> before_agent (checks last user message)
  before_response  -> after_model (checks LLM response)
  before_tool_call -> awrap_tool_call (checks tool name + args before execution)
  after_tool_call  -> awrap_tool_call (checks tool result after execution)

Actions:
  deny  -> raises ReflexDenied or returns a refusal message
  flag  -> merges reflex_flags into state (agent can read it)
  log   -> logs only
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

MAX_CONCURRENT = 10

from reflexes.core import (
    safe_acheck, safe_acheck_many, Verdict, ReflexDenied, build_feedback, EvaluatorError,
    format_upgrade_notice,
)
from reflexes.config import ReflexBinding
from reflexes._context import assemble_task_context

logger = logging.getLogger("reflexes.langgraph")


def _extract_last_user_message(state: dict) -> str | None:
    """Extract the last user message from agent state."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        # Handle both LangChain message objects and dicts
        if hasattr(msg, "type") and msg.type == "human":
            return msg.content
        if hasattr(msg, "role") and msg.role == "user":
            return msg.content
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg.get("content", "")
    return None


def _extract_last_ai_message(state: dict) -> str | None:
    """Extract the last AI message from agent state."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "ai":
            return msg.content if isinstance(msg.content, str) else str(msg.content)
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg.get("content", "")
    return None


def _extract_system_message(state: dict) -> str | None:
    """Best-effort standing instructions: the system message from graph state.

    LangGraph's system-message location isn't standardized across topologies —
    some put a SystemMessage in the messages list, others hold it in the model
    config outside graph state (which the middleware can't see). This reads a
    SystemMessage from the messages list when present; otherwise standing
    instructions degrade gracefully to absent (the current-request backbone is
    unaffected)."""
    messages = state.get("messages", []) if isinstance(state, dict) else []
    for msg in messages:
        if hasattr(msg, "type") and msg.type == "system":
            return msg.content if isinstance(msg.content, str) else str(msg.content)
        if hasattr(msg, "role") and msg.role == "system":
            content = msg.content
            return content if isinstance(content, str) else str(content)
        if isinstance(msg, dict) and msg.get("role") == "system":
            return msg.get("content")
    return None


async def _evaluate_bindings(
    bindings: list[ReflexBinding],
    content: str,
    context: str | None = None,
    check_type: str = "Content",
) -> list[tuple[ReflexBinding, Verdict]]:
    """Evaluate content against all bindings in parallel, return matches."""
    # One batched call with a System One evaluator; otherwise concurrent single
    # calls (safe_acheck_many handles both and preserves fail-open semantics).
    specs = [(b.policy.criteria, b.policy.name) for b in bindings]
    verdicts = await safe_acheck_many(
        specs, content, context=context, check_type=check_type,
    )
    results = list(zip(bindings, verdicts))
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


def _build_flag_entry(binding: ReflexBinding, verdict: Verdict) -> dict:
    """Build a flag metadata entry for state injection."""
    return {
        "reflex": binding.policy.name,
        "trigger": binding.trigger,
        "confidence": verdict.confidence,
    }


def create_reflex_middleware(bindings: list[ReflexBinding]):
    """Create a LangChain AgentMiddleware instance from reflex bindings.

    Args:
        bindings: List of ReflexBinding objects (from load_config).

    Returns:
        An AgentMiddleware instance that evaluates reflexes at the
        appropriate points in the agent loop.
    """
    from langchain.agents.factory import AgentMiddleware

    # Group bindings by trigger
    on_input = [b for b in bindings if b.trigger == "on_input"]
    before_response = [b for b in bindings if b.trigger == "before_response"]
    before_tool = [b for b in bindings if b.trigger == "before_tool_call"]
    after_tool = [b for b in bindings if b.trigger == "after_tool_call"]

    _reflex_retry_count = 0
    _MAX_REFLEX_RETRIES = 3
    # Surface the fail-open upgrade notice once per middleware instance
    # (≈ once per agent) rather than on every hook fire.
    _upgrade_surfaced = False

    def _upgrade_flags(upgrade) -> list:
        """Return a one-element reflex_flags list carrying the upgrade notice,
        or [] if there's nothing new to surface. Dedups via the closure flag."""
        nonlocal _upgrade_surfaced
        if upgrade is None or _upgrade_surfaced:
            return []
        _upgrade_surfaced = True
        return [{"reflex": "reflexes-upgrade", "notice": format_upgrade_notice(upgrade)}]

    class ReflexMiddleware(AgentMiddleware):

        async def abefore_agent(self, state, runtime):
            nonlocal _reflex_retry_count
            _reflex_retry_count = 0  # Reset on new agent invocation
            if not on_input:
                return None

            content = _extract_last_user_message(state)
            if not content:
                return None

            matches, upgrade = await _evaluate_bindings(on_input, content)
            flags = _upgrade_flags(upgrade)
            for binding, verdict in matches:
                if binding.action == "deny":
                    raise ReflexDenied(binding.policy.name, verdict.confidence, binding.policy.criteria)
                elif binding.action == "flag":
                    logger.warning(
                        "Reflex %s flagged (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
                    flags.append(_build_flag_entry(binding, verdict))
                elif binding.action == "log":
                    logger.warning(
                        "Reflex %s matched (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )

            if flags:
                return {"reflex_flags": flags}
            return None

        # NOTE (hardening): the deny path below returns {"jump_to": "model"} to
        # re-run the model (self-correction). On langchain 1.3.x this resolves
        # in both tools and no-tools graphs (verified live), but the
        # jump target isn't declared. If a future langchain requires it, add
        # `@hook_config(can_jump_to=["model"])` to this method to make the
        # "model" destination explicit and future-proof the revise loop.
        async def aafter_model(self, state, runtime):
            if not before_response:
                return None

            content = _extract_last_ai_message(state)
            if not content:
                return None

            user_context = assemble_task_context(
                primary_user_prompt=_extract_last_user_message(state),
                standing_instructions=_extract_system_message(state),
            ) or None
            matches, upgrade = await _evaluate_bindings(
                before_response, content,
                context=user_context, check_type="Agent response",
            )
            flags = _upgrade_flags(upgrade)
            for binding, verdict in matches:
                if binding.action == "deny":
                    nonlocal _reflex_retry_count
                    _reflex_retry_count += 1
                    if _reflex_retry_count > _MAX_REFLEX_RETRIES:
                        logger.warning(
                            "Reflex %s denied after %d retries, raising",
                            binding.policy.name, _MAX_REFLEX_RETRIES,
                        )
                        raise ReflexDenied(binding.policy.name, verdict.confidence, binding.policy.criteria)
                    from langchain_core.messages import HumanMessage
                    feedback = build_feedback("before_response", binding.policy.criteria)
                    logger.warning(
                        "Reflex %s denied (confidence=%.2f), retry %d/%d",
                        binding.policy.name, verdict.confidence,
                        _reflex_retry_count, _MAX_REFLEX_RETRIES,
                    )
                    return {"messages": [HumanMessage(content=feedback)], "jump_to": "model"}
                elif binding.action == "flag":
                    logger.warning(
                        "Reflex %s flagged (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
                    flags.append(_build_flag_entry(binding, verdict))
                elif binding.action == "log":
                    logger.warning(
                        "Reflex %s matched (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )

            if flags:
                return {"reflex_flags": flags}
            return None

        async def awrap_tool_call(self, request, handler):
            # Get user context from state if available, then assemble
            # through the shared PRIMARY/BACKGROUND framer so this matches
            # Claude Code / OpenAI Agents / Google ADK exactly.
            user_context = None
            if hasattr(request, "state"):
                user_context = assemble_task_context(
                    primary_user_prompt=_extract_last_user_message(request.state),
                    standing_instructions=_extract_system_message(request.state),
                ) or None

            # before_tool_call: check tool name + args. Content shape is
            # the JSON-object payload locked by the SDK Protocol
            # reconciliation — `{"tool_name": ..., "args": ...}`,
            # not the legacy `tool_name(args)` call-style string. Same
            # shape that claude_code.py and claude_agent_sdk.py emit.
            if before_tool:
                tool_name = request.tool_call.get("name", "")
                tool_args = request.tool_call.get("args", {})
                content = json.dumps({"tool_name": tool_name, "args": tool_args})

                matches, upgrade = await _evaluate_bindings(
                    before_tool, content,
                    context=user_context, check_type="Tool call",
                )
                if _upgrade_flags(upgrade):
                    logger.warning("Reflexes fail-open (upgrade required): %s", format_upgrade_notice(upgrade))
                for binding, verdict in matches:
                    if binding.action == "deny":
                        from langchain_core.messages import ToolMessage
                        return ToolMessage(
                            content=build_feedback("before_tool_call", binding.policy.criteria),
                            tool_call_id=request.tool_call.get("id", ""),
                        )
                    elif binding.action == "flag":
                        logger.warning(
                            "Reflex %s flagged tool call %s (confidence=%.2f)",
                            binding.policy.name, tool_name, verdict.confidence,
                        )
                    elif binding.action == "log":
                        logger.warning(
                            "Reflex %s matched tool call %s (confidence=%.2f)",
                            binding.policy.name, tool_name, verdict.confidence,
                        )

            # Execute the tool
            result = await handler(request)

            # after_tool_call: check tool result
            if after_tool and hasattr(result, "content"):
                tool_name = request.tool_call.get("name", "")
                content = f"{tool_name} returned: {result.content}"

                matches, upgrade = await _evaluate_bindings(
                    after_tool, content,
                    context=user_context, check_type="Tool result",
                )
                if _upgrade_flags(upgrade):
                    logger.warning("Reflexes fail-open (upgrade required): %s", format_upgrade_notice(upgrade))
                for binding, verdict in matches:
                    if binding.action == "deny":
                        from langchain_core.messages import ToolMessage
                        return ToolMessage(
                            content=build_feedback("after_tool_call", binding.policy.criteria),
                            tool_call_id=request.tool_call.get("id", ""),
                        )
                    elif binding.action == "flag":
                        logger.warning(
                            "Reflex %s flagged tool output from %s (confidence=%.2f)",
                            binding.policy.name, tool_name, verdict.confidence,
                        )
                    elif binding.action == "log":
                        logger.warning(
                            "Reflex %s matched tool output from %s (confidence=%.2f)",
                            binding.policy.name, tool_name, verdict.confidence,
                        )

            return result

    return ReflexMiddleware()
