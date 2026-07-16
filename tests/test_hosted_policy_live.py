"""Live end-to-end tests for hosted policy references against a real Zentropi
labeler.

Matrix: {local file, zentropi-hosted (pinned), zentropi-hosted (unpinned)} x
{zentropi evaluator, openai-compatible evaluator}, plus pinned-vs-unpinned
resolution, cold-vs-warm cache, fail-safe, and not-found-fails-closed.

**Opt-in / maintainer test.** It is skipped unless you point it at a labeler you
own via env vars — so it never fails for a contributor who merely has a
`ZENTROPI_API_KEY`:

    REFLEXES_TEST_LABELER_ID=<labeler_id>              # required to run
    REFLEXES_TEST_LABELER_PINNED_VERSION=<version_id>  # a deployed version, required
    ZENTROPI_API_KEY=...                               # fetch + CoPE eval
    OPENAI_API_KEY=...                                 # optional: cross-evaluator eval

Requirements on the labeler: it implements the *timeline-commitment* policy
(label=1 when content commits to a specific date/deadline — same shape as
``FILE_CRITERIA`` below), and it has at least two deployed versions where the
pinned version's criteria differs from the latest deployed one (so pinned ≠
unpinned can be asserted).
"""
from __future__ import annotations

import os

import pytest

from reflexes import core, policy_cache
from reflexes.document import resolve_policy
from reflexes.policy_sources import PolicyNotFound

LABELER = os.environ.get("REFLEXES_TEST_LABELER_ID")
PINNED = os.environ.get("REFLEXES_TEST_LABELER_PINNED_VERSION")

MATCH = "We will ship the new dashboard on March 15, 2027 — you can count on it."
NOMATCH = "We're actively working on the dashboard; no firm date yet."

FILE_CRITERIA = (
    "# Criteria\n\n## Overview\nDetects a commitment to a specific delivery "
    "date, deadline, or timeline.\n\n## Definition of Labels\n\n"
    "### (1): Timeline Commitment\nContent commits to a specific date, deadline, "
    "or delivery schedule.\n\n### (0): No Timeline Commitment\nContent discusses "
    "timing vaguely or not at all.\n"
)

needs_openai = "OPENAI_API_KEY" in os.environ
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not ("ZENTROPI_API_KEY" in os.environ and LABELER and PINNED),
        reason="set ZENTROPI_API_KEY + REFLEXES_TEST_LABELER_ID + "
        "REFLEXES_TEST_LABELER_PINNED_VERSION to run",
    ),
]


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_cache, "CACHE_DIR", tmp_path / "policy-cache")
    monkeypatch.delenv("REFLEXES_POLICY_CACHE_TTL", raising=False)
    yield tmp_path / "policy-cache"


async def _label(evaluator, api_env, model, policy_text, content):
    core.configure(evaluator=evaluator, api_key=os.environ.get(api_env), model=model)
    v = await core.acheck(policy=policy_text, content=content)
    return v.label


# --------------------------------------------------------------------------- #
# Resolution: pinned vs unpinned
# --------------------------------------------------------------------------- #

def test_pinned_resolves_and_fetches(isolated_cache):
    p = resolve_policy(f"zentropi:{LABELER}@{PINNED}", "timeline-pinned")
    assert p.criteria  # fetched from the Zentropi Build API


def test_unpinned_resolves_to_latest_deployed(isolated_cache):
    p = resolve_policy(f"zentropi:{LABELER}", "timeline-latest")
    assert p.criteria


def test_pinned_and_unpinned_differ(isolated_cache):
    pinned = resolve_policy(f"zentropi:{LABELER}@{PINNED}", "a").criteria
    latest = resolve_policy(f"zentropi:{LABELER}", "b").criteria
    assert pinned != latest, (
        "expected the pinned version to differ from the latest deployed one "
        "(the test labeler needs >=2 deployed versions)"
    )


# --------------------------------------------------------------------------- #
# Cache: cold → warm, pinned served permanently without network; fail-safe
# --------------------------------------------------------------------------- #

def test_cold_cache_writes_then_warm_serves_without_network(isolated_cache, monkeypatch):
    p1 = resolve_policy(f"zentropi:{LABELER}@{PINNED}", "x")
    text1 = p1.criteria
    assert len(list(isolated_cache.glob("zentropi-*.json"))) == 1

    # Break the network; a fresh resolve of the same PINNED ref must still return
    # the criteria from the permanent cache.
    monkeypatch.setenv("ZENTROPI_BUILD_API_BASE", "http://127.0.0.1:9")  # unreachable
    p2 = resolve_policy(f"zentropi:{LABELER}@{PINNED}", "x")
    assert p2.criteria == text1


def test_unpinned_fail_safe_serves_stale_on_network_error(isolated_cache, monkeypatch):
    warm = resolve_policy(f"zentropi:{LABELER}", "x").criteria
    monkeypatch.setenv("ZENTROPI_BUILD_API_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("REFLEXES_POLICY_CACHE_TTL", "0")  # force staleness
    assert resolve_policy(f"zentropi:{LABELER}", "x").criteria == warm


# --------------------------------------------------------------------------- #
# Not found → fail closed
# --------------------------------------------------------------------------- #

def test_bad_version_fails_closed(isolated_cache):
    p = resolve_policy(
        f"zentropi:{LABELER}@00000000-0000-0000-0000-000000000000", "x"
    )
    with pytest.raises(PolicyNotFound):
        _ = p.criteria


# --------------------------------------------------------------------------- #
# Evaluation matrix: {file, hosted-pinned, hosted-unpinned} x {zentropi, openai}
# --------------------------------------------------------------------------- #

def _sources(tmp_path):
    fpath = tmp_path / "timeline.md"
    fpath.write_text(FILE_CRITERIA)
    return {
        "file": resolve_policy(str(fpath), "timeline-file"),
        "hosted-pinned": resolve_policy(f"zentropi:{LABELER}@{PINNED}", "timeline-pinned"),
        "hosted-unpinned": resolve_policy(f"zentropi:{LABELER}", "timeline-latest"),
    }


@pytest.mark.parametrize("source", ["file", "hosted-pinned", "hosted-unpinned"])
def test_matrix_zentropi_evaluator(isolated_cache, tmp_path, source):
    import asyncio
    policy = _sources(tmp_path)[source]
    assert asyncio.run(_label("zentropi", "ZENTROPI_API_KEY", "cope-latest", policy.criteria, MATCH)) == 1
    assert asyncio.run(_label("zentropi", "ZENTROPI_API_KEY", "cope-latest", policy.criteria, NOMATCH)) == 0


@pytest.mark.skipif(not needs_openai, reason="OPENAI_API_KEY not set")
@pytest.mark.parametrize("source", ["file", "hosted-pinned", "hosted-unpinned"])
def test_matrix_openai_evaluator(isolated_cache, tmp_path, source):
    """Cross-evaluator: a zentropi-hosted policy fetched to text and classified
    by a non-Zentropi (OpenAI-compatible) model."""
    import asyncio
    policy = _sources(tmp_path)[source]
    assert asyncio.run(_label("openai", "OPENAI_API_KEY", "gpt-4o-mini", policy.criteria, MATCH)) == 1
    assert asyncio.run(_label("openai", "OPENAI_API_KEY", "gpt-4o-mini", policy.criteria, NOMATCH)) == 0
