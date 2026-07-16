"""Live test for GitHub-hosted policy fetch.

Opt-in via ``REFLEXES_TEST_GITHUB_REF`` — a public ``github:`` policy reference;
public repos only, so no secret is needed, just network. Skipped otherwise so
the default suite stays hermetic (the github fetch logic is covered offline in
``test_policy_sources.py``).

Example ref (a real public CoPE policy document; pin to a commit sha so it's
immutable):

    REFLEXES_TEST_GITHUB_REF="github:roostorg/model-community/projects/cope/demo/drug_policy.md@41b6f1306893bd4e088e1866844efde4e16c6fc6"
"""
from __future__ import annotations

import os

import pytest

from reflexes import policy_cache
from reflexes.document import resolve_policy
from reflexes.policy_ref import parse_ref
from reflexes.policy_sources import PolicyNotFound

GITHUB_REF = os.environ.get("REFLEXES_TEST_GITHUB_REF")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not GITHUB_REF,
        reason="set REFLEXES_TEST_GITHUB_REF to a public github:<owner>/<repo>/<path>[@ref]",
    ),
]


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_cache, "CACHE_DIR", tmp_path / "policy-cache")
    monkeypatch.delenv("REFLEXES_POLICY_CACHE_TTL", raising=False)
    yield tmp_path / "policy-cache"


def test_github_fetch_returns_criteria_and_caches(isolated_cache):
    p = resolve_policy(GITHUB_REF, "gh-policy")
    assert p.criteria.strip(), "expected non-empty criteria from the GitHub file"
    assert list(isolated_cache.glob("github-*.json")), "github fetch should cache"


def test_github_pinned_reresolves_from_cache(isolated_cache):
    ref = parse_ref(GITHUB_REF)
    if not ref.is_pinned:
        pytest.skip("ref is unpinned; permanent-cache assertion needs a pinned @sha")
    text = resolve_policy(GITHUB_REF, "gh").criteria
    # Second resolve of the same immutable pinned ref returns the same criteria
    # (served from the permanent cache; no-network behavior is covered in the
    # unit + zentropi-live suites).
    assert resolve_policy(GITHUB_REF, "gh").criteria == text


def test_github_not_found_fails_closed(isolated_cache):
    ref = parse_ref(GITHUB_REF)
    head, _, _ = ref.locator.rpartition("/")
    bad_locator = f"{head}/__reflexes_nonexistent__.md"
    bad = f"github:{bad_locator}" + (f"@{ref.version}" if ref.version else "")
    with pytest.raises(PolicyNotFound):
        _ = resolve_policy(bad, "x").criteria
