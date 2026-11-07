"""Evaluator for System One decision models (the TypeSafe spec).

System One models take one ``state`` (the content) plus a map of named,
typed ``questions`` and return a calibrated decision for each — all in a
single forward pass. That makes multi-policy evaluation at one decision
point a SINGLE API call instead of one call per policy.

Reflexes default backend is Zentropi's ``cope-b-s1`` via OpenRouter; the
same spec is implemented by TypeSafe's ``jev-latest`` and by a local
self-deployment, so switching backends is just model + endpoint:

    evaluators:
      default:
        type: systemone
        model: cope-b-s1        # or try jev-latest
        endpoint: https://openrouter.ai/api/v1/systemone   # or a local self-deployment

Each Reflexes policy becomes one ``noul`` (true/false) question using the
content-policy convention: the model returns ``P(true)`` — the probability
the content meets the policy criteria — which we threshold into the binary
Verdict label Reflexes already uses downstream.

Spec: https://docs.typesafe.ai/concepts/system-one
"""

from __future__ import annotations

import ipaddress
import os
import time
import logging
from urllib.parse import urlparse

import httpx

from reflexes.core import Verdict
from reflexes.evaluators._retry import RETRYABLE_STATUS_CODES, MAX_ATTEMPTS, backoff

logger = logging.getLogger("reflexes.evaluators.systemone")

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/systemone"
# jev-latest for now — Zentropi's cope-b-s1 (same spec) becomes the default here
# once it's available on OpenRouter; that swap is this one line.
DEFAULT_MODEL = "jev-latest"

# P(true) at/above which a policy is considered matched (label=1). 0.5 keeps the
# binary behavior of the older single-label CoPE path; raise it per use case
# (hard actions like deny want a higher bar). A future pass threads per-action
# thresholds through the binding layer; this is the evaluator-wide default.
DEFAULT_THRESHOLD = 0.5

# System One maps question ids to single letters A-Z internally, so a single
# request carries at most 26 questions. We chunk policy sets larger than this.
MAX_QUESTIONS_PER_CALL = 26

# The documented content-policy convention (see cope-b-s1 model card). The
# policy text is appended to this instruction; criteria frame the yes/no read.
_NOUL_INSTRUCTION = "Does the content meet the criteria described in the following policy?\n\n"
_NOUL_CRITERIA = {
    "true": "The content meets the policy's criteria.",
    "false": "The content does not meet the policy's criteria.",
}


def _is_local_endpoint(endpoint: str) -> bool:
    """True if the endpoint targets loopback/localhost/private network — the
    self-deployed, no-auth case. Remote endpoints (OpenRouter, TypeSafe) still
    require a key."""
    try:
        host = (urlparse(endpoint).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


class SystemOneEvaluator:
    """Policy-steerable decision model speaking the System One protocol.

    Implements the Reflexes ``Evaluator`` Protocol (``evaluate``) plus the
    optional batch capability (``evaluate_batch``) that the core batch seam
    uses to collapse K policies at one decision point into one request.
    """

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        threshold: float = DEFAULT_THRESHOLD,
    ):
        # Default env is OpenRouter's since that's the default endpoint; a Jev
        # or other backend passes its own key via config (api_key / api_key_env).
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.model = model or DEFAULT_MODEL
        self.threshold = threshold
        if not self.api_key and not _is_local_endpoint(self.endpoint):
            raise ValueError(
                "API key required for a remote System One endpoint. Set "
                "OPENROUTER_API_KEY (or pass api_key=/api_key_env for another "
                "backend such as TypeSafe/Jev). Local self-deployments "
                "(http://localhost:…) don't need one."
            )

    async def evaluate(
        self,
        *,
        policy: str,
        content: str,
        reason: bool = False,
        policy_slug: str | None = None,
    ) -> Verdict:
        # System One models return typed decisions + probabilities, never a
        # free-text rationale, so ``reason`` has nothing to populate (same as
        # the CoPE/LLM evaluators). ``policy_slug`` is cosmetic here — question
        # ids are not sent to the model.
        del reason, policy_slug
        verdicts = await self._evaluate_batch([policy], content)
        return verdicts[0]

    async def evaluate_batch(
        self,
        *,
        policies: list[tuple[str | None, str]],
        content: str,
        reason: bool = False,
    ) -> list[Verdict]:
        """Evaluate many policies against ONE content in a single pass.

        ``policies`` is a list of ``(slug, policy_text)``; slugs are optional
        and ignored on the wire. Returns one Verdict per input policy, in
        order. Policy sets larger than the 26-question cap are chunked.
        """
        del reason
        texts = [text for _, text in policies]
        return await self._evaluate_batch(texts, content)

    async def _evaluate_batch(self, policies: list[str], content: str) -> list[Verdict]:
        results: list[Verdict] = []
        for start in range(0, len(policies), MAX_QUESTIONS_PER_CALL):
            chunk = policies[start:start + MAX_QUESTIONS_PER_CALL]
            results.extend(await self._call(chunk, content))
        return results

    def _build_request(self, policies: list[str], content: str) -> dict:
        # Stable, charset-safe question ids (q0..qN); map back by index. The
        # model never sees these ids. Questions come before state in the body
        # so the policy prefix can be cached across contents server-side.
        questions = {
            f"q{i}": {
                "type": "noul",
                "instructions": _NOUL_INSTRUCTION + text,
                "criteria": _NOUL_CRITERIA,
            }
            for i, text in enumerate(policies)
        }
        return {"model": self.model, "questions": questions, "state": {"content": content}}

    def _verdict_from_noul(self, p_true: float, elapsed_ms: float) -> Verdict:
        # Threshold P(true) into the binary label Reflexes uses downstream.
        # Confidence is the probability mass behind whichever label we return,
        # matching the single-label CoPE evaluator's confidence semantics.
        label = 1 if p_true >= self.threshold else 0
        confidence = p_true if label == 1 else 1.0 - p_true
        return Verdict(
            label=label,
            confidence=float(confidence),
            reason=None,
            latency_ms=elapsed_ms,
            mode="cope",
        )

    async def _call(self, policies: list[str], content: str) -> list[Verdict]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = self._build_request(policies, content)

        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(MAX_ATTEMPTS):
                start = time.monotonic()
                response = await client.post(self.endpoint, headers=headers, json=body)
                elapsed_ms = (time.monotonic() - start) * 1000

                if response.status_code != 200:
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_ATTEMPTS - 1:
                        logger.warning(
                            "System One HTTP %s (attempt %d/%d), retrying",
                            response.status_code, attempt + 1, MAX_ATTEMPTS,
                        )
                        await backoff(attempt)
                        continue
                    logger.error("System One evaluation failed: %s %s",
                                 response.status_code, response.text)
                    raise RuntimeError(
                        f"System One evaluation failed ({response.status_code}): {response.text}"
                    )

                try:
                    answers = response.json()["answers"]
                    out = []
                    for i in range(len(policies)):
                        ans = answers[f"q{i}"]
                        out.append(self._verdict_from_noul(float(ans["noul"]), elapsed_ms))
                    return out
                except (KeyError, ValueError, TypeError) as e:
                    # Malformed/partial body — usually transient; retry then fail LOUD.
                    logger.warning(
                        "Could not parse System One response (attempt %d/%d): %s",
                        attempt + 1, MAX_ATTEMPTS, e,
                    )
                    if attempt < MAX_ATTEMPTS - 1:
                        await backoff(attempt)
                        continue
                    raise RuntimeError(
                        f"Could not parse System One response after {MAX_ATTEMPTS} "
                        f"attempts: {response.text[:200]}"
                    )
        # Unreachable: the loop either returns or raises.
        raise RuntimeError("System One evaluation exhausted retries")
