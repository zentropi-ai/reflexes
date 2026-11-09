"""Tests for the Claude Code integration's session-context storage.

Covers:
- Per-user cache dir resolution (XDG-aware).
- Session context file path: refuses 'default' fallback when session_id is empty.
- Save/extract/delete round-trip with proper permissions.
- TTL-based vacuum of stragglers from crashed sessions.
"""
from __future__ import annotations

import importlib
import os
import stat
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Reload claude_code with XDG_CACHE_HOME pointing at a clean tmp path."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Reload the module so module-level CACHE_DIR/SESSIONS_DIR pick up the env.
    import reflexes.integrations.claude_code as cc
    importlib.reload(cc)
    return cc


def test_cache_dir_uses_xdg_cache_home(isolated_cache, tmp_path):
    cc = isolated_cache
    assert cc.CACHE_DIR == tmp_path / "reflexes"
    assert cc.SESSIONS_DIR == tmp_path / "reflexes" / "sessions"


def test_cache_dir_default_when_xdg_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    import reflexes.integrations.claude_code as cc
    importlib.reload(cc)
    assert cc.CACHE_DIR == tmp_path / ".cache" / "reflexes"


def test_cache_dirs_are_owner_only(isolated_cache):
    cc = isolated_cache
    for d in (cc.CACHE_DIR, cc.SESSIONS_DIR):
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700, f"{d} has mode {oct(mode)}, expected 0o700"


def test_context_file_path_refuses_default_fallback(isolated_cache):
    cc = isolated_cache
    assert cc._context_file_for_session("") is None
    assert cc._context_file_for_session("abc123") is not None


def test_upgrade_error_surfaces_to_agent_per_turn_and_user_per_session(isolated_cache, monkeypatch):
    """A tier/capability evaluator failure (ReflexesUpgradeRequired) surfaces to
    the agent via additionalContext (once per TURN — useful recurring context)
    and to the user via systemMessage (once per SESSION — a banner every turn
    would annoy), each with the message + upgrade URL."""
    import asyncio
    import contextlib
    import io
    import json
    from types import SimpleNamespace

    cc = isolated_cache
    from reflexes.core import ReflexesUpgradeRequired, EvaluatorError

    binding = SimpleNamespace(
        trigger="before_response", action="deny",
        policy=SimpleNamespace(name="Sycophancy", criteria="Label = 1 if sycophantic."),
    )
    upgrade = ReflexesUpgradeRequired(
        error="rate_limit_exceeded", tier=0, reason="synthesized_from_plain_response",
        message="Rate limit reached. Or email info@zentropi.ai.",
        upgrade_url="https://zentropi.ai/subscription", upgrade_action="subscribe",
    )

    def stub_load_config(reflexes=None, config_file=None):
        return [binding]

    async def stub_safe_acheck(**kwargs):
        return EvaluatorError(policy_name="Sycophancy", error=str(upgrade), upgrade=upgrade)

    # The integration now evaluates via safe_acheck_many, whose fallback path
    # calls core.safe_acheck — patch there so the stub is reached.
    import reflexes.core as _core
    monkeypatch.setattr(_core, "safe_acheck", stub_safe_acheck)

    args = SimpleNamespace(config="/dev/null")
    hook_input = {
        "hook_event_name": "Stop", "session_id": "sess-up",
        "last_assistant_message": "You're absolutely right!",
        "cwd": "/tmp", "transcript_path": "/nonexistent",
    }

    def run_once():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc._main_body(args, hook_input, "Stop", "sess-up", "before_response",
                          ["default"], asyncio, stub_load_config, stub_safe_acheck, EvaluatorError)
        return json.loads(buf.getvalue().strip() or "{}")

    out1 = run_once()
    # Agent-facing note (additionalContext).
    ctx1 = out1.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "could not be evaluated" in ctx1
    assert "https://zentropi.ai/subscription" in ctx1
    assert "info@zentropi.ai" in ctx1
    assert "fail-open" in ctx1.lower()
    # User-facing warning (top-level systemMessage) — surfaced together.
    sysmsg1 = out1.get("systemMessage", "")
    assert "Reflexes" in sysmsg1 and "could not be checked" in sysmsg1
    assert "degraded" in sysmsg1.lower()
    assert "info@zentropi.ai" in sysmsg1  # the fix (from upgrade.message) reaches the user

    # Simulate the per-turn Stop-hook cleanup (deletes the eval-warnings marker
    # each turn; the dedicated per-session upgrade marker survives it).
    cc._delete_eval_warnings("sess-up")

    # Second call, same session (a later turn). The two audiences dedup
    # differently by design:
    #  - agent note (additionalContext): re-emitted — it's once per TURN, useful
    #    context to keep reminding the model enforcement is degraded.
    #  - user note (systemMessage): NOT re-emitted — once per SESSION, so the
    #    user isn't shown a banner every turn.
    out2 = run_once()
    assert "could not be evaluated" in out2.get("hookSpecificOutput", {}).get("additionalContext", ""), \
        "agent note re-emits per turn"
    assert "systemMessage" not in out2, "user banner stays once per session"


def test_context_file_path_sanitizes_traversal(isolated_cache):
    cc = isolated_cache
    p = cc._context_file_for_session("../etc/passwd")
    assert p is not None
    # No path traversal — the unsafe parts get replaced with '_'
    assert ".." not in p.name
    assert "/" not in p.name


def test_save_user_context_writes_with_owner_only_perms(isolated_cache):
    cc = isolated_cache
    cc._save_user_context("session-A", "the prompt body")
    path = cc._context_file_for_session("session-A")
    assert path.exists()
    assert path.read_text() == "the prompt body"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"{path} has mode {oct(mode)}, expected 0o600"


def test_save_user_context_no_op_when_session_id_missing(isolated_cache, caplog):
    cc = isolated_cache
    with caplog.at_level("WARNING", logger="reflexes.claude_code"):
        cc._save_user_context("", "a prompt")
    files_after = list(cc.SESSIONS_DIR.iterdir())
    assert files_after == [], f"unexpected files: {files_after!r}"
    assert any("session_id missing" in r.message for r in caplog.records)


def test_extract_user_context_returns_none_when_session_id_missing(isolated_cache):
    cc = isolated_cache
    # Ensure a file exists for some other session to confirm we don't read it.
    cc._save_user_context("session-A", "session A prompt")
    got = cc._extract_user_context({"hook_event_name": "PreToolUse", "session_id": ""})
    assert got is None


def test_extract_user_context_round_trip(isolated_cache):
    cc = isolated_cache
    cc._save_user_context("session-X", "stored prompt for X")
    got = cc._extract_user_context({"hook_event_name": "PreToolUse", "session_id": "session-X"})
    assert got == "stored prompt for X"


def test_extract_user_context_userprompt_event_returns_payload(isolated_cache):
    cc = isolated_cache
    got = cc._extract_user_context({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-Y",
        "prompt": "fresh prompt",
    })
    # UserPromptSubmit returns the payload directly, regardless of stored ctx.
    assert got == "fresh prompt"


def test_delete_session_context_removes_file(isolated_cache):
    cc = isolated_cache
    cc._save_user_context("session-Z", "to be deleted")
    path = cc._context_file_for_session("session-Z")
    assert path.exists()
    cc._delete_session_context("session-Z")
    assert not path.exists()


def test_delete_session_context_idempotent(isolated_cache):
    cc = isolated_cache
    # Should not raise even when the file doesn't exist.
    cc._delete_session_context("never-existed")
    cc._delete_session_context("")  # also handles missing session_id


def test_vacuum_old_sessions_only_removes_stale(isolated_cache):
    cc = isolated_cache
    cc._save_user_context("recent", "fresh prompt")
    cc._save_user_context("stale", "old prompt")

    # Backdate the stale file by 25 hours
    stale_path = cc._context_file_for_session("stale")
    backdated = time.time() - (25 * 60 * 60)
    os.utime(stale_path, (backdated, backdated))

    vacuumed = cc._vacuum_old_sessions(max_age_secs=24 * 60 * 60)
    assert vacuumed == 1
    assert not stale_path.exists()
    assert cc._context_file_for_session("recent").exists()


# --- Trajectory tracking tests -------------------------------------------


def test_trajectory_file_path_refuses_default_fallback(isolated_cache):
    cc = isolated_cache
    assert cc._trajectory_file_for_session("") is None
    assert cc._trajectory_file_for_session("abc") is not None


def test_trajectory_file_path_sanitizes_traversal(isolated_cache):
    cc = isolated_cache
    p = cc._trajectory_file_for_session("../etc/passwd")
    assert p is not None
    assert ".." not in p.name
    assert "/" not in p.name


def test_append_and_read_round_trip(isolated_cache):
    cc = isolated_cache
    cc._append_trajectory("session-A", {
        "event": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls /tmp"},
    })
    cc._append_trajectory("session-A", {
        "event": "PostToolUse", "tool_name": "Bash", "tool_response": "tmp1\ntmp2\n",
    })
    entries = cc._read_trajectory("session-A")
    assert len(entries) == 2
    assert entries[0]["event"] == "PreToolUse"
    assert entries[0]["tool_input"] == {"command": "ls /tmp"}
    assert entries[1]["event"] == "PostToolUse"
    assert entries[1]["tool_response"] == "tmp1\ntmp2\n"


def test_append_writes_with_owner_only_perms(isolated_cache):
    cc = isolated_cache
    cc._append_trajectory("session-X", {"event": "PreToolUse", "tool_name": "T"})
    path = cc._trajectory_file_for_session("session-X")
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"{path} has mode {oct(mode)}, expected 0o600"


def test_append_no_op_when_session_id_missing(isolated_cache):
    cc = isolated_cache
    cc._append_trajectory("", {"event": "PreToolUse", "tool_name": "T"})
    assert list(cc.SESSIONS_DIR.iterdir()) == []


def test_read_returns_empty_for_missing_file_or_empty_session(isolated_cache):
    cc = isolated_cache
    assert cc._read_trajectory("never-saved") == []
    assert cc._read_trajectory("") == []


def test_format_trajectory_renders_pre_and_post(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {"event": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm /tmp/x"}},
        {"event": "PostToolUse", "tool_name": "Bash", "tool_response": "removed"},
    ])
    assert text.startswith("Actions this turn:")
    assert "PreToolUse: Bash: rm /tmp/x" in text
    assert "PostToolUse: Bash ✓" in text


def test_format_trajectory_empty_when_no_entries(isolated_cache):
    cc = isolated_cache
    assert cc._format_trajectory([]) == ""


def test_format_trajectory_truncates_large_failure_payloads(isolated_cache):
    cc = isolated_cache
    huge_error = "x" * 5000
    text = cc._format_trajectory([
        {
            "event": "PostToolUse",
            "tool_name": "Bash",
            "tool_response": {"exit_code": 1, "stderr": huge_error},
        },
    ])
    # Failure-path head is rendered and capped; success path emits no head at all.
    assert "...[truncated]" in text
    assert text.count("x") < 5000


def test_format_trajectory_uses_primary_arg_for_known_tool(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/repo/api/users.py",
                "old_string": "def get():",
                "new_string": "def get_user():",
            },
        },
    ])
    # Primary arg (file_path) appears, ancillary fields are dropped.
    assert "PreToolUse: Edit: /repo/api/users.py" in text
    assert "old_string" not in text
    assert "new_string" not in text


def test_format_trajectory_falls_back_to_full_dict_for_unknown_tool(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PreToolUse",
            "tool_name": "MysteryTool",
            "tool_input": {"foo": "bar", "baz": 1},
        },
    ])
    # Unknown tools fall through to full-dict rendering.
    assert "PreToolUse: MysteryTool(" in text
    assert "foo" in text and "bar" in text


def test_format_trajectory_falls_back_when_primary_field_missing(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"timeout": 5000},  # no 'command' field
        },
    ])
    # Falls back to full-dict when primary field absent.
    assert "PreToolUse: Bash(" in text
    assert "timeout" in text


def test_format_trajectory_post_marks_success_for_zero_exit_code(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PostToolUse",
            "tool_name": "Bash",
            "tool_response": {"exit_code": 0, "stdout": "done\n"},
        },
    ])
    assert "PostToolUse: Bash ✓" in text
    assert "✗" not in text
    # Success path emits no payload head — the response dict should not appear.
    assert "stdout" not in text
    assert "done" not in text


def test_format_trajectory_post_marks_failure_for_nonzero_exit_code(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PostToolUse",
            "tool_name": "Bash",
            "tool_response": {"exit_code": 1, "stderr": "command not found"},
        },
    ])
    assert "PostToolUse: Bash ✗" in text
    assert "command not found" in text


def test_format_trajectory_post_marks_failure_for_error_field(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PostToolUse",
            "tool_name": "Read",
            "tool_response": {"error": "file not found: /missing.txt"},
        },
    ])
    assert "PostToolUse: Read ✗" in text
    # Error message surfaces in the head, not the full dict shape.
    assert "file not found" in text


def test_format_trajectory_post_marks_plain_string_as_success(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PostToolUse",
            "tool_name": "Read",
            "tool_response": "file contents here",
        },
    ])
    assert "PostToolUse: Read ✓" in text
    # Success path emits no payload head, even for plain strings.
    assert "file contents here" not in text


def test_format_trajectory_post_unrecognized_dict_treated_as_success(isolated_cache):
    cc = isolated_cache
    text = cc._format_trajectory([
        {
            "event": "PostToolUse",
            "tool_name": "WebFetch",
            "tool_response": {"status": 200, "body": "ok"},
        },
    ])
    # No error/exit_code/errno/stderr → success (false-success preferred over false-failure).
    assert "PostToolUse: WebFetch ✓" in text


def test_format_trajectory_caps_primary_arg_at_max_chars(isolated_cache):
    cc = isolated_cache
    long_command = "echo " + "y" * 1000
    text = cc._format_trajectory([
        {
            "event": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": long_command},
        },
    ])
    assert "...[truncated]" in text
    # Primary-arg cap is 200, well below the original 1005-char command.
    line = next(l for l in text.splitlines() if "Bash:" in l)
    assert len(line) < 300


def test_delete_trajectory_removes_file(isolated_cache):
    cc = isolated_cache
    cc._append_trajectory("session-Z", {"event": "PreToolUse", "tool_name": "T"})
    path = cc._trajectory_file_for_session("session-Z")
    assert path.exists()
    cc._delete_trajectory("session-Z")
    assert not path.exists()


def test_delete_trajectory_idempotent(isolated_cache):
    cc = isolated_cache
    cc._delete_trajectory("never-existed")
    cc._delete_trajectory("")  # also handles missing session_id


def test_vacuum_also_cleans_stale_trajectory_files(isolated_cache):
    cc = isolated_cache
    cc._append_trajectory("recent", {"event": "PreToolUse", "tool_name": "T"})
    cc._append_trajectory("stale", {"event": "PreToolUse", "tool_name": "T"})

    stale_path = cc._trajectory_file_for_session("stale")
    backdated = time.time() - (25 * 60 * 60)
    os.utime(stale_path, (backdated, backdated))

    vacuumed = cc._vacuum_old_sessions(max_age_secs=24 * 60 * 60)
    assert vacuumed == 1
    assert not stale_path.exists()
    assert cc._trajectory_file_for_session("recent").exists()


def test_read_skips_malformed_lines(isolated_cache):
    cc = isolated_cache
    path = cc._trajectory_file_for_session("session-bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"event": "PreToolUse", "tool_name": "ok"}\n'
        'not valid json line\n'
        '{"event": "PostToolUse", "tool_name": "ok2"}\n',
        encoding="utf-8",
    )
    entries = cc._read_trajectory("session-bad")
    assert len(entries) == 2
    assert entries[0]["tool_name"] == "ok"
    assert entries[1]["tool_name"] == "ok2"


# --- Per-session evaluator-warning dedup tests ---------------------------


def test_eval_warning_first_occurrence_marked(isolated_cache):
    cc = isolated_cache
    assert cc._has_emitted_warning("session-A", "key1") is False
    cc._record_emitted_warning("session-A", "key1")
    assert cc._has_emitted_warning("session-A", "key1") is True


def test_eval_warning_isolated_per_session(isolated_cache):
    cc = isolated_cache
    cc._record_emitted_warning("session-A", "key1")
    assert cc._has_emitted_warning("session-A", "key1") is True
    assert cc._has_emitted_warning("session-B", "key1") is False


def test_eval_warning_isolated_per_key(isolated_cache):
    cc = isolated_cache
    cc._record_emitted_warning("session-A", "key1")
    assert cc._has_emitted_warning("session-A", "key2") is False


def test_eval_warning_no_op_when_session_id_missing(isolated_cache):
    cc = isolated_cache
    cc._record_emitted_warning("", "key1")  # must not raise
    assert cc._has_emitted_warning("", "key1") is False


def test_eval_warnings_file_owner_only_perms(isolated_cache):
    cc = isolated_cache
    cc._record_emitted_warning("session-Z", "key1")
    path = cc._eval_warnings_file_for_session("session-Z")
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"{path} has mode {oct(mode)}, expected 0o600"


def test_surface_evaluator_failure_once_dedupes(isolated_cache, capsys):
    cc = isolated_cache
    cc._surface_evaluator_failure_once("session-X", "401 Unauthorized")
    cc._surface_evaluator_failure_once("session-X", "401 Unauthorized")
    cc._surface_evaluator_failure_once("session-X", "Connection refused")
    captured = capsys.readouterr()
    # Only the first call surfaces; subsequent same-session calls stay quiet.
    assert captured.err.count("[reflexes] WARNING:") == 1


def test_surface_evaluator_failure_once_distinct_sessions_each_get_one(isolated_cache, capsys):
    cc = isolated_cache
    cc._surface_evaluator_failure_once("session-X", "fail")
    cc._surface_evaluator_failure_once("session-Y", "fail")
    captured = capsys.readouterr()
    assert captured.err.count("[reflexes] WARNING:") == 2


def test_surface_without_session_id_emits_unconditionally(isolated_cache, capsys):
    cc = isolated_cache
    cc._surface_evaluator_failure_once("", "fail-1")
    cc._surface_evaluator_failure_once("", "fail-2")
    captured = capsys.readouterr()
    # No session_id → no dedup possible → both emit.
    assert captured.err.count("[reflexes] WARNING:") == 2


# --- Word-boundary trim ------------------------------------------------


def test_trim_at_word_boundary_short_passthrough(isolated_cache):
    cc = isolated_cache
    assert cc._trim_at_word_boundary("short string", 100) == "short string"


def test_trim_at_word_boundary_truncates_at_space(isolated_cache):
    cc = isolated_cache
    text = "the quick brown fox jumps over the lazy dog"
    out = cc._trim_at_word_boundary(text, 25)
    assert out.endswith("…"), out
    assert " " not in out[-2:]  # ellipsis isn't preceded by stray space
    # The truncation should land on a word boundary, not mid-word.
    body = out.rstrip("…")
    assert not body[-1].isalpha() or body in text  # body is a clean prefix
    assert text.startswith(body) or text.startswith(body.rstrip())


def test_trim_at_word_boundary_no_space_in_window_falls_back_to_hard_cut(isolated_cache):
    cc = isolated_cache
    # No spaces within the window → fall back to hard cut + ellipsis.
    text = "x" * 200
    out = cc._trim_at_word_boundary(text, 20)
    assert out.endswith("…")
    assert len(out) <= 21  # 20 chars + ellipsis


def test_trim_at_word_boundary(isolated_cache):
    """A long error string with `safe_acheck for fail-…` should NOT
    truncate mid-word like `safe_acheck for fa)`.
    """
    cc = isolated_cache
    text = "No evaluator configured. configure() failed (likely missing credentials or unreachable endpoint). Use safe_acheck for fail-open semantics."
    out = cc._trim_at_word_boundary(text, 120)
    # After truncation, the body should not end mid-word inside `safe_acheck`.
    body = out.rstrip("…").rstrip()
    assert not body.endswith("fa"), f"mid-word truncation regressed: {out!r}"


# --- Stop-cleanup invariant --------------------------------------------


def test_delete_eval_warnings_removes_file(isolated_cache):
    cc = isolated_cache
    cc._record_emitted_warning("session-D", "key1")
    path = cc._eval_warnings_file_for_session("session-D")
    assert path.exists()
    cc._delete_eval_warnings("session-D")
    assert not path.exists()


def test_delete_eval_warnings_idempotent(isolated_cache):
    cc = isolated_cache
    cc._delete_eval_warnings("never-existed")
    cc._delete_eval_warnings("")
