"""Policy-evaluation concurrency cap: default 10, overridable via env.

When multiple policies are bound to the same trigger, integrations evaluate
them in parallel capped at `max_concurrent()`. This locks the default and
the REFLEXES_MAX_CONCURRENT override contract shared by all hosts.
"""
from reflexes.core import DEFAULT_MAX_CONCURRENT, max_concurrent


def test_default_is_10(monkeypatch):
    monkeypatch.delenv("REFLEXES_MAX_CONCURRENT", raising=False)
    assert DEFAULT_MAX_CONCURRENT == 10
    assert max_concurrent() == 10


def test_env_override(monkeypatch):
    monkeypatch.setenv("REFLEXES_MAX_CONCURRENT", "3")
    assert max_concurrent() == 3


def test_invalid_override_falls_back_to_default(monkeypatch):
    for bad in ("0", "-1", "abc", ""):
        monkeypatch.setenv("REFLEXES_MAX_CONCURRENT", bad)
        assert max_concurrent() == 10, f"{bad!r} should fall back to default"
