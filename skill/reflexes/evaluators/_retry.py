"""Shared transient-retry policy for the LLM-judge evaluators.

Mirrors the CoPE/zentropi evaluator (see ``zentropi.py``): a transient server
condition — provider overload (429/529) or a 5xx from the model provider — is
worth a quick retry before we fail open, and an occasional unparseable body is
usually transient too. Retry 1 fires immediately, retry 2 after a short beat,
retry 3 after a 1s backstop. The happy path does zero retries and adds zero
latency.
"""
from __future__ import annotations

import asyncio

# Overload (429/529) + 5xx from the model provider. A hard 4xx (400/401/403) is
# deliberately NOT here — that's a config/auth error, surfaced immediately.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 529})

# Total attempts, covering BOTH transient HTTP status and unparseable bodies.
MAX_ATTEMPTS = 3

_RETRY_DELAYS = (0.0, 0.1, 1.0)  # backoff after the Nth (0-indexed) failed attempt


async def backoff(failed_attempt: int) -> None:
    """Await the backoff after ``failed_attempt`` (0-indexed) before retrying.
    Clamps to the last configured delay."""
    delay = _RETRY_DELAYS[min(failed_attempt, len(_RETRY_DELAYS) - 1)]
    if delay:
        await asyncio.sleep(delay)
