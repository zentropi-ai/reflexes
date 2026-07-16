"""Auto-update notice: the shared once-per-day surfacing helper + the host hooks.

`reflexes.update.agent_update_notice` is the single source of the agent-facing
"update available" notice, deduped to at most once per 24h per version across
every session and hook fire (so a long session — or a host whose SessionStart
re-fires on resume/clear/compact — doesn't nag). The Claude Code / Codex
SessionStart hook and the Hermes pre_llm_call both call it.
"""
from __future__ import annotations

import reflexes.update as up
import reflexes.integrations.claude_code as cc


# --- The shared helper: once-per-24h-per-version surfacing --------------------

def test_agent_update_notice_wraps_and_surfaces(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "SURFACED_PATH", tmp_path / "surfaced.json")
    monkeypatch.setattr(up, "check_for_update",
                        lambda inst: f"A newer Reflexes version (0.2.0) is available; you're on {inst}.")
    monkeypatch.setattr(up, "_latest_version", lambda: "0.2.0")
    msg = up.agent_update_notice("0.1.0")
    assert msg and "0.2.0" in msg
    assert "SKILL.md" in msg                 # where to update
    assert "let the user know" in msg        # informational, consent-gated
    assert "[Reflexes system status]" in msg # framed as status, not a command


def test_agent_update_notice_once_per_window(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "SURFACED_PATH", tmp_path / "surfaced.json")
    monkeypatch.setattr(up, "check_for_update", lambda inst: "A newer Reflexes version (0.2.0) is available.")
    monkeypatch.setattr(up, "_latest_version", lambda: "0.2.0")
    assert up.agent_update_notice("0.1.0")               # first surfaces
    assert up.agent_update_notice("0.1.0") is None        # same version, within 24h -> quiet
    assert up.agent_update_notice("0.1.0") is None        # still quiet


def test_agent_update_notice_new_version_resurfaces(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "SURFACED_PATH", tmp_path / "surfaced.json")
    monkeypatch.setattr(up, "check_for_update", lambda inst: "A newer version is available.")
    monkeypatch.setattr(up, "_latest_version", lambda: "0.2.0")
    assert up.agent_update_notice("0.1.0")               # surface 0.2.0
    assert up.agent_update_notice("0.1.0") is None        # quiet for 0.2.0
    monkeypatch.setattr(up, "_latest_version", lambda: "0.3.0")
    assert up.agent_update_notice("0.1.0")               # a NEW version re-surfaces immediately


def test_agent_update_notice_no_update(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "SURFACED_PATH", tmp_path / "surfaced.json")
    monkeypatch.setattr(up, "check_for_update", lambda inst: None)
    assert up.agent_update_notice("0.1.0") is None


def test_agent_update_notice_failsafe(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "SURFACED_PATH", tmp_path / "surfaced.json")

    def _boom(inst):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(up, "check_for_update", _boom)
    assert up.agent_update_notice("0.1.0") is None  # never raises


# --- The Claude Code / Codex SessionStart hook wrapper -----------------------

def test_session_start_emits_notice(monkeypatch):
    monkeypatch.setattr("reflexes.update.agent_update_notice",
                        lambda v: "[Reflexes system status] A newer Reflexes version (0.2.0) ... SKILL.md ...")
    out = cc._session_start_update_output()
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert "0.2.0" in hso["additionalContext"]


def test_session_start_no_update(monkeypatch):
    monkeypatch.setattr("reflexes.update.agent_update_notice", lambda v: None)
    assert cc._session_start_update_output() == {}


def test_session_start_failsafe(monkeypatch):
    def _boom(v):
        raise RuntimeError("boom")

    monkeypatch.setattr("reflexes.update.agent_update_notice", _boom)
    assert cc._session_start_update_output() == {}
