"""NVIDIA NeMo Guardrails integration for Reflexes.

Registers Reflexes evaluators as NeMo custom actions, replacing or
complementing NeMo's default self-check with faster, independent
evaluation via CoPE or other policy-steerable evaluators.

This is a complementary integration — NeMo handles orchestration
(Colang flows, dialog rails, topical control) while Reflexes handles
content evaluation (policy matching via dedicated classifiers).

**Limitation — no self-correction.** On ``deny`` NeMo *substitutes* a
canned refusal (``bot refuse on policy``) for the response; the model
never sees the policy text and never revises. This is a meaningful
difference from the harnesses (Claude Code, OpenClaw, Hermes) and
PydanticAI/smolagents, where deny feeds the policy back and the agent
produces a compliant answer. It's inherent to NeMo's output-rails model
(no retry/revise path), so it's a documented constraint. The fail-open
upgrade notice is likewise operator-only here (see the module note below).

Usage:
    from nemoguardrails import LLMRails, RailsConfig
    from reflexes.integrations.nemo_guardrails import register_reflex_actions

    config = RailsConfig.from_path("./config")
    rails = LLMRails(config)
    register_reflex_actions(rails, config_file="./reflexes.yaml")

Then in your Colang flows (config/rails.co). The bare ``execute
reflex_check_output`` form works because NeMo auto-injects the rails ``context``
into the action (the action reads ``bot_message`` / ``user_message`` from it):

    define flow reflex input safety
      $safe = execute reflex_check_input
      if not $safe
        bot refuse on policy
        stop

    define flow reflex output safety
      $safe = execute reflex_check_output
      if not $safe
        bot refuse on policy
        stop

    define bot refuse on policy
      "I can't share that — it would conflict with one of our content policies."

Fail-open behavior: if the evaluator can't run (rate limit / subscriber-only),
the check fails open (allows) and the notice is **operator-only** here — a stderr
notice + the ``upgrade_required`` counter. NeMo has no user channel that leaves the
bot's response intact (a ``bot inform`` in an output rail *replaces* the answer,
and a footer *appends* to it — both modify the response, which the integrity
principle forbids) and no agent-context channel, so the notice is not surfaced
in-band. See FRAMEWORKS.md "Fail-open upgrade notice".

And in config.yml:

    rails:
      input:
        flows:
          - reflex input safety
      output:
        flows:
          - reflex output safety
"""

from __future__ import annotations

import asyncio
import json
import logging

from reflexes.core import safe_acheck, safe_acheck_many, EvaluatorError

MAX_CONCURRENT = 10
from reflexes.config import load_config, ReflexBinding, configure_from_file
from reflexes._context import assemble_task_context

logger = logging.getLogger("reflexes.nemo")

# NeMo is operator-only for the fail-open upgrade notice: it's an orchestration
# layer, not an agent loop (no agent-context channel), and its only user-facing
# mechanisms in an output rail (`bot inform`, or a message footer) both MODIFY
# the bot's response — which the integrity principle forbids (see FRAMEWORKS.md
# "Fail-open upgrade notice"). So a fail-open here surfaces only via the baseline
# stderr notice + `upgrade_required` counter fired in the evaluator/core layer.


def register_reflex_actions(
    rails,
    reflexes: list[str] | None = None,
    *,
    config_file: str = "reflexes.yaml",
):
    """Register Reflexes evaluators as NeMo Guardrails custom actions.

    Registers these actions for use in Colang flows:
      - reflex_check_input: evaluates $user_message against on_input bindings
      - reflex_check_output: evaluates $bot_message against before_response bindings
      - reflex_check_tool_input: evaluates tool call against before_tool_call bindings
      - reflex_check_tool_output: evaluates tool result against after_tool_call bindings
      - reflex_check: general-purpose check with explicit policy and content params

    Args:
        rails: An LLMRails instance.
        reflexes: Config group names. 'default' is always included.
        config_file: Path to reflexes.yaml.
    """
    configure_from_file(config_file)

    reflexes = reflexes or []
    bindings = load_config(reflexes=reflexes, config_file=config_file)

    by_trigger: dict[str, list[ReflexBinding]] = {}
    for binding in bindings:
        by_trigger.setdefault(binding.trigger, []).append(binding)

    input_bindings = by_trigger.get("on_input", [])
    output_bindings = by_trigger.get("before_response", [])
    tool_input_bindings = by_trigger.get("before_tool_call", [])
    tool_output_bindings = by_trigger.get("after_tool_call", [])

    async def _evaluate(content: str, trigger_bindings: list[ReflexBinding],
                        context: str | None = None, check_type: str | None = None) -> bool:
        """Evaluate all bindings in parallel. Returns True if safe, False if a
        deny matched. Evaluator failures fail open (True) with a log — the
        fail-open notice is baseline-only on NeMo (see module note)."""
        _specs = [(b.policy.criteria, b.policy.name) for b in trigger_bindings]
        _verdicts = await safe_acheck_many(_specs, content, context=context, check_type=check_type or "Content")
        results = list(zip(trigger_bindings, _verdicts))

        for binding, verdict in results:
            if isinstance(verdict, EvaluatorError):
                logger.warning("Evaluator failed for '%s', skipping: %s", verdict.policy_name, verdict.error[:80])
                continue
            if verdict.label == 1:
                if binding.action == "deny":
                    logger.warning(
                        "Reflex %s denied (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
                    return False
                elif binding.action == "flag":
                    logger.warning(
                        "Reflex %s flagged (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
                elif binding.action == "log":
                    logger.warning(
                        "Reflex %s matched (confidence=%.2f)",
                        binding.policy.name, verdict.confidence,
                    )
        return True

    # NeMo v1.0 only auto-populates action params named `context` / `events` /
    # `__context__*` (runtime.py); plain params like `bot_message` stay at their
    # defaults. So each action takes the rails `context` dict and reads the vars
    # it needs from it — matching NeMo's own `self_check_output`. An explicitly
    # passed arg (e.g. `execute reflex_check_output(bot_message=$bot_message)`)
    # still wins as a fallback.

    # Input check — called from Colang as: $safe = execute reflex_check_input
    async def reflex_check_input(context: dict | None = None, user_message: str = "", **kwargs) -> bool:
        """Check user input against on_input reflex policies.

        Returns True if the message is allowed, False if it should be blocked.
        """
        ctx = context or {}
        user_message = user_message or ctx.get("user_message", "") or ""
        if not user_message or not input_bindings:
            return True
        return await _evaluate(user_message, input_bindings)

    # Output check — called from Colang as: $safe = execute reflex_check_output
    async def reflex_check_output(context: dict | None = None, bot_message: str = "", user_message: str = "", **kwargs) -> bool:
        """Check bot output against before_response reflex policies.

        Returns True if the response is allowed, False if it should be blocked.
        """
        ctx = context or {}
        bot_message = bot_message or ctx.get("bot_message", "") or ""
        user_message = user_message or ctx.get("user_message", "") or ""
        if not bot_message or not output_bindings:
            return True
        # Wrap the user_message via the shared task-context framer so the
        # evaluator sees the same labeled context Claude Code, OpenAI Agents,
        # and Google ADK produce. Standing-instructions slot
        # (RailsConfig.instructions) stays v0.2-deferred for parity-of-scope
        # with the LangGraph integration.
        task_ctx = assemble_task_context(primary_user_prompt=user_message or None) or None
        return await _evaluate(bot_message, output_bindings,
                               context=task_ctx, check_type="Agent response")

    # Tool input check
    async def reflex_check_tool_input(context: dict | None = None, tool_name: str = "", tool_arguments: str = "", user_message: str = "", **kwargs) -> bool:
        """Check tool call arguments against before_tool_call reflex policies."""
        ctx = context or {}
        tool_name = tool_name or ctx.get("tool_name", "") or ""
        tool_arguments = tool_arguments or ctx.get("tool_arguments", "") or ""
        user_message = user_message or ctx.get("user_message", "") or ""
        if not tool_input_bindings:
            return True
        # JSON-object payload per SDK Protocol reconciliation —
        # `{"tool_name": ..., "args": ...}`. tool_arguments arrives as a
        # string from Colang; attempt to parse so policies see structure,
        # fall back to the raw string if it's not parseable JSON.
        try:
            parsed_args = json.loads(tool_arguments) if tool_arguments else {}
        except (ValueError, TypeError):
            parsed_args = tool_arguments
        content = json.dumps({"tool_name": tool_name, "args": parsed_args})
        task_ctx = assemble_task_context(primary_user_prompt=user_message or None) or None
        return await _evaluate(content, tool_input_bindings,
                               context=task_ctx, check_type="Tool call")

    # Tool output check
    async def reflex_check_tool_output(context: dict | None = None, tool_name: str = "", tool_result: str = "", user_message: str = "", **kwargs) -> bool:
        """Check tool result against after_tool_call reflex policies."""
        ctx = context or {}
        tool_name = tool_name or ctx.get("tool_name", "") or ""
        tool_result = tool_result or ctx.get("tool_result", "") or ""
        user_message = user_message or ctx.get("user_message", "") or ""
        if not tool_output_bindings:
            return True
        # after_tool_call shape stays as the legacy `<tool> returned: <result>`
        # string — the SDK Protocol reconciliation only locked the
        # before_tool_call payload shape, not after_tool_call.
        content = f"{tool_name} returned: {tool_result}"
        task_ctx = assemble_task_context(primary_user_prompt=user_message or None) or None
        return await _evaluate(content, tool_output_bindings,
                               context=task_ctx, check_type="Tool result")

    # General-purpose check with explicit policy
    async def reflex_check(policy: str = "", content: str = "", **kwargs) -> bool:
        """Check content against an explicit policy string.

        Use this for one-off checks in Colang flows where you want to
        specify the policy inline rather than using reflexes.yaml bindings.
        """
        if not policy or not content:
            return True
        verdict = await safe_acheck(policy=policy, policy_name="inline", content=content)
        if isinstance(verdict, EvaluatorError):
            logger.warning("Evaluator failed for inline check, skipping: %s", verdict.error[:80])
            return True  # fail open
        if verdict.label == 1:
            logger.warning("Reflex check matched (confidence=%.2f)", verdict.confidence)
            return False
        return True

    # Register all actions
    rails.register_action(reflex_check_input, name="reflex_check_input")
    rails.register_action(reflex_check_output, name="reflex_check_output")
    rails.register_action(reflex_check_tool_input, name="reflex_check_tool_input")
    rails.register_action(reflex_check_tool_output, name="reflex_check_tool_output")
    rails.register_action(reflex_check, name="reflex_check")

    registered = []
    if input_bindings:
        registered.append(f"reflex_check_input ({len(input_bindings)} policies)")
    if output_bindings:
        registered.append(f"reflex_check_output ({len(output_bindings)} policies)")
    if tool_input_bindings:
        registered.append(f"reflex_check_tool_input ({len(tool_input_bindings)} policies)")
    if tool_output_bindings:
        registered.append(f"reflex_check_tool_output ({len(tool_output_bindings)} policies)")
    registered.append("reflex_check (general-purpose)")

    logger.info("Registered NeMo Guardrails actions: %s", ", ".join(registered))
