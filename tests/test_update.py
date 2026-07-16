"""Tests for the deterministic version-check half of self-update.

The update *action* is agent-driven (SKILL.md 'Updating Reflexes'); only the
check is code, and it must be fast, cached, and fail-silent.
"""
import re
from pathlib import Path

import reflexes.update as up
from reflexes import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skill" / "SKILL.md"


def test_version_single_source_of_truth():
    from reflexes._version import __version__ as vfile
    assert __version__ == vfile
    # SKILL.md frontmatter version must match _version.py (can't drift).
    m = re.search(r'(?m)^\s*version:\s*"?([0-9][^"\n]*?)"?\s*$', SKILL_MD.read_text())
    assert m, "no version in SKILL.md frontmatter"
    assert m.group(1).strip() == vfile, f"SKILL.md {m.group(1)!r} != _version.py {vfile!r}"


def test_newer_version_returns_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(up, "_fetch_latest",
                        lambda url, t: {"version": "9.9.9", "notes_url": "https://x/notes"})
    n = up.check_for_update("0.1.0", force=True)
    assert n and "9.9.9" in n and "0.1.0" in n and "notes" in n


def test_same_or_older_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(up, "_fetch_latest", lambda url, t: {"version": "0.1.0"})
    assert up.check_for_update("0.1.0", force=True) is None
    monkeypatch.setattr(up, "_fetch_latest", lambda url, t: {"version": "0.0.9"})
    assert up.check_for_update("0.1.0", force=True) is None


def test_fail_silent_on_any_error(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "CACHE_PATH", tmp_path / "c.json")
    def boom(url, t):
        raise RuntimeError("network down")
    monkeypatch.setattr(up, "_fetch_latest", boom)
    assert up.check_for_update("0.1.0", force=True) is None
    # Malformed payload (no 'version') is also silent.
    monkeypatch.setattr(up, "_fetch_latest", lambda url, t: {"nope": 1})
    assert up.check_for_update("0.1.0", force=True) is None


def test_version_key_ordering():
    assert up._version_key("0.2.0") > up._version_key("0.1.9")
    assert up._version_key("1.0.0") > up._version_key("0.9.9")
    assert up._version_key("0.10.0") > up._version_key("0.9.0")   # not lexicographic
    assert up._version_key("0.2.0rc1") == (0, 2, 0)               # tolerates suffixes


def test_cache_avoids_network_when_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "CACHE_PATH", tmp_path / "c.json")
    calls = {"n": 0}
    def fetch(url, t):
        calls["n"] += 1
        return {"version": "9.9.9"}
    monkeypatch.setattr(up, "_fetch_latest", fetch)
    up.check_for_update("0.1.0", force=True)          # forced: fetches + writes cache
    assert calls["n"] == 1
    n = up.check_for_update("0.1.0", force=False)      # fresh cache: no new fetch
    assert calls["n"] == 1 and n and "9.9.9" in n


def test_skill_md_has_agent_driven_updating_section():
    body = SKILL_MD.read_text()
    assert "## Updating" in body
    assert "--check-update" in body
    assert "sha256" in body.lower()                    # mandatory integrity step
    assert "reflexes.yaml" in body                     # preserve user config
