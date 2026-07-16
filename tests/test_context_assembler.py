"""Tests for reflexes._context.assemble_task_context (cross-framework)
and reflexes.integrations._claude_code_context.assemble_for_claude_code
(Claude Code-specific resolvers).
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


# --- Shared assembler ----------------------------------------------------

from reflexes._context import (
    assemble_task_context,
    PRIMARY_PROMPT_MAX,
    INITIAL_PROMPT_MAX,
    STANDING_INSTRUCTIONS_MAX,
)


def test_empty_returns_empty_string():
    assert assemble_task_context(
        primary_user_prompt=None,
        initial_user_prompt=None,
        standing_instructions=None,
    ) == ""


def test_primary_only_renders_primary_section():
    out = assemble_task_context(
        primary_user_prompt="delete /tmp/oldfile.txt",
        initial_user_prompt=None,
        standing_instructions=None,
    )
    assert out.startswith("Current request:")
    assert "delete /tmp/oldfile.txt" in out
    assert "Standing instructions:" not in out and "Original task:" not in out


def test_primary_always_leads_when_present():
    """Anti-pollution invariant: the current request appears first regardless of input order."""
    out = assemble_task_context(
        primary_user_prompt="primary",
        initial_user_prompt="initial",
        standing_instructions="standing",
    )
    primary_pos = out.index("Current request:")
    background_pos = out.index("Standing instructions:")
    assert primary_pos < background_pos


def test_background_includes_only_present_sections():
    out = assemble_task_context(
        primary_user_prompt="primary",
        initial_user_prompt=None,
        standing_instructions="standing only",
    )
    assert ("Standing instructions:" in out or "Original task:" in out)
    assert "Standing instructions:" in out
    assert "standing only" in out
    assert "Original task:" not in out


def test_initial_suppressed_when_identical_to_primary():
    """Single-turn sessions where initial == most-recent shouldn't double-print."""
    same_prompt = "delete the file"
    out = assemble_task_context(
        primary_user_prompt=same_prompt,
        initial_user_prompt=same_prompt,
        standing_instructions=None,
    )
    # Current request section appears once; no Original task duplicate.
    assert out.count(same_prompt) == 1
    assert "Original task:" not in out


def test_initial_kept_when_distinct_from_primary():
    out = assemble_task_context(
        primary_user_prompt="follow-up question",
        initial_user_prompt="initial setup task",
        standing_instructions=None,
    )
    assert ("Standing instructions:" in out or "Original task:" in out)
    assert "Original task:" in out
    assert "initial setup task" in out


def test_primary_truncated_at_budget():
    huge = "x" * (PRIMARY_PROMPT_MAX + 1000)
    out = assemble_task_context(
        primary_user_prompt=huge,
        initial_user_prompt=None,
        standing_instructions=None,
    )
    assert "[...truncated]" in out
    # Body of the primary section is <= cap + truncation marker length.
    assert out.count("x") <= PRIMARY_PROMPT_MAX


def test_standing_truncated_most_aggressively():
    """Standing instructions are capped tightest so a large CLAUDE.md
    can't crowd out the primary slot."""
    huge_standing = "y" * 10_000
    out = assemble_task_context(
        primary_user_prompt="abc",  # no y's so the count below is unambiguous
        initial_user_prompt=None,
        standing_instructions=huge_standing,
    )
    assert out.count("y") <= STANDING_INSTRUCTIONS_MAX
    assert "[...truncated]" in out


def test_per_section_budget_not_starved_by_other_large_sections():
    """A huge standing-instructions block must not eat the primary slot."""
    huge_standing = "y" * 100_000
    primary = "real user request"
    out = assemble_task_context(
        primary_user_prompt=primary,
        initial_user_prompt=None,
        standing_instructions=huge_standing,
    )
    # Primary survives in full.
    assert primary in out


# --- Claude Code resolver -----------------------------------------------


@pytest.fixture
def claude_code_env(tmp_path, monkeypatch):
    """Isolated HOME + cwd + transcript fixture for the Claude Code resolver.

    Returns a dict with `cwd`, `transcript_path`, and helper closures.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    return {
        "tmp": tmp_path,
        "home": fake_home,
        "project": project,
        "transcript": transcript,
    }


def _import_resolver():
    import reflexes.integrations._claude_code_context as mod
    importlib.reload(mod)
    return mod


def test_resolver_no_sources_returns_empty(claude_code_env):
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(claude_code_env["project"]),
        transcript_path=str(claude_code_env["transcript"]),
        recent_user_prompt=None,
    )
    assert out == ""


def test_resolver_recent_prompt_only(claude_code_env):
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(claude_code_env["project"]),
        transcript_path=str(claude_code_env["transcript"]),
        recent_user_prompt="delete /tmp/x",
    )
    assert "Current request:" in out
    assert "delete /tmp/x" in out


def test_resolver_reads_project_claude_md(claude_code_env):
    project = claude_code_env["project"]
    (project / "CLAUDE.md").write_text("Always use mv-to-trash, never rm.\n")
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(project),
        transcript_path=str(claude_code_env["transcript"]),
        recent_user_prompt="delete the file",
    )
    assert ("Standing instructions:" in out or "Original task:" in out)
    assert "Standing instructions:" in out
    assert "Always use mv-to-trash" in out


def test_resolver_follows_claude_md_pointer_to_agents_md(claude_code_env):
    project = claude_code_env["project"]
    (project / "CLAUDE.md").write_text("@AGENTS.md\n")  # short pointer
    (project / "AGENTS.md").write_text(
        "Real instructions: never delete user data without confirmation.\n"
    )
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(project),
        transcript_path=str(claude_code_env["transcript"]),
        recent_user_prompt="delete the file",
    )
    assert "Real instructions" in out
    assert "@AGENTS.md" not in out  # the pointer text shouldn't leak through


def test_resolver_falls_back_to_agents_md_when_claude_md_absent(claude_code_env):
    project = claude_code_env["project"]
    (project / "AGENTS.md").write_text("AGENTS-only project instructions.\n")
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(project),
        transcript_path=str(claude_code_env["transcript"]),
        recent_user_prompt="anything",
    )
    assert "AGENTS-only project instructions." in out


def test_resolver_reads_user_global_claude_md(claude_code_env):
    home = claude_code_env["home"]
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("Global preference: be terse.\n")
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(claude_code_env["project"]),
        transcript_path=str(claude_code_env["transcript"]),
        recent_user_prompt="anything",
    )
    assert "Global preference: be terse." in out


def test_resolver_extracts_initial_prompt_from_transcript(claude_code_env):
    transcript = claude_code_env["transcript"]
    transcript.write_text(
        json.dumps({"type": "queue-operation", "operation": "noop"}) + "\n"
        + json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "the original task setup"},
        }) + "\n"
        + json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "follow-up message"},
        }) + "\n",
        encoding="utf-8",
    )
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(claude_code_env["project"]),
        transcript_path=str(transcript),
        recent_user_prompt="follow-up message",
    )
    assert "the original task setup" in out
    # follow-up should not be double-printed (matches primary).
    assert out.count("follow-up message") == 1


def test_resolver_handles_list_content_blocks_in_transcript(claude_code_env):
    transcript = claude_code_env["transcript"]
    transcript.write_text(
        json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first text block"},
                    {"type": "image", "source": {}},
                    {"type": "text", "text": "second text block"},
                ],
            },
        }) + "\n",
        encoding="utf-8",
    )
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(claude_code_env["project"]),
        transcript_path=str(transcript),
        recent_user_prompt="anything",
    )
    assert "first text block" in out
    assert "second text block" in out


def test_resolver_skips_malformed_transcript_lines(claude_code_env):
    transcript = claude_code_env["transcript"]
    transcript.write_text(
        "not json\n"
        + json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "real prompt"},
        }) + "\n",
        encoding="utf-8",
    )
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=str(claude_code_env["project"]),
        transcript_path=str(transcript),
        recent_user_prompt="follow-up",
    )
    assert "real prompt" in out


def test_resolver_no_cwd_no_transcript(claude_code_env):
    cc = _import_resolver()
    out = cc.assemble_for_claude_code(
        cwd=None,
        transcript_path=None,
        recent_user_prompt="just a primary",
    )
    assert "Current request:" in out
    assert "just a primary" in out
