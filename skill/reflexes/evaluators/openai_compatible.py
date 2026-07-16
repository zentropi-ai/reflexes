"""Evaluator for any OpenAI-compatible chat completions endpoint.

Works with: OpenAI, Ollama, vLLM, Together, Groq, Azure OpenAI,
LiteLLM, LocalAI, or any server that implements the
POST /v1/chat/completions interface.

Trade-offs vs CoPE:
  - More flexible (any model, any provider)
  - Slower (1-3s vs <200ms) and more expensive ($0.01-0.05 vs $0.001)
  - Requires prompt engineering to get structured output
  - Model may not follow instructions perfectly
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
import logging
from urllib.parse import urlparse

import httpx

from reflexes.core import Verdict
from reflexes.evaluators._retry import RETRYABLE_STATUS_CODES, MAX_ATTEMPTS, backoff

logger = logging.getLogger("reflexes.evaluators.openai_compatible")

DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"


def _is_local_endpoint(endpoint: str) -> bool:
    """True if the endpoint targets a loopback/localhost/private-network host —
    the self-hosted, no-auth case (Ollama, vLLM, llama.cpp, a local CoPE
    server, …). Remote/public endpoints are not local and still require a key.
    """
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

SYSTEM_PROMPT = """\
You are a content classifier. You evaluate whether content matches a policy.

You will receive a POLICY and CONTENT. Determine whether the content matches \
the policy criteria.

Respond with ONLY a JSON object, no other text:
{"label": 0 or 1, "confidence": 0.0 to 1.0}

label=1 means the content matches the policy criteria.
label=0 means it does not."""


class OpenAICompatibleEvaluator:
    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.model = model or DEFAULT_MODEL
        # A key is required for remote/public endpoints, but a self-hosted local
        # server (Ollama, vLLM, local CoPE, …) typically needs no auth — so only
        # demand a key when the endpoint isn't local.
        if not self.api_key and not _is_local_endpoint(self.endpoint):
            raise ValueError(
                "API key required for a remote evaluator endpoint. Set "
                "OPENAI_API_KEY or pass api_key= to configure(). (Local endpoints "
                "like http://localhost:… don't need one.)"
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
        # evaluator returns label + confidence only — no model-authored reason.
        # (Asking a general model for a reason alongside the label made weaker
        # models rationalize an already-committed label and invert it.)
        del policy_slug, reason
        start = time.monotonic()

        user_message = f"POLICY:\n{policy}\n\nCONTENT:\n{content}"
        system = SYSTEM_PROMPT

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # Omit the Authorization header entirely for keyless local servers,
            # rather than sending "Bearer None".
            headers["Authorization"] = f"Bearer {self.api_key}"
        base = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        }
        # Most OpenAI-compatible endpoints (Groq, Ollama, vLLM, llama.cpp,
        # older OpenAI) take `max_tokens` + `temperature`. Newer OpenAI
        # reasoning models reject both: they require `max_completion_tokens`
        # and only allow the default temperature. Try the widely-supported
        # form first; on the specific 400 that names max_completion_tokens,
        # retry with the reasoning-model form (drop temperature).
        last_raw = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(MAX_ATTEMPTS):
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    json={**base, "temperature": 0, "max_tokens": 512},
                )
                if response.status_code == 400 and "max_completion_tokens" in response.text:
                    response = await client.post(
                        self.endpoint,
                        headers=headers,
                        json={**base, "max_completion_tokens": 512},
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
                        f"OpenAI-compatible evaluation failed ({response.status_code}): {response.text}"
                    )

                raw_content = response.json()["choices"][0]["message"]["content"].strip()
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
        # via safe_acheck), never a silent label=0 — a silent 0 on a deny policy
        # is a silent miss.
        raise RuntimeError(
            f"Could not parse evaluator response as JSON after "
            f"{MAX_ATTEMPTS} attempts: {(last_raw or '')[:200]}"
        )
