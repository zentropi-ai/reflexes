"""Google ADK integration for Reflexes.

Maps reflex triggers to ADK agent-level callbacks:
  on_input         -> before_model_callback (checks user message from request)
  before_response  -> after_model_callback (checks LLM response)
  before_tool_call -> before_tool_callback (checks tool name + args)
  after_tool_call  -> after_tool_callback (checks tool result)

Actions:
  deny  -> returns a short-circuit response / replacement result
  flag  -> writes to context.state["reflex_flags"] (agent can read live)
  log   -> logs only

Flag behavior: ADK has the cleanest flag story — context.state is a mutable
dict shared across the agent's lifecycle. Flag writes structured metadata
that the agent can read mid-run.

Note: ADK calls callbacks with keyword arguments (callback_context=,
llm_request=, etc.), not positional args.
"""

from __future__ import annotations

import json
import logging

from reflexes.core import (
    safe_acheck, Verdict, build_feedback, EvaluatorError, format_upgrade_notice,
)
from reflexes.config import ReflexBinding

logger = logging.getLogger("reflexes.adk")


def _surface_upgrade(ctx, upgrade) -> None:
    """Append the fail-open upgrade notice to context.state once per session.

    ADK's context.state is a mutable dict shared across the agent's lifecycle,
    so a state marker dedups the notice to once per session (the agent can read
    it from reflex_flags mid-run). No-op when there's no upgrade condition."""
    if upgrade is None or ctx.state.get("_reflex_upgrade_surfaced"):
        return
    ctx.state["_reflex_upgrade_surfaced"] = True
    flags = ctx.state.get("reflex_flags", [])
    flags.append({"reflex": "reflexes-upgrade", "notice": format_upgrade_notice(upgrade)})
    ctx.state["reflex_flags"] = flags


def _build_flag_entry(binding: ReflexBinding, verdict: Verdict) -> dict:
    return {
        "reflex": binding.policy.name,
        "trigger": binding.trigger,
        "confidence": verdict.confidence,
    }


def _add_flag_to_state(ctx, entry: dict):
    """Append a flag entry to context.state['reflex_flags']."""
    flags = ctx.state.get("reflex_flags", [])
    flags.append(entry)
    ctx.state["reflex_flags"] = flags


def _make_before_model_callbacks(bindings: list[ReflexBinding]) -> list:
    """Create before_model_callback functions for on_input bindings."""
    callbacks = []

    for binding in bindings:
        async def _callback(*, callback_context, llm_request, _binding=binding):
            content = None
            if llm_request.contents:
                for c in reversed(llm_request.contents):
                    if c.role == "user" and c.parts:
                        content = c.parts[0].text if hasattr(c.parts[0], "text") else str(c.parts[0])
                        break
            if not content:
                return None

            verdict = await safe_acheck(policy=_binding.policy.criteria, policy_name=_binding.policy.name, content=content)
            if isinstance(verdict, EvaluatorError):
                _surface_upgrade(callback_context, verdict.upgrade)
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                return None
            if verdict.label == 1:
                if _binding.action == "deny":
                    from google.adk.models.llm_response import LlmResponse
                    from google.genai import types
                    return LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text=build_feedback("on_input", _binding.policy.criteria))],
                        )
                    )
                elif _binding.action == "flag":
                    logger.warning("Reflex %s flagged (confidence=%.2f)", _binding.policy.name, verdict.confidence)
                    _add_flag_to_state(callback_context, _build_flag_entry(_binding, verdict))
                elif _binding.action == "log":
                    logger.info("Reflex %s matched (confidence=%.2f)", _binding.policy.name, verdict.confidence)
            return None

        callbacks.append(_callback)

    return callbacks


def _build_task_context(*, recent_input: str | None, agent_instruction: str | None) -> tuple[str | None, str]:
    """Compose PRIMARY/BACKGROUND task-context using the shared assembler."""
    from reflexes._context import assemble_task_context

    composed = assemble_task_context(
        primary_user_prompt=recent_input,
        standing_instructions=agent_instruction,
    )
    if composed:
        return composed, "Task context"
    return None, "User request"


def _make_after_model_callbacks(bindings: list[ReflexBinding], agent_instruction: str | None = None) -> list:
    """Create after_model_callback functions for before_response bindings."""
    callbacks = []

    for binding in bindings:
        async def _callback(*, callback_context, llm_response, _binding=binding):
            content = None
            if llm_response.content and llm_response.content.parts:
                content = llm_response.content.parts[0].text if hasattr(llm_response.content.parts[0], "text") else str(llm_response.content.parts[0])
            if not content:
                return None

            recent_input = None
            if hasattr(callback_context, "user_content") and callback_context.user_content:
                uc = callback_context.user_content
                if hasattr(uc, "parts") and uc.parts:
                    recent_input = uc.parts[0].text if hasattr(uc.parts[0], "text") else str(uc.parts[0])

            task_context, context_label = _build_task_context(
                recent_input=recent_input, agent_instruction=agent_instruction,
            )
            verdict = await safe_acheck(
                policy=_binding.policy.criteria, policy_name=_binding.policy.name, content=content,
                context=task_context, check_type="Agent response", context_label=context_label,
            )
            if isinstance(verdict, EvaluatorError):
                _surface_upgrade(callback_context, verdict.upgrade)
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                return None
            if verdict.label == 1:
                if _binding.action == "deny":
                    from google.adk.models.llm_response import LlmResponse
                    from google.genai import types
                    feedback = build_feedback("before_response", _binding.policy.criteria)
                    logger.warning(
                        "Reflex %s denied (confidence=%.2f). ADK after_model_callback "
                        "cannot force a retry — returning feedback as response.",
                        _binding.policy.name, verdict.confidence,
                    )
                    return LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text=feedback)],
                        )
                    )
                elif _binding.action == "flag":
                    logger.warning("Reflex %s flagged (confidence=%.2f)", _binding.policy.name, verdict.confidence)
                    _add_flag_to_state(callback_context, _build_flag_entry(_binding, verdict))
                elif _binding.action == "log":
                    logger.info("Reflex %s matched (confidence=%.2f)", _binding.policy.name, verdict.confidence)
            return None

        callbacks.append(_callback)

    return callbacks


def _make_before_tool_callbacks(bindings: list[ReflexBinding], agent_instruction: str | None = None) -> list:
    """Create before_tool_callback functions for before_tool_call bindings."""
    callbacks = []

    for binding in bindings:
        async def _callback(*, tool, args, tool_context, _binding=binding):
            tool_name = getattr(tool, "name", str(tool))
            content = f"{tool_name}({json.dumps(args)})"

            recent_input = None
            if hasattr(tool_context, "user_content") and tool_context.user_content:
                uc = tool_context.user_content
                if hasattr(uc, "parts") and uc.parts:
                    recent_input = uc.parts[0].text if hasattr(uc.parts[0], "text") else str(uc.parts[0])

            task_context, context_label = _build_task_context(
                recent_input=recent_input, agent_instruction=agent_instruction,
            )
            verdict = await safe_acheck(
                policy=_binding.policy.criteria, policy_name=_binding.policy.name, content=content,
                context=task_context, check_type="Tool call", context_label=context_label,
            )
            if isinstance(verdict, EvaluatorError):
                _surface_upgrade(tool_context, verdict.upgrade)
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                return None
            if verdict.label == 1:
                if _binding.action == "deny":
                    return {"error": build_feedback("before_tool_call", _binding.policy.criteria)}
                elif _binding.action == "flag":
                    logger.warning("Reflex %s flagged tool %s (confidence=%.2f)", _binding.policy.name, tool_name, verdict.confidence)
                    _add_flag_to_state(tool_context, _build_flag_entry(_binding, verdict))
                elif _binding.action == "log":
                    logger.info("Reflex %s matched tool %s (confidence=%.2f)", _binding.policy.name, tool_name, verdict.confidence)
            return None

        callbacks.append(_callback)

    return callbacks


def _make_after_tool_callbacks(bindings: list[ReflexBinding], agent_instruction: str | None = None) -> list:
    """Create after_tool_callback functions for after_tool_call bindings."""
    callbacks = []

    for binding in bindings:
        async def _callback(*, tool, args, tool_context, tool_response, _binding=binding):
            tool_name = getattr(tool, "name", str(tool))
            result_str = json.dumps(tool_response) if isinstance(tool_response, dict) else str(tool_response)
            content = f"{tool_name} returned: {result_str}"

            recent_input = None
            if hasattr(tool_context, "user_content") and tool_context.user_content:
                uc = tool_context.user_content
                if hasattr(uc, "parts") and uc.parts:
                    recent_input = uc.parts[0].text if hasattr(uc.parts[0], "text") else str(uc.parts[0])

            task_context, context_label = _build_task_context(
                recent_input=recent_input, agent_instruction=agent_instruction,
            )
            verdict = await safe_acheck(
                policy=_binding.policy.criteria, policy_name=_binding.policy.name, content=content,
                context=task_context, check_type="Tool result", context_label=context_label,
            )
            if isinstance(verdict, EvaluatorError):
                _surface_upgrade(tool_context, verdict.upgrade)
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                return None
            if verdict.label == 1:
                if _binding.action == "deny":
                    return {"error": build_feedback("after_tool_call", _binding.policy.criteria)}
                elif _binding.action == "flag":
                    logger.warning("Reflex %s flagged tool output from %s (confidence=%.2f)", _binding.policy.name, tool_name, verdict.confidence)
                    _add_flag_to_state(tool_context, _build_flag_entry(_binding, verdict))
                elif _binding.action == "log":
                    logger.info("Reflex %s matched tool output from %s (confidence=%.2f)", _binding.policy.name, tool_name, verdict.confidence)
            return None

        callbacks.append(_callback)

    return callbacks


def apply_reflexes(agent, bindings: list[ReflexBinding]):
    """Wire reflex bindings into a Google ADK LlmAgent.

    Reads each binding's trigger and creates the appropriate ADK callback.
    Appends to existing callback lists if present.

    Call after all tools have been added to the agent.
    """
    on_input = [b for b in bindings if b.trigger == "on_input"]
    before_response = [b for b in bindings if b.trigger == "before_response"]
    before_tool = [b for b in bindings if b.trigger == "before_tool_call"]
    after_tool = [b for b in bindings if b.trigger == "after_tool_call"]

    # Capture standing instructions at apply-time. ADK's Agent has an
    # `instruction` field (singular). Used as Standing instructions
    # instructions context per SPEC §5.1 enrichment.
    raw_instruction = getattr(agent, "instruction", None)
    agent_instruction = raw_instruction if isinstance(raw_instruction, str) else None

    if on_input:
        cbs = _make_before_model_callbacks(on_input)
        existing = agent.before_model_callback
        if existing is None:
            agent.before_model_callback = cbs if len(cbs) > 1 else cbs[0]
        elif isinstance(existing, list):
            existing.extend(cbs)
        else:
            agent.before_model_callback = [existing] + cbs

    if before_response:
        cbs = _make_after_model_callbacks(before_response, agent_instruction)
        existing = agent.after_model_callback
        if existing is None:
            agent.after_model_callback = cbs if len(cbs) > 1 else cbs[0]
        elif isinstance(existing, list):
            existing.extend(cbs)
        else:
            agent.after_model_callback = [existing] + cbs

    if before_tool:
        cbs = _make_before_tool_callbacks(before_tool, agent_instruction)
        existing = agent.before_tool_callback
        if existing is None:
            agent.before_tool_callback = cbs if len(cbs) > 1 else cbs[0]
        elif isinstance(existing, list):
            existing.extend(cbs)
        else:
            agent.before_tool_callback = [existing] + cbs

    if after_tool:
        cbs = _make_after_tool_callbacks(after_tool, agent_instruction)
        existing = agent.after_tool_callback
        if existing is None:
            agent.after_tool_callback = cbs if len(cbs) > 1 else cbs[0]
        elif isinstance(existing, list):
            existing.extend(cbs)
        else:
            agent.after_tool_callback = [existing] + cbs

    return agent
