"""OpenAI Agents SDK integration for Reflexes.

Maps reflex triggers to native SDK guardrail types:
  on_input          -> InputGuardrail (on agent)
  before_response   -> OutputGuardrail (on agent)
  before_tool_call  -> ToolInputGuardrail (on each tool)
  after_tool_call   -> ToolOutputGuardrail (on each tool)

Actions:
  deny  -> tripwire / reject (blocks the operation)
  flag  -> output_info carries verdict; also logged (agent can't read mid-run)
  log   -> logged only

Flag behavior note: OpenAI SDK surfaces flag verdicts via output_info on
the run result, but the agent cannot read them mid-run. For frameworks
that support live state injection (ADK, LangGraph), flag writes to agent
state. For Claude Code, flag uses additionalContext.
"""

from __future__ import annotations

import json
import logging

from reflexes.core import safe_acheck, safe_acheck_many, Verdict, build_feedback, EvaluatorError
from reflexes.config import ReflexBinding

logger = logging.getLogger("reflexes.openai")

SUPPORTED_TRIGGERS = {"on_input", "before_response", "before_tool_call", "after_tool_call"}


def _build_flag_info(binding: ReflexBinding, verdict: Verdict) -> dict:
    """Build structured metadata for a flag verdict."""
    return {
        "reflex": binding.policy.name,
        "trigger": binding.trigger,
        "action": binding.action,
        "label": verdict.label,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "policy_criteria": binding.policy.criteria,
    }


def _handle_verdict(binding: ReflexBinding, verdict) -> tuple[bool, dict | None]:
    """Process a verdict and return (should_block, output_info)."""
    if isinstance(verdict, EvaluatorError):
        logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
        return False, None
    info = _build_flag_info(binding, verdict)

    if verdict.label != 1:
        return False, info

    if binding.action == "deny":
        return True, info

    if binding.action == "flag":
        logger.warning(
            "Reflex %s flagged (confidence=%.2f): %s",
            binding.policy.name, verdict.confidence,
            verdict.reason or "",
        )
        return False, info

    if binding.action == "log":
        logger.warning(
            "Reflex %s matched (confidence=%.2f): %s",
            binding.policy.name, verdict.confidence,
            verdict.reason or "",
        )
        return False, info

    return False, info


def _extract_user_text(turn_input) -> str | None:
    """Normalize an agents-SDK turn input (a str, or a list of message
    dicts/objects) to the most recent user message text."""
    if isinstance(turn_input, str):
        return turn_input or None
    if isinstance(turn_input, list):
        for msg in reversed(turn_input):
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            if role in (None, "user") and isinstance(content, str) and content.strip():
                return content
    return None


def _recent_input(ctx) -> str | None:
    """Best-effort most-recent user input from an agents-SDK context, across
    versions: ``turn_input`` (0.18.x RunContextWrapper), ``input`` (older), or
    ``run_config.input``."""
    if ctx is None:
        return None
    for attr in ("turn_input", "input"):
        text = _extract_user_text(getattr(ctx, attr, None))
        if text:
            return text
    rc = getattr(ctx, "run_config", None)
    if rc is not None:
        return _extract_user_text(getattr(rc, "input", None))
    return None


def _make_input_guardrail(bindings: list[ReflexBinding]):
    # ONE guardrail for all on_input bindings: one System One call for the lot,
    # tripwire on the first deny (any deny blocks the run, as it did with one
    # guardrail per binding); non-blocking flag/log infos are collected into
    # output_info (observable on the run result).
    from agents import InputGuardrail, GuardrailFunctionOutput

    async def _check(ctx, agent, input_data):
        content = input_data if isinstance(input_data, str) else str(input_data)
        specs = [(b.policy.criteria, b.policy.name) for b in bindings]
        verdicts = await safe_acheck_many(specs, content)
        infos = []
        for binding, verdict in zip(bindings, verdicts):
            should_block, info = _handle_verdict(binding, verdict)
            if should_block:
                return GuardrailFunctionOutput(output_info=info, tripwire_triggered=True)
            if info is not None:
                infos.append(info)
        return GuardrailFunctionOutput(output_info=(infos or None), tripwire_triggered=False)

    return InputGuardrail(guardrail_function=_check, name="reflex:on_input")


def _build_task_context(*, recent_input: str | None, agent_instructions: str | None) -> tuple[str | None, str]:
    """Compose PRIMARY/BACKGROUND task-context using the shared assembler.

    Returns (context_or_None, label). When neither source has content,
    returns (None, "User request") so existing eval shape is preserved
    for callers that don't yet supply standing instructions.
    """
    from reflexes._context import assemble_task_context

    composed = assemble_task_context(
        primary_user_prompt=recent_input,
        standing_instructions=agent_instructions,
    )
    if composed:
        return composed, "Task context"
    return None, "User request"


def _make_output_guardrail(bindings: list[ReflexBinding], agent_instructions: str | None = None):
    from agents import OutputGuardrail, GuardrailFunctionOutput

    async def _check(ctx, agent, output_data):
        content = output_data if isinstance(output_data, str) else str(output_data)
        logger.debug("Output guardrail evaluating: %r", content[:200])
        recent_input = _recent_input(ctx)
        # Prefer the live agent's instructions if available (handles dynamic
        # instructions); fall back to the closure-captured value.
        live_instructions = getattr(agent, "instructions", None) if agent is not None else None
        instructions = live_instructions if isinstance(live_instructions, str) else agent_instructions
        task_context, context_label = _build_task_context(
            recent_input=recent_input, agent_instructions=instructions,
        )
        specs = [(b.policy.criteria, b.policy.name) for b in bindings]
        verdicts = await safe_acheck_many(
            specs, content, context=task_context, check_type="Agent response",
            context_label=context_label,
        )
        infos = []
        for binding, verdict in zip(bindings, verdicts):
            should_block, info = _handle_verdict(binding, verdict)
            if should_block:
                return GuardrailFunctionOutput(output_info=info, tripwire_triggered=True)
            if info is not None:
                infos.append(info)
        return GuardrailFunctionOutput(output_info=(infos or None), tripwire_triggered=False)

    return OutputGuardrail(guardrail_function=_check, name="reflex:before_response")


def _make_tool_input_guardrail(bindings: list[ReflexBinding], agent_instructions: str | None = None):
    from agents import ToolInputGuardrail, ToolGuardrailFunctionOutput

    async def _check(data):
        tool_name = data.context.qualified_tool_name or ""
        tool_input = data.context.tool_input or ""
        content = f"{tool_name}({json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)})"
        recent_input = _recent_input(getattr(data, "context", None))
        task_context, context_label = _build_task_context(
            recent_input=recent_input, agent_instructions=agent_instructions,
        )
        specs = [(b.policy.criteria, b.policy.name) for b in bindings]
        verdicts = await safe_acheck_many(
            specs, content, context=task_context, check_type="Tool call",
            context_label=context_label,
        )
        for binding, verdict in zip(bindings, verdicts):
            should_block, _info = _handle_verdict(binding, verdict)
            if should_block:
                return ToolGuardrailFunctionOutput.reject_content(
                    build_feedback(binding.trigger, binding.policy.criteria)
                )
        return ToolGuardrailFunctionOutput.allow()

    return ToolInputGuardrail(guardrail_function=_check, name="reflex:before_tool_call")


def _make_tool_output_guardrail(bindings: list[ReflexBinding], agent_instructions: str | None = None):
    from agents import ToolOutputGuardrail, ToolGuardrailFunctionOutput

    async def _check(data):
        tool_name = data.context.qualified_tool_name or ""
        output = data.output
        content = f"{tool_name} returned: {output if isinstance(output, str) else json.dumps(output)}"
        recent_input = _recent_input(getattr(data, "context", None))
        task_context, context_label = _build_task_context(
            recent_input=recent_input, agent_instructions=agent_instructions,
        )
        specs = [(b.policy.criteria, b.policy.name) for b in bindings]
        verdicts = await safe_acheck_many(
            specs, content, context=task_context, check_type="Tool result",
            context_label=context_label,
        )
        for binding, verdict in zip(bindings, verdicts):
            should_block, _info = _handle_verdict(binding, verdict)
            if should_block:
                return ToolGuardrailFunctionOutput.reject_content(
                    build_feedback(binding.trigger, binding.policy.criteria)
                )
        return ToolGuardrailFunctionOutput.allow()

    return ToolOutputGuardrail(guardrail_function=_check, name="reflex:after_tool_call")


def apply_reflexes(agent, bindings: list[ReflexBinding]):
    """Wire reflex bindings into an OpenAI Agents SDK agent.

    Reads each binding's trigger and creates the appropriate SDK guardrail type.
    - on_input / before_response: appended to agent's guardrail lists
    - before_tool_call / after_tool_call: injected into every tool on the agent

    Call after all tools have been added to the agent. If tools are added
    later, call again to pick up the new tools.
    """
    flag_bindings = [b for b in bindings if b.action == "flag"]
    if flag_bindings:
        names = ", ".join(b.policy.name for b in flag_bindings)
        logger.warning(
            "Action 'flag' on OpenAI Agents SDK is observable after run only "
            "(via output_info on run result), not visible to agent mid-run. "
            "Affected: %s", names,
        )

    # Capture the agent's standing instructions at apply-time. Used as
    # Standing-instructions context in evaluator calls (per
    # SPEC §5.1 enrichment + the anti-pollution behavior). Only
    # plain-string instructions are captured here; callable instructions
    # (dynamic per-turn) are queried live in the output guardrail where
    # the agent ref is in scope. None for callable in tool guardrails —
    # acceptable for v0.1, can be revisited if dynamic instructions
    # become a real use case.
    raw_instructions = getattr(agent, "instructions", None)
    agent_instructions = raw_instructions if isinstance(raw_instructions, str) else None

    # Group bindings by trigger and make ONE guardrail per trigger that batches
    # all of that trigger's policies into a single System One call. (The SDK runs
    # one guardrail per binding would fan out to one request each.)
    known = {"on_input", "before_response", "before_tool_call", "after_tool_call"}
    for binding in bindings:
        if binding.trigger not in known:
            raise ValueError(f"Unknown trigger: {binding.trigger}")
    by_trigger = {t: [b for b in bindings if b.trigger == t] for t in known}

    tool_input_guardrails = []
    tool_output_guardrails = []
    if by_trigger["on_input"]:
        agent.input_guardrails.append(_make_input_guardrail(by_trigger["on_input"]))
    if by_trigger["before_response"]:
        agent.output_guardrails.append(_make_output_guardrail(by_trigger["before_response"], agent_instructions))
    if by_trigger["before_tool_call"]:
        tool_input_guardrails.append(_make_tool_input_guardrail(by_trigger["before_tool_call"], agent_instructions))
    if by_trigger["after_tool_call"]:
        tool_output_guardrails.append(_make_tool_output_guardrail(by_trigger["after_tool_call"], agent_instructions))

    if tool_input_guardrails or tool_output_guardrails:
        for tool in getattr(agent, "tools", []):
            if hasattr(tool, "tool_input_guardrails") and tool_input_guardrails:
                if tool.tool_input_guardrails is None:
                    tool.tool_input_guardrails = []
                tool.tool_input_guardrails.extend(tool_input_guardrails)
            if hasattr(tool, "tool_output_guardrails") and tool_output_guardrails:
                if tool.tool_output_guardrails is None:
                    tool.tool_output_guardrails = []
                tool.tool_output_guardrails.extend(tool_output_guardrails)

    return agent


async def run_with_reflexes(agent, input, *, max_retries: int = 3, **runner_kwargs):
    """Run an OpenAI agent with automatic self-correction on before_response deny.

    Wraps Runner.run() with a retry loop. When an OutputGuardrailTripwireTriggered
    is raised, the feedback is injected as additional input and the agent retries.

    Args:
        agent: An Agent with reflexes applied via apply_reflexes().
        input: The user input (str or list).
        max_retries: Maximum self-correction attempts (default 3).
        **runner_kwargs: Additional kwargs passed to Runner.run().

    Returns:
        The RunResult from a successful run.
    """
    from agents import Runner, OutputGuardrailTripwireTriggered

    current_input = input
    for attempt in range(max_retries + 1):
        try:
            result = await Runner.run(agent, current_input, **runner_kwargs)
            return result
        except OutputGuardrailTripwireTriggered as e:
            if attempt >= max_retries:
                raise
            # Extract the policy feedback from the guardrail output
            feedback = None
            if hasattr(e, 'guardrail_result'):
                gr = e.guardrail_result
                output = getattr(gr, 'output', None)
                info = getattr(output, 'output_info', None) if output else None
                if isinstance(info, dict) and info.get("action") == "deny":
                    criteria = info.get("policy_criteria", "")
                    if criteria:
                        feedback = build_feedback("before_response", criteria)
            if not feedback:
                feedback = f"Your previous response did not comply with a policy. Please revise. (Attempt {attempt + 1}/{max_retries})"

            logger.warning(
                "Output guardrail tripped (attempt %d/%d), retrying with feedback",
                attempt + 1, max_retries,
            )
            # Use feedback as the new input — includes the full policy text
            current_input = f"{input}\n\n{feedback}"
