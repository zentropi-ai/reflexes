"""Tests for the OpenAI Codex integration.

Codex shares Claude Code's hook protocol — JSON on stdin, JSON on
stdout. The Reflexes integration is a 24-line delegator: it reuses
``reflexes.integrations.claude_code.main`` as-is. These tests verify
the delegation is intact AND that invoking the module as a script
behaves the same as the Claude Code module under a synthetic Codex
hook payload.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_codex_main_is_claude_code_main():
    """The module-level `main` import points at Claude Code's main —
    no diverging behavior, no separate code path to maintain.
    """
    from reflexes.integrations.openai_codex import main as codex_main
    from reflexes.integrations.claude_code import main as cc_main
    assert codex_main is cc_main, (
        "openai_codex.main must be the same object as claude_code.main; "
        "if Codex needs divergent behavior, that's a v0.2 design choice "
        "with its own integration file, not a quiet alias break."
    )


def test_codex_subprocess_pre_tool_use_returns_passthrough_without_evaluator(tmp_path, monkeypatch):
    """End-to-end subprocess invocation: synthetic Codex PreToolUse hook
    input → process exits 0 with `{}` (passthrough) when no evaluator can
    be configured. Same shape as Claude Code; this guards the delegation
    against silent breakage.
    """
    monkeypatch.delenv("ZENTROPI_API_KEY", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo test"},
        "session_id": "codex-test-session",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "cwd": str(tmp_path),
    })

    # Use a non-existent config path — config-load failure path runs
    # cleanly; the hook subprocess returns {} (passthrough) regardless.
    result = subprocess.run(
        [
            sys.executable, "-m", "reflexes.integrations.openai_codex",
            "--config", str(tmp_path / "nonexistent-reflexes.yaml"),
        ],
        input=payload,
        text=True,
        capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=15,
    )

    assert result.returncode == 0, (
        f"Codex hook subprocess exited non-zero "
        f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    # stdout should be a single JSON object — empty passthrough.
    parsed = json.loads(result.stdout.strip() or "{}")
    assert parsed == {} or "hookSpecificOutput" in parsed, (
        f"unexpected stdout JSON: {parsed!r}"
    )


def test_codex_subprocess_session_id_missing_refuses(tmp_path, monkeypatch):
    """Same session_id refusal contract as Claude Code: missing
    session_id → refuse to evaluate, exit 0, stdout = {}.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    (tmp_path / "home").mkdir()

    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo test"},
        # session_id deliberately omitted
        "cwd": str(tmp_path),
    })

    result = subprocess.run(
        [sys.executable, "-m", "reflexes.integrations.openai_codex",
         "--config", str(tmp_path / "reflexes.yaml")],
        input=payload, text=True, capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=15,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "{}", (
        f"expected empty passthrough; got {result.stdout!r}"
    )
