"""Evaluator using Zentropi's CoPE models via the Zentropi API.

Default model: cope-latest (smart alias → newest CoPE model). Also
available: cope-b-12b (text + images).
Purpose-built for policy-steerable classification. Fastest and cheapest
evaluator option.

Requires an API key: create a free account at https://zentropi.ai, mint
a key, and pass it via ``api_key`` (or the ``api_key_env`` config field,
default ``ZENTROPI_API_KEY``). A subscriber tier raises rate limits.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import logging

import httpx

from reflexes.core import ReflexesUpgradeRequired, Verdict

logger = logging.getLogger("reflexes.evaluators.zentropi")

DEFAULT_ENDPOINT = "https://api.zentropi.ai/v1/label"
DEFAULT_MODEL = "cope-latest"

# Transient server-side conditions worth an immediate retry: overload
# (429/529) and 5xx from the inference service (500/502/503). 529 is the
# "temporarily overloaded" signal the Zentropi inference tier emits under
# load. Tier-aware 401/429 bypass this set; they raise
# ReflexesUpgradeRequired immediately.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}
# Reflexes runs in the agent's hot path, so the first two retries are
# fast by design: retry 1 fires immediately, retry 2 after a 100ms beat —
# long enough for a load balancer to route around a single bad backend,
# short enough to stay within the in-loop latency budget. Retry 3 waits a
# full 1s as a backstop for a slightly-longer transient (e.g. a brief
# overload spike) before giving up and failing open. The happy path does
# zero retries and adds zero latency.
MAX_RETRIES = 3
RETRY_DELAYS = [0.0, 0.1, 1.0]


def _retry_delay(attempt: int) -> float:
    """Delay (seconds) before the given retry attempt. Clamps to the last
    configured delay so RETRY_DELAYS and MAX_RETRIES don't have to stay in
    lockstep (a short delay list just reuses its final value)."""
    return RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]

# Process-wide set of (tier, error) tuples that have already had their
# notice printed to stderr. Subsequent raises in the same process suppress
# the print to avoid log spam, but the exception itself still propagates.
_UPGRADE_NOTICE_PRINTED: set[tuple[int, str]] = set()

RESOLVED_POLICY_HEADER = "X-Reflexes-Policy-Resolved"


def _print_upgrade_notice_once(exc: ReflexesUpgradeRequired) -> None:
    """Print the upgrade notice to stderr the first time a (tier, error)
    is raised in this process. Idempotent across concurrent calls — a
    rare double-print in a race is harmless; missing prints would not be.
    """
    key = (exc.tier, exc.error)
    if key in _UPGRADE_NOTICE_PRINTED:
        return
    _UPGRADE_NOTICE_PRINTED.add(key)
    print(
        f"\n[Reflexes] {exc.message}\n[Reflexes] {exc.upgrade_url}\n",
        file=sys.stderr,
        flush=True,
    )


def _parse_upgrade_envelope(
    response: httpx.Response,
) -> ReflexesUpgradeRequired | None:
    """Return a ReflexesUpgradeRequired if the response carries a
    tier-aware error envelope, else None.

    Pattern-matches on the Zentropi tier/upgrade error envelope: the
    response body must be JSON with both ``tier`` and ``error`` fields.
    Anything else (HTML error pages, plain-text 5xx, legacy 429) returns
    None and falls through to the existing retry path.
    """
    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if "tier" not in data or "error" not in data:
        return None
    retry_after_header = response.headers.get("Retry-After")
    retry_after: int | None = None
    if retry_after_header is not None:
        try:
            retry_after = int(retry_after_header)
        except ValueError:
            retry_after = None
    return ReflexesUpgradeRequired(
        error=str(data.get("error", "")),
        tier=int(data.get("tier", 0)),
        reason=str(data.get("reason", "")),
        message=str(data.get("message", "")),
        upgrade_url=str(data.get("upgrade_url", "")),
        upgrade_action=str(data.get("upgrade_action", "")),
        retry_after=retry_after,
    )


# Client-side upgrade notice target. Used only to synthesize the signal below,
# because the deployed API does not populate the envelope's upgrade_url.
UPGRADE_URL = "https://zentropi.ai/subscription"
SUPPORT_EMAIL = "info@zentropi.ai"
# CoPE is open-weights — self-hosting it removes Zentropi API rate limits
# entirely (you run your own inference). Offered as an alternative to
# subscribing in the rate-limit case.
COPE_MODEL_CARD = "https://huggingface.co/zentropi-ai/cope-b-a4b"


def _error_detail(response: httpx.Response) -> str:
    """Best-effort human-readable message from a plain (non-envelope) error body."""
    try:
        data = response.json()
        if isinstance(data, dict):
            msg = data.get("detail") or data.get("error")
            if msg:
                return str(msg)[:200]
    except Exception:
        pass
    return response.text[:200]


def _synthesize_upgrade(status: int, detail: str) -> ReflexesUpgradeRequired:
    """Build a ReflexesUpgradeRequired from a plain API error body.

    The deployed API returns plain ``{"detail": ...}`` / ``{"error": ...}``
    bodies for the tier/capability cases — a 403 for image/video inference
    ("requires a subscription") and sustained 429 rate limiting — rather than
    the structured tier envelope (see ``_parse_upgrade_envelope``).
    Until it does, synthesize the signal client-side so these
    surface an upgrade notice + the ``upgrade_required`` counter and fail open,
    instead of vanishing as a generic error.

    FUTURE REQUEST (Zentropi API): emit the structured envelope (``tier``,
    ``message``, ``upgrade_url``, ``upgrade_action``) on the 403/tier-429
    statuses; then ``_parse_upgrade_envelope`` handles them directly and this
    synthesis can be deleted.
    """
    if status == 403:
        error, action = "subscription_required", "subscribe"
        base = detail or "This evaluation requires a Zentropi subscription."
        message = f"{base} Or email {SUPPORT_EMAIL}."
    else:  # sustained 429
        error, action = "rate_limit_exceeded", "subscribe"
        base = detail or "Zentropi rate limit reached — evaluations are being throttled."
        # Two fixes: subscribe to raise the limit, or self-host the open CoPE
        # model to remove rate limits entirely.
        message = (
            f"{base} To fix it: raise the limit with a Zentropi subscription, or "
            f"self-host the open CoPE model for no rate limits at all "
            f"({COPE_MODEL_CARD}). Questions: {SUPPORT_EMAIL}."
        )
    return ReflexesUpgradeRequired(
        error=error,
        tier=0,
        reason="synthesized_from_plain_response",
        message=message,
        upgrade_url=UPGRADE_URL,
        upgrade_action=action,
    )


class ZentropiEvaluator:
    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
    ):
        # api_key may still be None here (env unset); the server requires
        # one and will reject unauthenticated calls. We don't hard-fail
        # client-side — the request surfaces the auth error (and the
        # doctor flags a missing key up front with a get-a-key message).
        self.api_key = api_key or os.environ.get("ZENTROPI_API_KEY")
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.model = model or DEFAULT_MODEL

    async def evaluate(
        self,
        *,
        policy: str,
        content: str,
        reason: bool = False,
        policy_slug: str | None = None,
    ) -> Verdict:
        # policy_slug is part of the Evaluator Protocol but unused here —
        # CoPE evaluates from the policy text directly. The heuristic
        # evaluator dispatches on it.
        del policy_slug
        last_error = None
        last_status: int | None = None
        last_detail: str | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                headers = {"Content-Type": "application/json"}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        self.endpoint,
                        headers=headers,
                        json={
                            "content_text": content,
                            "criteria_text": policy,
                            "model": self.model,
                        },
                    )
                    elapsed_ms = (time.monotonic() - start) * 1000

                if response.status_code == 200:
                    resolved = response.headers.get(RESOLVED_POLICY_HEADER)
                    if resolved:
                        logger.debug("Policy resolved server-side: %s", resolved)
                    data = response.json()
                    return Verdict(
                        label=int(data["label"]),
                        confidence=float(data["confidence"]),
                        reason=data.get("explanation") if reason else None,
                        latency_ms=elapsed_ms,
                        mode="cope",
                    )

                if response.status_code in (401, 429):
                    upgrade = _parse_upgrade_envelope(response)
                    if upgrade is not None:
                        _print_upgrade_notice_once(upgrade)
                        raise upgrade

                # The API does not currently emit the tier envelope above. For a
                # 403 (subscriber-only capability, e.g. image/video inference)
                # synthesize the upgrade signal from the plain body and surface
                # it immediately — retrying cannot succeed.
                if response.status_code == 403:
                    upgrade = _synthesize_upgrade(403, _error_detail(response))
                    _print_upgrade_notice_once(upgrade)
                    raise upgrade

                if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "Zentropi returned %d, retrying in %.1fs (attempt %d/%d)",
                        response.status_code, delay, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    last_error = f"HTTP {response.status_code}: {response.text[:100]}"
                    continue

                last_status = response.status_code
                last_detail = _error_detail(response)
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                break

            except ReflexesUpgradeRequired:
                # Don't retry tier-aware upgrade conditions — retrying
                # the same request cannot succeed, and the caller has
                # received a structured notice explaining what to do next.
                raise

            except httpx.HTTPError as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "Zentropi request failed: %s, retrying in %.1fs (attempt %d/%d)",
                        e, delay, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                break

        # A 429 that survived every retry is sustained throttling, not a blip —
        # surface it as a rate-limit upgrade signal (notice + upgrade_required
        # counter, fail-open) rather than a silent generic failure.
        if last_status == 429:
            upgrade = _synthesize_upgrade(429, last_detail)
            _print_upgrade_notice_once(upgrade)
            raise upgrade

        # Retries exhausted or hit a non-retryable status. `attempt` holds
        # the last iteration index, so attempts made = attempt + 1 (accurate
        # whether we broke early on a hard failure or ran the full retry set).
        attempts = attempt + 1
        logger.error("Zentropi evaluation failed after %d attempt(s): %s", attempts, last_error)
        raise RuntimeError(f"Zentropi evaluation failed after {attempts} attempt(s): {last_error}")
