"""core.check() (sync wrapper) must return a Verdict even when called from
inside a running event loop — not an un-awaited executor Future."""
import asyncio

import reflexes.core as core
from reflexes.core import Verdict


class _FakeEv:
    async def evaluate(self, **kwargs):
        return Verdict(label=1, confidence=0.9, reason=None, latency_ms=1.0)


def test_sync_check_no_running_loop_returns_verdict():
    orig = core._evaluator
    core._evaluator = _FakeEv()
    try:
        v = core.check("policy", "content")
        assert isinstance(v, Verdict) and v.label == 1
    finally:
        core._evaluator = orig


def test_sync_check_from_running_loop_returns_verdict():
    """The bug: the running-loop branch returned loop.run_in_executor(...) (a
    Future) directly, violating the `-> Verdict` contract. It must block for and
    return the real Verdict."""
    orig = core._evaluator
    core._evaluator = _FakeEv()
    try:
        async def _driver():
            # Inside a running loop — check() must still yield a Verdict.
            return core.check("policy", "content")

        v = asyncio.run(_driver())
        assert isinstance(v, Verdict), f"expected Verdict, got {type(v).__name__}"
        assert v.label == 1
    finally:
        core._evaluator = orig
