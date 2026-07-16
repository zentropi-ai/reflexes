"""CrewAI integration for Reflexes.

NOT ADVERTISED / UNWIRED (as of v0.1): this integration is unvalidated (no test
coverage, no maturity-tier entry) and is intentionally not reachable from
`with_reflexes()` or listed in the docs. Kept for future validation — before
re-advertising it, add tests and a FRAMEWORKS.md row, then restore the dispatch
branch in api.py.

Maps reflex triggers to CrewAI's hook mechanisms:
  on_input         -> before_kickoff_callbacks (checks kickoff inputs)
  before_response  -> Task.guardrails (checks task output, retry on deny)
  before_tool_call -> register_before_tool_call_hook (global, return False to block)
  after_tool_call  -> register_after_tool_call_hook (global, return str to replace)

Actions:
  deny  -> block/retry depending on mechanism
  flag  -> log only (CrewAI has no state injection mechanism)
  log   -> log only

Note: CrewAI hooks are global — before/after tool call hooks apply to ALL
crews, not just the one you're configuring. Use clear_all_tool_call_hooks()
to reset if needed.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from reflexes.core import safe_check, ReflexDenied, build_feedback, EvaluatorError
from reflexes.config import load_config, ReflexBinding, configure_from_file

MAX_CONCURRENT = 10


def _evaluate_parallel_sync(bindings, content, context=None, check_type="Content"):
    """Evaluate all bindings in parallel using threads. Returns list of (binding, verdict) for matches."""
    def _eval_one(binding):
        verdict = safe_check(
            policy=binding.policy.criteria, policy_name=binding.policy.name,
            content=content, context=context, check_type=check_type,
        )
        return binding, verdict

    matches = []
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT, len(bindings))) as pool:
        futures = {pool.submit(_eval_one, b): b for b in bindings}
        for future in as_completed(futures):
            binding, verdict = future.result()
            if isinstance(verdict, EvaluatorError):
                logging.getLogger("reflexes.crewai").warning(
                    "Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                continue
            if verdict.label == 1:
                matches.append((binding, verdict))
    return matches

logger = logging.getLogger("reflexes.crewai")


def _make_kickoff_callback(bindings: list[ReflexBinding]):
    """Create a before_kickoff_callback for on_input bindings."""
    def _callback(inputs):
        if not inputs:
            return inputs

        # Evaluate the stringified inputs
        content = json.dumps(inputs) if isinstance(inputs, dict) else str(inputs)

        for binding in bindings:
            verdict = safe_check(policy=binding.policy.criteria, policy_name=binding.policy.name, content=content)
            if isinstance(verdict, EvaluatorError):
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                continue
            if verdict.label == 1:
                if binding.action == "deny":
                    raise ReflexDenied(binding.policy.name, verdict.confidence)
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

        return inputs

    return _callback


def _make_task_guardrail(bindings: list[ReflexBinding]):
    """Create a Task guardrail function for before_response bindings."""
    def _guardrail(output):
        content = output.raw if hasattr(output, "raw") else str(output)
        user_context = None
        if hasattr(output, "description"):
            user_context = output.description
        elif hasattr(output, "task_output") and hasattr(output.task_output, "description"):
            user_context = output.task_output.description

        matches = _evaluate_parallel_sync(bindings, content, context=user_context, check_type="Agent response")
        for binding, verdict in matches:
            if binding.action == "deny":
                return (False, build_feedback("before_response", binding.policy.criteria))
            elif binding.action == "flag":
                logger.warning("Reflex %s flagged output (confidence=%.2f)", binding.policy.name, verdict.confidence)
            elif binding.action == "log":
                logger.warning("Reflex %s matched output (confidence=%.2f)", binding.policy.name, verdict.confidence)

        return (True, output)

    return _guardrail


def _make_before_tool_hook(bindings: list[ReflexBinding]):
    """Create a before_tool_call hook function."""
    def _hook(context):
        content = f"{context.tool_name}({json.dumps(context.tool_input)})"
        user_context = None
        if context.task and hasattr(context.task, "description"):
            user_context = context.task.description

        matches = _evaluate_parallel_sync(bindings, content, context=user_context, check_type="Tool call")
        for binding, verdict in matches:
            if binding.action == "deny":
                return False
            elif binding.action == "flag":
                logger.warning("Reflex %s flagged tool %s (confidence=%.2f)", binding.policy.name, context.tool_name, verdict.confidence)
            elif binding.action == "log":
                logger.warning("Reflex %s matched tool %s (confidence=%.2f)", binding.policy.name, context.tool_name, verdict.confidence)

        return None  # Allow

    return _hook


def _make_after_tool_hook(bindings: list[ReflexBinding]):
    """Create an after_tool_call hook function."""
    def _hook(context):
        result = context.tool_result or ""
        content = f"{context.tool_name} returned: {result}"
        user_context = None
        if context.task and hasattr(context.task, "description"):
            user_context = context.task.description

        matches = _evaluate_parallel_sync(bindings, content, context=user_context, check_type="Tool result")
        for binding, verdict in matches:
            if binding.action == "deny":
                return build_feedback("after_tool_call", binding.policy.criteria)
            elif binding.action == "flag":
                logger.warning(
                    "Reflex %s flagged tool output from %s (confidence=%.2f)",
                    binding.policy.name, context.tool_name, verdict.confidence,
                )
            elif binding.action == "log":
                logger.warning(
                        "Reflex %s matched tool output from %s (confidence=%.2f)",
                        binding.policy.name, context.tool_name, verdict.confidence,
                    )

        return None  # Keep original result

    return _hook


def apply_reflexes(
    crew,
    reflexes: list[str] | None = None,
    *,
    config_file: str = "reflexes.yaml",
):
    """Apply reflexes to a CrewAI Crew.

    - on_input: added to crew.before_kickoff_callbacks
    - before_response: added as guardrail to every task on the crew
    - before_tool_call: registered as global hook (affects ALL crews)
    - after_tool_call: registered as global hook (affects ALL crews)

    Args:
        crew: A CrewAI Crew instance.
        reflexes: Config group names. 'default' is always included.
        config_file: Path to reflexes.yaml.

    Returns:
        The crew, with reflexes wired in.
    """
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
            "Action 'flag' on CrewAI degrades to log-only "
            "(no state injection mechanism). Affected: %s", names,
        )

    # on_input -> before_kickoff_callbacks
    if "on_input" in by_trigger:
        crew.before_kickoff_callbacks.append(
            _make_kickoff_callback(by_trigger["on_input"])
        )

    # before_response -> Task guardrails
    if "before_response" in by_trigger:
        guardrail = _make_task_guardrail(by_trigger["before_response"])
        for task in crew.tasks:
            if task.guardrails is None:
                task.guardrails = []
            task.guardrails.append(guardrail)

    # before_tool_call -> global hook
    if "before_tool_call" in by_trigger:
        from crewai.hooks import register_before_tool_call_hook
        logger.warning(
            "CrewAI tool hooks are global — before_tool_call reflexes will "
            "apply to ALL crews, not just this one."
        )
        register_before_tool_call_hook(_make_before_tool_hook(by_trigger["before_tool_call"]))

    # after_tool_call -> global hook
    if "after_tool_call" in by_trigger:
        from crewai.hooks import register_after_tool_call_hook
        logger.warning(
            "CrewAI tool hooks are global — after_tool_call reflexes will "
            "apply to ALL crews, not just this one."
        )
        register_after_tool_call_hook(_make_after_tool_hook(by_trigger["after_tool_call"]))

    return crew
