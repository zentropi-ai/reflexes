"""Tests for core check functionality.

These tests hit the real Zentropi API — they require ZENTROPI_API_KEY to be set.
Skip with: pytest -m "not integration"
"""

import os
import pytest
import pytest_asyncio

from reflexes.core import Verdict, acheck, configure

pytestmark = pytest.mark.integration

needs_api_key = pytest.mark.skipif(
    not os.environ.get("ZENTROPI_API_KEY"),
    reason="ZENTROPI_API_KEY not set",
)


@needs_api_key
@pytest.mark.asyncio
async def test_acheck_returns_verdict():
    verdict = await acheck(
        policy="Label = 1 when the content is a question. Label = 0 otherwise.",
        content="What time is it?",
    )
    assert isinstance(verdict, Verdict)
    assert verdict.label in (0, 1)
    assert 0.0 <= verdict.confidence <= 1.0
    assert verdict.latency_ms is not None


@needs_api_key
@pytest.mark.asyncio
async def test_acheck_detects_question():
    verdict = await acheck(
        policy="Label = 1 when the content is a question. Label = 0 otherwise.",
        content="What time is it?",
    )
    assert verdict.label == 1


@needs_api_key
@pytest.mark.asyncio
async def test_acheck_statement_is_not_question():
    verdict = await acheck(
        policy="Label = 1 when the content is a question. Label = 0 otherwise.",
        content="The sky is blue.",
    )
    assert verdict.label == 0


def test_verdict_is_frozen():
    v = Verdict(label=1, confidence=0.9)
    with pytest.raises(AttributeError):
        v.label = 0
