from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Literal, Protocol


# When several policies are bound to the same trigger, integrations
# evaluate them concurrently, capped at this many in-flight evaluator
# calls per trigger fire. Default 10; override with REFLEXES_MAX_CONCURRENT.
DEFAULT_MAX_CONCURRENT = 10


def max_concurrent() -> int:
    """Max policies evaluated in parallel per trigger fire (default 10)."""
    raw = os.environ.get("REFLEXES_MAX_CONCURRENT")
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return DEFAULT_MAX_CONCURRENT


class Evaluator(Protocol):
    async def evaluate(
        self,
        *,
        policy: str,
        content: str,
        reason: bool = False,
        policy_slug: str | None = None,
    ) -> Verdict: ...


_evaluator: Evaluator | None = None
# When configure() can't instantiate the evaluator (missing key, bad endpoint),
# it stashes the reason here so the doctor can surface the *specific* cause
# instead of a generic "failed to configure". None means "no failure recorded".
_evaluator_error: str | None = None


@dataclass(frozen=True)
class Verdict:
    label: int  # 0 or 1
    confidence: float  # 0.0 - 1.0
    reason: str | None = None
    latency_ms: float | None = None
    # Provenance: which evaluation backend produced this verdict.
    # "cope" = CoPE-graded (Zentropi), "heuristic" = local pattern match,
    # None = third-party LLM evaluator (OpenAI/Anthropic/Gemini) or unset.
    # Used by adapters to surface "[DEMO]" framing on heuristic verdicts.
    mode: Literal["heuristic", "cope"] | None = None


class ReflexDenied(Exception):
    """Raised when a reflex with action=deny matches."""
    def __init__(self, policy_name: str, confidence: float, policy_criteria: str = ""):
        self.policy_name = policy_name
        self.confidence = confidence
        self.policy_criteria = policy_criteria
        super().__init__(f"[Reflex: {policy_name}] Blocked (confidence: {confidence:.2f})")


class ReflexesUpgradeRequired(Exception):
    """Raised when the Zentropi server returns a tier-aware upgrade envelope.

    Surfaced from the SDK on 401 (auth_required) and tier-aware 429
    (rate_limit_exceeded) responses, where retrying the same request
    cannot succeed — the caller needs to upgrade their tier (signup or
    subscribe) for continued evaluation. Distinct from ``EvaluatorError``
    (transient / unknown failures) so framework integrations can surface
    the upgrade notice loudly rather than silently failing open.

    Field shape mirrors the wire envelope exactly so downstream tooling
    can pattern-match on (tier, error, reason) without re-shaping.
    """
    def __init__(
        self,
        *,
        error: str,
        tier: int,
        reason: str,
        message: str,
        upgrade_url: str,
        upgrade_action: str,
        retry_after: int | None = None,
    ):
        self.error = error
        self.tier = tier
        self.reason = reason
        self.message = message
        self.upgrade_url = upgrade_url
        self.upgrade_action = upgrade_action
        self.retry_after = retry_after
        super().__init__(f"[Reflexes Tier {tier}] {message} See: {upgrade_url}")


# Standard feedback messages for self-correction, keyed by trigger.
# Framed neutrally — policies check compliance with rules, not just safety.
FEEDBACK_INTROS = {
    "on_input": "The user's input did not comply with a policy. Inform the user and ask them to rephrase.",
    "before_tool_call": "Your tool call did not comply with a policy. Resubmit your tool call using an alternative approach that complies with the policy.",
    "after_tool_call": "The tool result did not comply with a policy. Do not use this result directly.",
    "before_response": "Your response did not comply with a policy. Please revise to be consistent with the following rules.",
}


def build_feedback(trigger: str, policy_criteria: str) -> str:
    """Build a self-correction feedback message with the full policy text.

    Used by all adapters when deny fires — gives the agent enough context
    to self-correct without custom feedback strings.
    """
    intro = FEEDBACK_INTROS.get(trigger, "This was flagged by a policy check.")
    return f"{intro}\n\nPolicy:\n---\n{policy_criteria}\n---"


class EvaluatorError:
    """Returned by safe_acheck/safe_check when the evaluator fails.

    Allows callers to distinguish between 'check passed' (Verdict with label=0),
    'check failed' (Verdict with label=1), and 'check could not run' (EvaluatorError).
    """
    def __init__(self, policy_name: str, error: str, upgrade: "ReflexesUpgradeRequired | None" = None):
        self.policy_name = policy_name
        self.error = error
        # Set when the failure was a tier/capability upgrade condition
        # (rate limit, subscriber-only capability) — lets integrations surface
        # the notice to the agent rather than treating it as a generic outage.
        self.upgrade = upgrade

    def __repr__(self):
        return f"EvaluatorError(policy={self.policy_name}, error={self.error[:80]})"


# --- Shared upgrade-notice surfacing (host-agnostic) ------------------------
#
# When a policy can't be evaluated for a tier/capability reason (rate limit /
# subscriber-only), the check fails open — the operation proceeds unchecked.
# Every host should tell the agent so it can inform the user that enforcement
# is degraded. The *text* of that advisory and the *once-per-session* dedup are
# the same everywhere, so they live here; each host only supplies its own
# context-injection channel (additionalContext, graph state, etc.).

# In-process record of session keys that have already surfaced an upgrade
# notice, so a sustained condition annotates the agent once per session instead
# of on every hook fire. Long-lived hosts (framework middlewares, gateways)
# pass a stable session key; subprocess hosts (Claude Code CLI) do their own
# file-marker dedup and call format_upgrade_notice directly.
_UPGRADE_SURFACED_SESSIONS: set[str] = set()


def format_upgrade_notice(upgrade: "ReflexesUpgradeRequired") -> str:
    """Agent-facing advisory that a policy could not be evaluated for a tier or
    capability reason (rate limit / subscriber-only), so the operation proceeded
    WITHOUT that check (fail-open). Hosts inject this into the agent's context so
    it can tell the user enforcement is degraded. One consistent string across
    every host; the upgrade's own ``message`` carries the specific fix (subscribe
    or self-host the open CoPE model)."""
    message = getattr(upgrade, "message", "") or "the evaluator required an upgrade to run this check"
    url = getattr(upgrade, "upgrade_url", "") or ""
    tail = f" More: {url}" if url else ""
    return (
        "[REFLEXES] A policy could not be evaluated — "
        f"{message} The operation proceeded WITHOUT this check (fail-open), so "
        f"enforcement for it is degraded. If it's relevant to the user, tell "
        f"them.{tail}"
    )


def format_upgrade_user_notice(upgrade: "ReflexesUpgradeRequired") -> str:
    """User-facing one-line warning that a policy couldn't be checked (rate limit
    / subscriber-only), so enforcement was degraded this turn. Distinct from
    format_upgrade_notice (which is written to the *agent*): this is shown
    directly to the *user* on hosts that have a user-output channel not requiring
    a rewrite of the agent's response (Claude Code / Codex `systemMessage`,
    OpenClaw reply footer). The upgrade's own ``message`` carries the fix."""
    message = getattr(upgrade, "message", "") or "a policy check could not run"
    return (
        f"⚠️ Reflexes: a policy could not be checked — {message} "
        "Enforcement is degraded for this turn."
    )


def should_surface_upgrade(session_key: str) -> bool:
    """Per-session dedup for the upgrade notice (in-process). Returns True the
    first time for ``session_key`` and False thereafter, so a long-lived host
    annotates once per session rather than on every fire. An empty/unknown key
    can't be deduped, so it returns True every time — the host applies its own
    guard if it has one."""
    if not session_key:
        return True
    if session_key in _UPGRADE_SURFACED_SESSIONS:
        return False
    _UPGRADE_SURFACED_SESSIONS.add(session_key)
    return True


def upgrade_from_results(results) -> "ReflexesUpgradeRequired | None":
    """Pull the first attached upgrade condition out of a batch of evaluation
    results — accepts EvaluatorError/Verdict objects or ``(binding, verdict)``
    pairs. Lets a host surface the upgrade notice from a parallel batch where the
    EvaluatorErrors would otherwise be dropped as generic fail-opens."""
    for item in results:
        verdict = item[1] if isinstance(item, tuple) else item
        upgrade = getattr(verdict, "upgrade", None)
        if upgrade is not None:
            return upgrade
    return None


def configure(
    evaluator: str = "zentropi",
    *,
    api_key: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
):
    """Configure the default evaluator.

    All supported evaluators are policy-steerable — they read the policy
    criteria and evaluate content against it.

    Fails open: if the evaluator cannot be instantiated (missing API key,
    bad endpoint, etc.) a warning is logged and ``_evaluator`` is left
    None. Subsequent ``acheck`` / ``safe_acheck`` calls return an
    ``EvaluatorError`` rather than crashing the host. This matches the
    contract from SPEC §4.3 — a governance tool that crashes the host
    on evaluator-config failure won't be adopted.

    Args:
        evaluator: Evaluator type — "zentropi", "openai", "anthropic", or "gemini".
        api_key: API key for the evaluator.
        endpoint: Custom endpoint URL.
        model: Model name/ID. For zentropi: "cope-latest" (default), "cope-b-12b".
    """
    global _evaluator, _evaluator_error
    _evaluator_error = None

    if evaluator not in ("zentropi", "openai", "anthropic", "gemini"):
        # Unknown type is a config error, not a runtime / auth issue —
        # surface loud so users can fix their reflexes.yaml.
        raise ValueError(
            f"Unknown evaluator: {evaluator}. "
            f"Use 'zentropi', 'openai', 'anthropic', or 'gemini'."
        )

    try:
        if evaluator == "zentropi":
            from reflexes.evaluators.zentropi import ZentropiEvaluator
            _evaluator = ZentropiEvaluator(api_key=api_key, endpoint=endpoint, model=model)
        elif evaluator == "openai":
            from reflexes.evaluators.openai_compatible import OpenAICompatibleEvaluator
            _evaluator = OpenAICompatibleEvaluator(
                api_key=api_key, endpoint=endpoint, model=model,
            )
        elif evaluator == "anthropic":
            from reflexes.evaluators.anthropic import AnthropicEvaluator
            _evaluator = AnthropicEvaluator(
                api_key=api_key, endpoint=endpoint, model=model,
            )
        elif evaluator == "gemini":
            from reflexes.evaluators.gemini import GeminiEvaluator
            _evaluator = GeminiEvaluator(
                api_key=api_key, endpoint=endpoint, model=model,
            )
    except Exception as e:
        # Logger only — no stderr emission here. The stderr surface
        # lives at the integration layer (e.g. claude_code.py) which
        # has access to session_id for per-session dedup. A direct
        # stderr print at this point would fire on every fresh hook
        # subprocess (one per tool call), spamming the user — exactly
        # the failure mode dedup is supposed to prevent. The
        # integration sees `_evaluator is None` after configure() and
        # routes the surface through its session-aware dedup channel.
        _logger_module = logging.getLogger("reflexes.core")
        _logger_module.warning(
            "Could not configure %s evaluator: %s. Evaluation will "
            "fail open — Reflexes calls will return EvaluatorError "
            "and the host will continue uninterrupted. Fix the config "
            "or set credentials to re-enable evaluation. See "
            "https://reflexes.sh for setup help.",
            evaluator, e,
        )
        _evaluator = None
        _evaluator_error = f"{evaluator} evaluator: {e}"


def _get_evaluator() -> Evaluator | None:
    """Return the configured evaluator, or None if configure() couldn't
    instantiate one. Callers MUST handle None and treat it as evaluator-
    unavailable (return EvaluatorError, fail open per SPEC §4.3).

    On first call without prior ``configure()``, attempts a default
    Zentropi instantiation; if that fails the function returns None
    rather than raising.
    """
    global _evaluator
    if _evaluator is None:
        try:
            from reflexes.evaluators.zentropi import ZentropiEvaluator
            _evaluator = ZentropiEvaluator()
        except Exception as e:
            _logger_module = logging.getLogger("reflexes.core")
            _logger_module.warning(
                "No evaluator configured and default Zentropi instantiation "
                "failed (%s); evaluation will fail open.",
                e,
            )
            return None
    return _evaluator


def _assemble_content(
    content: str,
    context: str | None = None,
    check_type: str = "Content",
    context_label: str = "User request",
) -> str:
    """Assemble evaluator content with optional context.

    When context is provided, the content is structured with a preamble
    telling the evaluator what to focus on and what the context represents.
    """
    if not context:
        return content
    return (
        f"Evaluate the {check_type} below against the policy. "
        f"The {context_label} is provided for reference — evaluate the {check_type}, not the {context_label}.\n\n"
        f"{context_label}: {context}\n\n"
        f"{check_type}: {content}"
    )


async def acheck(
    policy: str,
    content: str,
    *,
    context: str | None = None,
    check_type: str = "Content",
    context_label: str = "User request",
    reason: bool = False,
    policy_slug: str | None = None,
) -> Verdict:
    """Evaluate content against a policy. Async version.

    Args:
        policy: The policy criteria text.
        content: The content to evaluate.
        context: Optional context provided for reference.
        check_type: What is being evaluated (e.g., "Agent response", "Tool call").
        context_label: What the context represents (e.g., "User request",
                       "System prompt", "Conversation history").
        reason: Whether to request a reason from the evaluator.
        policy_slug: Optional slug identifying the policy (e.g. "safe-delete").
            Forwarded to the evaluator. CoPE / LLM evaluators ignore it;
            the heuristic evaluator dispatches on it.
    """
    evaluator = _get_evaluator()
    if evaluator is None:
        raise RuntimeError(
            "No evaluator configured. configure() failed (likely missing "
            "credentials or unreachable endpoint). Use safe_acheck for "
            "fail-open semantics."
        )
    assembled = _assemble_content(content, context, check_type, context_label)
    return await evaluator.evaluate(
        policy=policy, content=assembled, reason=reason, policy_slug=policy_slug,
    )


def check(
    policy: str,
    content: str,
    *,
    context: str | None = None,
    check_type: str = "Content",
    context_label: str = "User request",
    reason: bool = False,
    policy_slug: str | None = None,
) -> Verdict:
    """Evaluate content against a policy. Sync wrapper."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(acheck(policy=policy, content=content, context=context, check_type=check_type, context_label=context_label, reason=reason, policy_slug=policy_slug))
    # A loop is already running on this thread, so we can't call asyncio.run()
    # here. Run the coroutine to completion on a worker thread and BLOCK for the
    # actual Verdict — `.result()`, not an un-awaited future (which would violate
    # this function's sync `-> Verdict` contract).
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(acheck(policy=policy, content=content, context=context, check_type=check_type, context_label=context_label, reason=reason, policy_slug=policy_slug))).result()


_logger = logging.getLogger("reflexes.core")


async def safe_acheck(
    policy: str,
    content: str,
    *,
    policy_name: str = "unknown",
    context: str | None = None,
    check_type: str = "Content",
    context_label: str = "User request",
    reason: bool = False,
    host: str | None = None,
    trigger: str | None = None,
    policy_slug: str | None = None,
) -> Verdict | EvaluatorError:
    """Like acheck but returns EvaluatorError instead of raising on failure.

    Wraps the evaluator call in an OpenTelemetry span (no-op without the
    ``[otel]`` extra). Annotates the span with verdict attributes on
    success or evaluator.error on failure. ``host`` and ``trigger`` are
    optional context attributes for the span; integrations should pass
    them through when available.
    """
    from reflexes import observability as _obs

    evaluator_type = _evaluator.__class__.__name__ if _evaluator is not None else "none"
    with _obs.evaluator_span(
        policy_name=policy_name,
        evaluator_type=evaluator_type,
        host=host,
        trigger=trigger,
    ) as _span:
        try:
            verdict = await acheck(
                policy=policy, content=content,
                context=context, check_type=check_type,
                context_label=context_label, reason=reason,
                policy_slug=policy_slug,
            )
            _obs.record_verdict(
                _span,
                label=verdict.label,
                confidence=verdict.confidence,
                latency_ms=verdict.latency_ms,
            )
            if verdict.latency_ms is not None:
                _obs.record_latency(evaluator_type, verdict.latency_ms)
            return verdict
        except ReflexesUpgradeRequired as e:
            # Tier-aware envelope dispatch (SPEC §4.3
            # two-tier contract):
            #   - auth_required → fail-CLOSED (re-raise). User authored a
            #     policy and is trusting the framework to enforce it;
            #     silent fail-open on a config-shaped error breaks the
            #     security mental model. Closer to "typo in a security
            #     rule" than to an infra hiccup.
            #   - rate_limit_exceeded → fail-open with stderr notice + the
            #     `upgrade_required` counter. notice print already fired in
            #     the evaluator before the raise; safe_acheck wraps to
            #     preserve the existing fail-open contract for transient
            #     limit hits.
            #   - other future error classes → fail-open until
            #     categorized (default to least-disruptive behavior).
            # The counter increments BEFORE the dispatch so dashboards
            # see both classes without a branch.
            _logger.warning(
                "Evaluator returned upgrade_required for policy '%s' (tier=%s, reason=%s, error=%s)",
                policy_name, e.tier, e.reason, e.error,
            )
            _obs.record_evaluator_error(_span, error_type=type(e).__name__)
            _obs.increment_verdict_counter("upgrade_required")
            if e.error == "auth_required":
                raise
            return EvaluatorError(policy_name=policy_name, error=str(e), upgrade=e)
        except Exception as e:
            _logger.error("Evaluator failed for policy '%s': %s", policy_name, e)
            _obs.record_evaluator_error(_span, error_type=type(e).__name__)
            _obs.increment_verdict_counter("error")
            return EvaluatorError(policy_name=policy_name, error=str(e))


def safe_check(
    policy: str,
    content: str,
    *,
    policy_name: str = "unknown",
    context: str | None = None,
    check_type: str = "Content",
    context_label: str = "User request",
    reason: bool = False,
    policy_slug: str | None = None,
) -> Verdict | EvaluatorError:
    """Like check but returns EvaluatorError instead of raising on failure."""
    try:
        return check(policy=policy, content=content, context=context, check_type=check_type, context_label=context_label, reason=reason, policy_slug=policy_slug)
    except Exception as e:
        _logger.error("Evaluator failed for policy '%s': %s", policy_name, e)
        return EvaluatorError(policy_name=policy_name, error=str(e))
