"""Evaluator using Anthropic's Claude models via the Messages API.

Works with any Claude model: haiku, sonnet, opus.
Faster and cheaper models (haiku) are recommended for reflex evaluation.
"""

from __future__ import annotations

import json
import os
import time
import logging

import httpx

from reflexes.core import Verdict
from reflexes.evaluators._retry import RETRYABLE_STATUS_CODES, MAX_ATTEMPTS, backoff

logger = logging.getLogger("reflexes.evaluators.anthropic")

DEFAULT_ENDPOINT = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """\
You are a content classifier. You evaluate whether content matches a policy.

You will receive a POLICY and CONTENT. Determine whether the content matches \
the policy criteria.

Respond with ONLY a JSON object, no other text:
{"label": 0 or 1, "confidence": 0.0 to 1.0}

label=1 means the content matches the policy criteria.
label=0 means it does not."""


class AnthropicEvaluator:
    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.model = model or DEFAULT_MODEL
        if not self.api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY or pass api_key= to configure()."
            )

    async def evaluate(
        self,
        *,
        policy: str,
        content: str,
        reason: bool = False,
        policy_slug: str | None = None,
    ) -> Verdict:
        # policy_slug/reason accepted for Protocol compliance. Like CoPE, this
        # evaluator returns label + confidence only — no model-authored reason
        # (which made weaker models rationalize and invert an already-committed
        # label).
        del policy_slug, reason
        start = time.monotonic()

        user_message = f"POLICY:\n{policy}\n\nCONTENT:\n{content}"
        system = SYSTEM_PROMPT

        last_raw = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(MAX_ATTEMPTS):
                response = await client.post(
                    self.endpoint,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "system": system,
                        "messages": [
                            {"role": "user", "content": user_message},
                        ],
                        "max_tokens": 512,
                        "temperature": 0,
                    },
                )
                if response.status_code != 200:
                    # Transient overload/5xx → retry; a hard error surfaces now.
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_ATTEMPTS - 1:
                        logger.warning(
                            "Evaluator HTTP %s (attempt %d/%d), retrying",
                            response.status_code, attempt + 1, MAX_ATTEMPTS,
                        )
                        await backoff(attempt)
                        continue
                    logger.error("Evaluation failed: %s %s", response.status_code, response.text)
                    raise RuntimeError(
                        f"Anthropic evaluation failed ({response.status_code}): {response.text}"
                    )

                raw_content = response.json()["content"][0]["text"].strip()
                # Handle markdown code fences
                if raw_content.startswith("```"):
                    raw_content = raw_content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                try:
                    result = json.loads(raw_content)
                except json.JSONDecodeError:
                    # An unparseable body is usually transient — retry.
                    last_raw = raw_content
                    logger.warning(
                        "Failed to parse evaluator response (attempt %d/%d): %s",
                        attempt + 1, MAX_ATTEMPTS, raw_content,
                    )
                    if attempt < MAX_ATTEMPTS - 1:
                        await backoff(attempt)
                        continue
                    break
                return Verdict(
                    label=int(result.get("label", 0)),
                    confidence=float(result.get("confidence", 0.0)),
                    reason=None,
                    latency_ms=(time.monotonic() - start) * 1000,
                )

        # Every attempt returned an unparseable body. Fail LOUD (visible fail-open
        # via safe_acheck), never a silent label=0.
        raise RuntimeError(
            f"Could not parse evaluator response as JSON after "
            f"{MAX_ATTEMPTS} attempts: {(last_raw or '')[:200]}"
        )
