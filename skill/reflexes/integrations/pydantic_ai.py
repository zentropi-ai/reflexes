"""PydanticAI integration for Reflexes.

Maps reflex triggers to PydanticAI's validation mechanisms:
  on_input         -> history_processor (checks last user message)
  before_response  -> @output_validator (checks LLM output, ModelRetry on deny)
  before_tool_call -> args_validator (checks tool args, ModelRetry on deny)
  after_tool_call  -> Not directly supported (no post-tool hook in PydanticAI)

Actions:
  deny  -> raises ModelRetry (tells the LLM to try again)
  flag  -> logs the match (PydanticAI has no state injection mechanism)
  log   -> logs only

Usage:
    from pydantic_ai import Agent
    from reflexes.integrations.pydantic_ai import apply_reflexes

    agent = Agent('openai:gpt-4o-mini', system_prompt='...')
    apply_reflexes(agent, reflexes=["safety"], config_file="./reflexes.yaml")
"""

from __future__ import annotations

import json
import asyncio
import logging

from reflexes.core import (
    safe_acheck, safe_acheck_many, ReflexDenied, build_feedback, EvaluatorError, format_upgrade_notice,
)
from reflexes._context import assemble_task_context

MAX_CONCURRENT = 10


def _static_system_prompt(agent) -> str | None:
    """Best-effort static system prompt → standing instructions. Dynamic
    (function) system prompts aren't captured (documented limitation)."""
    sp = getattr(agent, "_system_prompts", None)
    if sp:
        try:
            parts = [s for s in sp if isinstance(s, str) and s.strip()]
            if parts:
                return "\n\n".join(parts)
        except TypeError:
            pass
    return None


def _wrap_run_methods_with_capability(agent, capability) -> bool:
    """Attach a pydantic-ai 2.x capability to an already-constructed agent by
    wrapping its run methods to inject it (2.x has no mutable capabilities list).
    Returns True if any run method was wrapped."""
    import inspect

    wrapped_any = False
    for name in ("run", "run_sync", "run_stream"):
        orig = getattr(agent, name, None)
        if orig is None or getattr(orig, "_reflexes_wrapped", False):
            continue
        try:
            params = inspect.signature(orig).parameters
        except (TypeError, ValueError):
            continue
        if "capabilities" not in params:
            continue

        def _make(orig):
            def wrapper(*args, **kwargs):
                caps = list(kwargs.pop("capabilities", None) or [])
                if capability not in caps:
                    caps.append(capability)
                return orig(*args, capabilities=caps, **kwargs)
            wrapper._reflexes_wrapped = True
            return wrapper

        try:
            setattr(agent, name, _make(orig))
            wrapped_any = True
        except (AttributeError, TypeError):
            pass
    return wrapped_any


def _inject_agent_notice(messages, notice):
    """Best-effort: surface a fail-open notice to the model by appending it to the
    latest user message in the history (PydanticAI's reachable agent channel).
    Never raises — if the message model can't be augmented, degrades to the
    operator-only stderr/OTel baseline."""
    try:
        for msg in reversed(messages):
            for part in getattr(msg, "parts", []) or []:
                if type(part).__name__ == "UserPromptPart" and isinstance(getattr(part, "content", None), str):
                    if notice not in part.content:
                        part.content = f"{part.content}\n\n{notice}"
                    return messages
    except Exception:
        logging.getLogger("reflexes.pydantic_ai").debug(
            "could not inject fail-open notice into PydanticAI history")
    return messages


async def _evaluate_parallel(
    bindings, content, context=None, check_type="Content", context_label="User request",
):
    """Evaluate all bindings in parallel.

    Returns ``(matches, upgrade)`` — matches is a list of (binding, verdict), and
    upgrade is the first tier/capability upgrade condition seen (or None), so the
    caller can surface the fail-open notice to the agent."""
    _specs = [(b.policy.criteria, b.policy.name) for b in bindings]
    _verdicts = await safe_acheck_many(_specs, content, context=context, check_type=check_type, context_label=context_label)
    results = list(zip(bindings, _verdicts))
    matches = []
    upgrade = None
    for binding, verdict in results:
        if isinstance(verdict, EvaluatorError):
            if upgrade is None and getattr(verdict, "upgrade", None) is not None:
                upgrade = verdict.upgrade
            logging.getLogger("reflexes.pydantic_ai").warning(
                "Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
            continue
        if verdict.label == 1:
            matches.append((binding, verdict))
    return matches, upgrade
from reflexes.config import load_config, ReflexBinding, configure_from_file

logger = logging.getLogger("reflexes.pydantic_ai")


def apply_reflexes(
    agent,
    reflexes: list[str] | None = None,
    *,
    config_file: str = "reflexes.yaml",
):
    """Apply reflexes to a PydanticAI Agent.

    Registers history processors, output validators, and tool arg validators
    based on the reflex config.

    Args:
        agent: A PydanticAI Agent instance.
        reflexes: Config group names from reflexes.yaml. 'default' is always included.
        config_file: Path to reflexes.yaml.

    Returns:
        The agent, with reflexes wired in.
    """
    from pydantic_ai import ModelRetry

    configure_from_file(config_file)

    reflexes = reflexes or []
    bindings = load_config(reflexes=reflexes, config_file=config_file)

    by_trigger: dict[str, list[ReflexBinding]] = {}
    for binding in bindings:
        by_trigger.setdefault(binding.trigger, []).append(binding)

    flag_bindings = [b for b in bindings if b.action == "flag"]
    if flag_bindings:
        names = ", ".join(b.policy.name for b in flag_bindings)
        logger.warning(
            "Action 'flag' on PydanticAI degrades to log-only "
            "(no state injection mechanism). Affected: %s", names,
        )

    system_prompt = _static_system_prompt(agent)
    _pending = {"notice": None}  # fail-open notice awaiting delivery to the model

    # A single history processor runs on_input checks (if any) AND delivers any
    # pending fail-open notice to the model — so the agent learns enforcement was
    # degraded, not just the operator log. Registered whenever there are bindings.
    async def _history_processor(messages):
        input_bindings = by_trigger.get("on_input")
        if input_bindings:
            user_content = None
            for msg in reversed(messages):
                if getattr(msg, "kind", None) == "request":
                    for part in msg.parts:
                        if type(part).__name__ == "UserPromptPart" and hasattr(part, "content"):
                            user_content = part.content if isinstance(part.content, str) else str(part.content)
                            break
                if user_content:
                    break
            if user_content:
                matches, upgrade = await _evaluate_parallel(input_bindings, user_content)
                if upgrade is not None:
                    _pending["notice"] = format_upgrade_notice(upgrade)
                for binding, verdict in matches:
                    if binding.action == "deny":
                        raise ReflexDenied(binding.policy.name, verdict.confidence, binding.policy.criteria)
                    elif binding.action == "flag":
                        logger.warning(
                            "Reflex %s flagged input (confidence=%.2f)",
                            binding.policy.name, verdict.confidence,
                        )
                    elif binding.action == "log":
                        logger.warning(
                            "Reflex %s matched input (confidence=%.2f)",
                            binding.policy.name, verdict.confidence,
                        )
        # Deliver any pending fail-open notice (from this or a prior check).
        if _pending["notice"]:
            messages = _inject_agent_notice(messages, _pending["notice"])
            _pending["notice"] = None
        return messages

    # on_input + fail-open agent-notice run through a history processor. Pre-2.x
    # PydanticAI exposed a mutable `agent.history_processors` list; 2.x moved this
    # to the `capabilities` system (no mutable list — attach by wrapping the run
    # methods to inject a ProcessHistory capability). Either way, before_response
    # (output_validator) + before_tool_call (args_validator) enforce independently.
    if bindings and hasattr(agent, "history_processors"):
        if agent.history_processors is None:
            agent.history_processors = []
        agent.history_processors.append(_history_processor)
    elif bindings:
        attached = False
        try:
            from pydantic_ai.capabilities.process_history import ProcessHistory
            attached = _wrap_run_methods_with_capability(
                agent, ProcessHistory(processor=_history_processor)
            )
        except Exception as e:  # version probing — never break wiring over it
            logger.debug("pydantic-ai capabilities attach failed: %s", e)
        if not attached and "on_input" in by_trigger:
            logger.warning(
                "on_input enforcement + fail-open agent-notice could not be wired "
                "on this pydantic-ai version; %d on_input binding(s) skipped. "
                "before_response + before_tool_call still enforce.",
                len(by_trigger["on_input"]),
            )

    # before_response -> output_validator
    if "before_response" in by_trigger:
        response_bindings = by_trigger["before_response"]

        @agent.output_validator
        async def _output_validator(ctx, output):
            content = output if isinstance(output, str) else str(output)
            user_prompt = ctx.prompt if (hasattr(ctx, "prompt") and isinstance(ctx.prompt, str)) else None
            task_context = assemble_task_context(
                primary_user_prompt=user_prompt, standing_instructions=system_prompt,
            ) or None
            matches, upgrade = await _evaluate_parallel(
                response_bindings, content, context=task_context,
                check_type="Agent response", context_label="Task context",
            )
            if upgrade is not None:
                _pending["notice"] = format_upgrade_notice(upgrade)
            for binding, verdict in matches:
                if binding.action == "deny":
                    raise ModelRetry(
                        build_feedback("before_response", binding.policy.criteria)
                    )
                elif binding.action == "flag":
                    logger.warning(
                        "Reflex %s flagged output (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
                elif binding.action == "log":
                        logger.warning(
                            "Reflex %s matched output (confidence=%.2f)",
                            binding.policy.name, verdict.confidence,
                        )
            return output

    # before_tool_call -> can't inject args_validator after agent creation
    # because args_validator is set per-tool at decoration time.
    # We warn and skip.
    if "before_tool_call" in by_trigger:
        logger.warning(
            "Trigger 'before_tool_call' requires args_validator set at tool "
            "decoration time in PydanticAI. Use make_args_validator() to create "
            "a validator for individual tools. %d binding(s) not auto-wired.",
            len(by_trigger["before_tool_call"]),
        )

    if "after_tool_call" in by_trigger:
        logger.warning(
            "Trigger 'after_tool_call' is not supported in PydanticAI "
            "(no post-tool hook). %d binding(s) skipped.",
            len(by_trigger["after_tool_call"]),
        )

    return agent


def make_args_validator(
    reflexes: list[str] | None = None,
    *,
    config_file: str = "reflexes.yaml",
):
    """Create a tool args_validator function from reflex config.

    Use this when defining tools with @agent.tool():

        from reflexes.integrations.pydantic_ai import make_args_validator

        validator = make_args_validator(reflexes=["tool-checks"])

        @agent.tool(args_validator=validator)
        async def search_database(ctx, query: str) -> str:
            ...

    Only before_tool_call bindings are used.
    """
    from pydantic_ai import ModelRetry

    configure_from_file(config_file)

    reflexes = reflexes or []
    bindings = load_config(reflexes=reflexes, config_file=config_file)
    tool_bindings = [b for b in bindings if b.trigger == "before_tool_call"]

    if not tool_bindings:
        return None

    async def _validator(ctx, *args, **kwargs):
        content = json.dumps(kwargs) if kwargs else str(args)
        user_prompt = ctx.prompt if (hasattr(ctx, "prompt") and isinstance(ctx.prompt, str)) else None
        task_context = assemble_task_context(primary_user_prompt=user_prompt) or None
        for binding in tool_bindings:
            verdict = await safe_acheck(
                policy=binding.policy.criteria, policy_name=binding.policy.name,
                content=content, context=task_context,
                check_type="Tool call", context_label="Task context",
            )
            if isinstance(verdict, EvaluatorError):
                continue  # fail-open on evaluator error
            if verdict.label == 1:
                if binding.action == "deny":
                    raise ModelRetry(
                        build_feedback("before_tool_call", binding.policy.criteria)
                    )
                elif binding.action == "flag":
                    logger.warning(
                        "Reflex %s flagged tool args (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
                elif binding.action == "log":
                    logger.warning(
                        "Reflex %s matched tool args (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )

    return _validator
