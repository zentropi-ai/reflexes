"""Tests for the hosted-policy criteria cache (`reflexes.policy_cache`).

Covers: pinned → permanent cache; unpinned → TTL + write-through + fail-safe to
stale; file scheme bypasses the cache; atomic write leaves a file.
"""
from __future__ import annotations

import pytest

from reflexes import policy_cache
from reflexes.policy_ref import parse_ref


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    """Isolate the cache dir and give a controllable clock + TTL."""
    monkeypatch.setattr(policy_cache, "CACHE_DIR", tmp_path / "pcache")
    clock = {"t": 1000.0}
    monkeypatch.setattr(policy_cache.time, "time", lambda: clock["t"])
    monkeypatch.setenv("REFLEXES_POLICY_CACHE_TTL", "3600")
    return clock


def _counting_fetch(value="CRITERIA"):
    calls = {"n": 0}
    holder = {"value": value}

    def fetch():
        calls["n"] += 1
        return holder["value"]

    return fetch, calls, holder


def test_pinned_is_cached_permanently(cache_env):
    ref = parse_ref("zentropi:lab@v5")
    fetch, calls, holder = _counting_fetch("v5-text")
    assert policy_cache.get_or_fetch(ref, fetch) == "v5-text"
    # Change what the source would return; pinned must NOT refetch.
    holder["value"] = "CHANGED"
    cache_env["t"] += 10 * 3600  # far past any TTL
    assert policy_cache.get_or_fetch(ref, fetch) == "v5-text"
    assert calls["n"] == 1


def test_unpinned_serves_fresh_cache_within_ttl(cache_env):
    ref = parse_ref("zentropi:lab")
    fetch, calls, _ = _counting_fetch("latest-text")
    assert policy_cache.get_or_fetch(ref, fetch) == "latest-text"
    cache_env["t"] += 60  # still within the 3600s TTL
    assert policy_cache.get_or_fetch(ref, fetch) == "latest-text"
    assert calls["n"] == 1


def test_unpinned_refetches_after_ttl_with_write_through(cache_env):
    ref = parse_ref("zentropi:lab")
    fetch, calls, holder = _counting_fetch("old-text")
    assert policy_cache.get_or_fetch(ref, fetch) == "old-text"
    # Move past TTL and change the upstream text → refetch + write-through.
    cache_env["t"] += 3601
    holder["value"] = "new-text"
    assert policy_cache.get_or_fetch(ref, fetch) == "new-text"
    assert calls["n"] == 2
    # And the write-through means a subsequent within-TTL read sees new text.
    assert policy_cache.get_or_fetch(ref, fetch) == "new-text"
    assert calls["n"] == 2


def test_unpinned_fail_safe_to_stale_on_fetch_error(cache_env):
    ref = parse_ref("zentropi:lab")
    fetch, calls, _ = _counting_fetch("cached-text")
    assert policy_cache.get_or_fetch(ref, fetch) == "cached-text"

    cache_env["t"] += 3601  # expire

    def boom():
        raise RuntimeError("network down")

    # Refetch fails, but a cached copy exists → serve the stale copy.
    assert policy_cache.get_or_fetch(ref, boom) == "cached-text"


def test_cold_cache_plus_fetch_error_raises(cache_env):
    ref = parse_ref("zentropi:never-cached")

    def boom():
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        policy_cache.get_or_fetch(ref, boom)


def test_file_scheme_bypasses_cache(cache_env):
    ref = parse_ref("./policies/local.md")
    fetch, calls, _ = _counting_fetch("disk-text")
    # File is not cacheable — fetch runs every call.
    assert policy_cache.get_or_fetch(ref, fetch) == "disk-text"
    assert policy_cache.get_or_fetch(ref, fetch) == "disk-text"
    assert calls["n"] == 2
    assert not (policy_cache.CACHE_DIR).exists()  # nothing written for file refs


def test_write_leaves_a_cache_file(cache_env):
    ref = parse_ref("zentropi:lab@v1")
    fetch, _, _ = _counting_fetch("x")
    policy_cache.get_or_fetch(ref, fetch)
    files = list((policy_cache.CACHE_DIR).glob("zentropi-*.json"))
    assert len(files) == 1


def test_env_ttl_zero_always_refetches(cache_env, monkeypatch):
    monkeypatch.setenv("REFLEXES_POLICY_CACHE_TTL", "0")
    ref = parse_ref("zentropi:lab")
    fetch, calls, _ = _counting_fetch("t")
    policy_cache.get_or_fetch(ref, fetch)
    policy_cache.get_or_fetch(ref, fetch)  # ttl=0 → stale immediately
    assert calls["n"] == 2
