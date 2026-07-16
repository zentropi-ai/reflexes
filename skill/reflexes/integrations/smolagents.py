"""Smolagents (HuggingFace) integration for Reflexes.

Maps reflex triggers to smolagents' callback mechanisms:
  on_input         -> TaskStep callback (fires when task is registered)
  before_response  -> final_answer_checks (fires before final answer accepted)
  before_tool_call -> Not supported (step_callbacks fire AFTER execution)
  after_tool_call  -> ActionStep callback (fires after tool execution)

Actions:
  deny  -> raises ReflexDenied (on_input, after_tool_call) or assert failure
           (before_response triggers retry)
  flag  -> log only (smolagents has no state injection mechanism)
  log   -> log only

Usage:
    from smolagents import ToolCallingAgent, InferenceClientModel
    from reflexes.integrations.smolagents import apply_reflexes

    agent = ToolCallingAgent(tools=[...], model=model)
    apply_reflexes(agent, reflexes=["safety"], config_file="./reflexes.yaml")
"""

from __future__ import annotations

import json
import logging

from concurrent.futures import ThreadPoolExecutor, as_completed

from reflexes.core import safe_check, ReflexDenied, build_feedback, EvaluatorError
from reflexes._context import assemble_task_context

MAX_CONCURRENT = 10


def _task_from_agent(agent) -> str | None:
    """Best-effort recovery of the session's originating task from a smolagents
    agent's memory, for use as "Current request:" task context (SPEC §5.1).

    smolagents has no standing-instructions field we can read cleanly (the
    system prompt is templated), so only the current request is threaded."""
    memory = getattr(agent, "memory", None)
    steps = getattr(memory, "steps", None) if memory else None
    if not steps:
        return None
    for step in steps:
        task = getattr(step, "task", None)
        if task:
            return task
    return None


def _evaluate_parallel_sync(bindings, content, context=None, check_type="Content"):
    """Evaluate all bindings in parallel using threads."""
    def _eval_one(binding):
        verdict = safe_check(
            policy=binding.policy.criteria, policy_name=binding.policy.name,
            content=content, context=context, check_type=check_type,
        )
        return binding, verdict

    matches = []
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT, len(bindings) or 1)) as pool:
        futures = {pool.submit(_eval_one, b): b for b in bindings}
        for future in as_completed(futures):
            binding, verdict = future.result()
            if isinstance(verdict, EvaluatorError):
                logging.getLogger("reflexes.smolagents").warning(
                    "Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                continue
            if verdict.label == 1:
                matches.append((binding, verdict))
    return matches
from reflexes.config import load_config, ReflexBinding, configure_from_file

logger = logging.getLogger("reflexes.smolagents")


def _make_task_callback(bindings: list[ReflexBinding]):
    """Create a TaskStep callback for on_input bindings."""
    def _callback(step):
        content = step.task
        if not content:
            return

        for binding in bindings:
            verdict = safe_check(policy=binding.policy.criteria, policy_name=binding.policy.name, content=content)
            if isinstance(verdict, EvaluatorError):
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                continue
            if verdict.label == 1:
                if binding.action == "deny":
                    raise ReflexDenied(binding.policy.name, verdict.confidence, binding.policy.criteria)
                elif binding.action == "flag":
                    logger.warning(
                        "Reflex %s flagged input (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
                elif binding.action == "log":
                    logger.info(
                        "Reflex %s matched input (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )

    return _callback


def _make_action_callback(bindings: list[ReflexBinding]):
    """Create an ActionStep callback for after_tool_call bindings."""
    # Two-parameter signature so smolagents' CallbackRegistry passes `agent`
    # (single-param callbacks receive only the step) — lets us recover the task
    # for task context (SPEC §5.1).
    def _callback(step, agent=None):
        if not step.tool_calls or not step.observations:
            return

        task_ctx = assemble_task_context(primary_user_prompt=_task_from_agent(agent)) or None
        for tool_call in step.tool_calls:
            content = f"{tool_call.name} returned: {step.observations}"

            for binding in bindings:
                verdict = safe_check(policy=binding.policy.criteria, policy_name=binding.policy.name, content=content,
                                     context=task_ctx, check_type="Tool result")
                if isinstance(verdict, EvaluatorError):
                    logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                    continue
                if verdict.label == 1:
                    if binding.action == "deny":
                        raise ReflexDenied(binding.policy.name, verdict.confidence, binding.policy.criteria)
                    elif binding.action == "flag":
                        logger.warning(
                            "Reflex %s flagged tool output from %s (confidence=%.2f)",
                            binding.policy.name, tool_call.name, verdict.confidence,
                        )
                    elif binding.action == "log":
                        logger.info(
                            "Reflex %s matched tool output from %s (confidence=%.2f)",
                            binding.policy.name, tool_call.name, verdict.confidence,
                        )

    return _callback


def _make_final_answer_check(bindings: list[ReflexBinding]):
    """Create a final_answer_checks function for before_response bindings."""
    def _check(final_answer, memory, agent=None):
        content = str(final_answer)
        user_context = None
        if memory and hasattr(memory, "steps") and memory.steps:
            for step in memory.steps:
                if hasattr(step, "task") and step.task:
                    user_context = step.task
                    break

        # Frame the task through the shared assembler so the evaluator sees the
        # self-describing "Current request:" label matching the other hosts.
        task_ctx = assemble_task_context(primary_user_prompt=user_context) or None
        matches = _evaluate_parallel_sync(bindings, content, context=task_ctx, check_type="Agent response")
        for binding, verdict in matches:
            if binding.action == "deny":
                feedback = build_feedback("before_response", binding.policy.criteria)
                raise ValueError(feedback)
            elif binding.action == "flag":
                logger.warning("Reflex %s flagged output (confidence=%.2f)", binding.policy.name, verdict.confidence)
            elif binding.action == "log":
                logger.info("Reflex %s matched output (confidence=%.2f)", binding.policy.name, verdict.confidence
                    )

        return True

    _check.__name__ = "reflex_final_answer_check"
    return _check


def apply_reflexes(
    agent,
    reflexes: list[str] | None = None,
    *,
    config_file: str = "reflexes.yaml",
):
    """Apply reflexes to a smolagents agent (ToolCallingAgent, CodeAgent, etc.).

    Args:
        agent: A smolagents MultiStepAgent instance.
        reflexes: Config group names. 'default' is always included.
        config_file: Path to reflexes.yaml.

    Returns:
        The agent, with reflexes wired in.
    """
    from smolagents.memory import TaskStep, ActionStep

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
            "Action 'flag' on smolagents degrades to log-only "
            "(no state injection mechanism). Affected: %s", names,
        )

    if "on_input" in by_trigger:
        # TaskStep callbacks don't fire (TaskStep bypasses _finalize_step),
        # so we wrap the run method to check input before execution starts
        input_bindings = by_trigger["on_input"]
        original_run = agent.run

        def _wrapped_run(task, *args, **kwargs):
            matches = _evaluate_parallel_sync(input_bindings, task)
            for binding, verdict in matches:
                if binding.action == "deny":
                    raise ReflexDenied(binding.policy.name, verdict.confidence, binding.policy.criteria)
                elif binding.action == "flag":
                    logger.warning("Reflex %s flagged input (confidence=%.2f)", binding.policy.name, verdict.confidence)
                elif binding.action == "log":
                    logger.info("Reflex %s matched input (confidence=%.2f)", binding.policy.name, verdict.confidence)
            return original_run(task, *args, **kwargs)

        agent.run = _wrapped_run

    if "before_response" in by_trigger:
        agent.final_answer_checks.append(
            _make_final_answer_check(by_trigger["before_response"])
        )

    if "before_tool_call" in by_trigger:
        logger.warning(
            "Trigger 'before_tool_call' is not supported in smolagents "
            "(step_callbacks fire after execution, not before). "
            "%d binding(s) skipped.",
            len(by_trigger["before_tool_call"]),
        )

    if "after_tool_call" in by_trigger:
        agent.step_callbacks.register(
            ActionStep, _make_action_callback(by_trigger["after_tool_call"])
        )

    return agent
