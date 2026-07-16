"""Regression guards for fixes found by the 2026-07-14 live harness E2E round.

Hermetic (no network, no SDKs): they guard the two Python-side fixes.
  1. `reflexes doctor` must honor CLAUDE_CONFIG_DIR / CODEX_HOME (isolated
     installs) instead of only looking at ~/.claude / ~/.codex.
  2. The `reflexes-hermes-hook` console script the Hermes docstring points at
     must actually be declared (it was referenced but missing → the documented
     hook command failed to spawn and the guardrail failed open).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_doctor_honors_claude_config_dir(tmp_path, monkeypatch):
    from reflexes.doctor import _check_hosts

    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command",
         "command": "python -m reflexes.integrations.claude_code --config x"}]}]}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    results = _check_hosts()
    cc = [r for r in results if r[0] == "Claude Code"]
    assert cc, "Claude Code not detected via CLAUDE_CONFIG_DIR"
    # The status message should point at the isolated dir, and it is 'wired'.
    assert str(tmp_path) in cc[0][2]


def test_doctor_honors_codex_home(tmp_path, monkeypatch):
    from reflexes.doctor import _check_hosts

    cfg = tmp_path / "hooks.json"
    cfg.write_text(json.dumps({"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command",
         "command": "python -m reflexes.integrations.openai_codex --config x"}]}]}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    results = _check_hosts()
    cx = [r for r in results if r[0] == "OpenAI Codex"]
    assert cx, "OpenAI Codex not detected via CODEX_HOME"
    assert str(tmp_path) in cx[0][2]


def test_hermes_console_script_declared():
    """The Hermes docstring recommends `reflexes-hermes-hook`; it must exist as a
    console script (Hermes spawns shell=False, so a missing script fails open)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    scripts = data.get("project", {}).get("scripts", {})
    assert scripts.get("reflexes-hermes-hook") == "reflexes.integrations.nous_hermes_shell:main"


def test_hermes_docstring_has_no_shell_operators():
    """The Hermes docstring's example hook commands must be bare executables —
    no `cd`/`&&`, which fail under Hermes' shell=False spawner (fail-open)."""
    import reflexes.integrations.nous_hermes_shell as m

    doc = m.__doc__ or ""
    # Look only at lines that are example hook commands.
    cmd_lines = [ln for ln in doc.splitlines() if "command:" in ln]
    assert cmd_lines, "no example command lines found in docstring"
    for ln in cmd_lines:
        assert "&&" not in ln and "cd " not in ln, f"shell operator in hook command: {ln.strip()}"
