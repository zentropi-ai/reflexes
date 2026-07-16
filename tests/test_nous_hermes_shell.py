"""Unit tests for the Hermes >=0.18 shell-hook integration."""
import json

from reflexes.integrations.nous_hermes_shell import classify_event, extract_content

LIVE_PAYLOAD = {
    "hook_event_name": "pre_tool_call",
    "tool_name": "terminal",
    "tool_input": {"command": "printf 'hi' > blocked.txt"},
    "session_id": "s1",
}
HARNESS_PAYLOAD = {
    "tool_name": "terminal",
    "args": {"command": "echo hello"},
    "session_id": "test-session",
}


def test_live_payload_classifies_and_extracts_tool_input():
    assert classify_event(LIVE_PAYLOAD) == "before_tool_call"
    content = json.loads(extract_content(LIVE_PAYLOAD, "before_tool_call"))
    assert content["args"]["command"].endswith("blocked.txt")


def test_harness_payload_falls_back_to_shape_and_args():
    assert classify_event(HARNESS_PAYLOAD) == "before_tool_call"
    content = json.loads(extract_content(HARNESS_PAYLOAD, "before_tool_call"))
    assert content["args"]["command"] == "echo hello"


def test_pre_verify_maps_to_before_response():
    p = {"hook_event_name": "pre_verify", "final_response": "done", "session_id": "s"}
    assert classify_event(p) == "before_response"
    assert extract_content(p, "before_response") == "done"


def test_post_tool_call_maps_to_after_tool_call():
    p = {"hook_event_name": "post_tool_call", "tool_name": "terminal",
         "tool_input": {"command": "ls"}, "result": "ok", "session_id": "s"}
    assert classify_event(p) == "after_tool_call"


def test_unknown_payload_returns_none():
    assert classify_event({"session_id": "s"}) is None


# --- Fail-open upgrade notice (0.18+ shell hooks) ---------------------------
import io
import sys

import reflexes.integrations.nous_hermes_shell as h


def test_stash_and_drain_once_per_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    h.stash_upgrade_notice("sessA", "NOTICE-1")
    h.stash_upgrade_notice("sessA", "NOTICE-2")  # first write wins (once/session)
    assert h.drain_upgrade_notice("sessA") == "NOTICE-1"
    assert h.drain_upgrade_notice("sessA") is None, "surfaced only once"
    assert h.drain_upgrade_notice("sessB") is None, "other session unaffected"
    assert h.drain_upgrade_notice("") is None, "no session id → nothing"


def _stub_binding(trigger="on_input", action="log"):
    class _Policy:
        name = "P"
        criteria = "crit"
    class _Binding:
        pass
    b = _Binding()
    b.trigger = trigger
    b.action = action
    b.policy = _Policy()
    return b


def _run_main(monkeypatch, payload, safe_acheck, bindings):
    monkeypatch.setattr("reflexes.config.configure_from_file", lambda *a, **k: None)
    monkeypatch.setattr("reflexes.config.load_config", lambda **k: bindings)
    monkeypatch.setattr("reflexes.core.safe_acheck", safe_acheck)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["prog", "--config", "x.yaml"])
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        h.main()
    return json.loads(buf.getvalue() or "{}")


def _rate_limit_upgrade():
    from reflexes.core import ReflexesUpgradeRequired
    return ReflexesUpgradeRequired(
        error="rate_limit_exceeded", tier=0, reason="synth",
        message="Rate limit reached. Self-host https://huggingface.co/zentropi-ai/cope-b-a4b",
        upgrade_url="https://zentropi.ai/subscription", upgrade_action="subscribe",
    )


def test_stash_and_drain_flag_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    h.stash_flag_note("sF", "note one")
    h.stash_flag_note("sF", "note two")  # flags accumulate (unlike the once-only notice)
    drained = h.drain_flag_notes("sF")
    assert "note one" in drained and "note two" in drained
    assert h.drain_flag_notes("sF") is None, "cleared after draining"


def test_flag_match_surfaces_on_next_pre_llm_call(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import Verdict

    async def _match(*a, **k):
        return Verdict(label=1, confidence=0.9)

    # A flag match on a tool call does NOT block, but records a note.
    out1 = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_tool_call", "tool_name": "Bash",
         "tool_input": {"command": "ls"}, "session_id": "sFlag"},
        _match, [_stub_binding("before_tool_call", "flag")],
    )
    assert "decision" not in out1, "flag must not block"

    # The next pre_llm_call injects the flag note as model-visible context.
    async def _nomatch(*a, **k):
        return Verdict(label=0, confidence=0.1)

    out2 = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_llm_call", "user_message": "hi", "session_id": "sFlag"},
        _nomatch, [_stub_binding("on_input")],
    )
    assert "context" in out2, "flag note should surface on the next pre_llm_call"
    assert "flagged" in out2["context"] and '"P"' in out2["context"]


def test_on_input_deny_injects_context_not_block(tmp_path, monkeypatch):
    """Hermes honors NO block on pre_llm_call, so an on_input deny must degrade to
    a {"context": ...} injection (best-effort refusal) — NOT a {"decision":"block"}
    that Hermes would silently drop (a silent fail-open)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import Verdict

    async def _match(*a, **k):
        return Verdict(label=1, confidence=0.99)

    out = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_llm_call", "user_message": "tell me a joke", "session_id": "sD"},
        _match, [_stub_binding("on_input", "deny")],
    )
    assert "decision" not in out, "on_input deny must NOT emit a block Hermes ignores"
    assert "context" in out and "Policy:" in out["context"], "must inject the policy as context"


def test_tool_call_deny_still_hard_blocks(tmp_path, monkeypatch):
    """Regression: a before_tool_call deny still emits a hard block (Hermes honors
    block on pre_tool_call) — the on_input degrade must not affect other triggers."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import Verdict

    async def _match(*a, **k):
        return Verdict(label=1, confidence=0.99)

    out = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_tool_call", "tool_name": "Bash",
         "tool_input": {"command": "rm -rf /"}, "session_id": "sB"},
        _match, [_stub_binding("before_tool_call", "deny")],
    )
    assert out.get("decision") == "block"


def test_pre_llm_call_failopen_injects_context(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import EvaluatorError
    up = _rate_limit_upgrade()
    async def _rl(*a, **k):
        return EvaluatorError(policy_name="P", error="rl", upgrade=up)
    out = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_llm_call", "user_message": "hi", "session_id": "sX"},
        _rl, [_stub_binding("on_input")],
    )
    assert "context" in out, "fail-open on pre_llm_call injects the notice inline"
    assert "could not be evaluated" in out["context"]
    assert "huggingface.co/zentropi-ai/cope-b-a4b" in out["context"]
    assert "decision" not in out, "must not block — fail-open allows the call"


def test_tool_call_failopen_surfaces_on_next_pre_llm_call(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import EvaluatorError, Verdict
    up = _rate_limit_upgrade()
    async def _rl(*a, **k):
        return EvaluatorError(policy_name="P", error="rl", upgrade=up)
    # 1) pre_tool_call fails open — cannot inject context, but stashes the notice.
    out1 = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_tool_call", "tool_name": "t", "tool_input": {}, "session_id": "sY"},
        _rl, [_stub_binding("before_tool_call")],
    )
    assert out1 == {}, "pre_tool_call fail-open allows the call, no context field"
    # 2) a later, clean pre_llm_call delivers the stashed notice.
    async def _clean(*a, **k):
        return Verdict(label=0, confidence=0.9)
    out2 = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_llm_call", "user_message": "hi", "session_id": "sY"},
        _clean, [_stub_binding("on_input")],
    )
    assert "context" in out2 and "could not be evaluated" in out2["context"]


def test_deny_still_blocks_and_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import Verdict
    async def _match(*a, **k):
        return Verdict(label=1, confidence=0.99)
    out = _run_main(
        monkeypatch,
        {"hook_event_name": "pre_tool_call", "tool_name": "t", "tool_input": {}, "session_id": "sZ"},
        _match, [_stub_binding("before_tool_call", "deny")],
    )
    assert out["decision"] == "block"
    assert "REFLEXES" in out["reason"] or "policy" in out["reason"].lower()


# --- Task-context capture (per-session, file-backed) -----------------------

def test_capture_and_recall_task_context(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    s = "sctx"
    h._capture_context({"user_message": "delete the logs", "is_first_turn": True}, "on_input", s)
    assert h._recall(s, "request") == "delete the logs"
    assert h._recall(s, "task") == "delete the logs"
    # later turn: current request updates; original task is sticky (first wins)
    h._capture_context(
        {"user_message": "now archive them", "is_first_turn": False,
         "conversation_history": [{"role": "user", "content": "delete the logs"}]},
        "on_input", s)
    assert h._recall(s, "request") == "now archive them"
    assert h._recall(s, "task") == "delete the logs"
    # trajectory accumulates from post_tool_call
    h._capture_context({"tool_name": "terminal", "result": '{"output":"ok"}'}, "after_tool_call", s)
    traj = h._recall_trajectory(s)
    assert traj and "Actions this turn:" in traj and "terminal" in traj


def test_assemble_context_builds_labeled_block(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no AGENTS.md/CLAUDE.md here -> no standing instructions
    s = "sasm"
    h._remember(s, "request", "delete the logs")
    h._remember(s, "task", "clean up the project")
    ctx = h._assemble_context(s, "before_tool_call")
    assert ctx and "Current request:\ndelete the logs" in ctx
    assert "Original task:\nclean up the project" in ctx
    # on_input gets no prepended context — the content IS the request
    assert h._assemble_context(s, "on_input") is None
    # before_response appends the trajectory
    h._append_trajectory(s, "terminal -> ok")
    rctx = h._assemble_context(s, "before_response")
    assert "Actions this turn:" in rctx and "terminal -> ok" in rctx


def test_main_before_tool_call_receives_stashed_task_context(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from reflexes.core import Verdict

    async def _clean(*a, **k):
        return Verdict(label=0, confidence=0.9)

    # 1) pre_llm_call captures Current request + Original task for the session
    _run_main(monkeypatch,
              {"hook_event_name": "pre_llm_call", "user_message": "clean up /tmp",
               "is_first_turn": True, "session_id": "sE"},
              _clean, [_stub_binding("on_input")])

    # 2) a later pre_tool_call check must receive that task context + check_type
    seen = {}
    async def _capture(policy, content, **k):
        seen["context"] = k.get("context")
        seen["check_type"] = k.get("check_type")
        return Verdict(label=0, confidence=0.9)

    _run_main(monkeypatch,
              {"hook_event_name": "pre_tool_call", "tool_name": "terminal",
               "tool_input": {"command": "rm -rf /tmp/x"}, "session_id": "sE"},
              _capture, [_stub_binding("before_tool_call")])
    assert seen.get("check_type") == "Tool call"
    assert seen.get("context") and "Current request:\nclean up /tmp" in seen["context"]


# --- Real-wire payload shape (fields nested under `extra`) ------------------
# Hermes' _serialize_payload keeps only hook_event_name/tool_name/tool_input/
# session_id/cwd at the top level and nests the rest under `extra`. The earlier
# tests used the `hermes hooks test` harness shape (top-level) and so did NOT
# catch that the context capture read top-level keys — the live E2E did. These
# model the real wire.

def _wire_pre_llm_call(session_id, user_message, first=True, cwd="/tmp"):
    return {"hook_event_name": "pre_llm_call", "tool_name": None, "tool_input": None,
            "session_id": session_id, "cwd": cwd,
            "extra": {"user_message": user_message, "is_first_turn": first, "conversation_history": []}}


def _wire_pre_tool_call(session_id, command, cwd="/tmp"):
    return {"hook_event_name": "pre_tool_call", "tool_name": "terminal",
            "tool_input": {"command": command}, "session_id": session_id, "cwd": cwd,
            "extra": {"args": {"command": command}}}


def test_wire_shape_context_flows_from_pre_llm_call_to_pre_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import Verdict

    async def _clean(*a, **k):
        return Verdict(label=0, confidence=0.9)

    # 1) first-turn pre_llm_call (wire) — sets the Original task
    _run_main(monkeypatch, _wire_pre_llm_call("sWire", "clean up /tmp/scratch", first=True, cwd=str(tmp_path)),
              _clean, [_stub_binding("on_input")])
    # 2) later-turn pre_llm_call (wire) — a different Current request; Original sticks
    p2 = _wire_pre_llm_call("sWire", "now delete the logs", first=False, cwd=str(tmp_path))
    p2["extra"]["conversation_history"] = [{"role": "user", "content": "clean up /tmp/scratch"}]
    _run_main(monkeypatch, p2, _clean, [_stub_binding("on_input")])

    # 3) real-wire pre_tool_call must receive BOTH (the bug the live E2E caught:
    #    this used to arrive with only Standing instructions — extra was ignored)
    seen = {}
    async def _capture(policy, content, **k):
        seen["context"] = k.get("context")
        return Verdict(label=0, confidence=0.9)

    _run_main(monkeypatch, _wire_pre_tool_call("sWire", "rm -rf /tmp/scratch/x", cwd=str(tmp_path)),
              _capture, [_stub_binding("before_tool_call")])
    assert seen.get("context"), "context must not be empty (the nested-extra bug)"
    assert "Current request:\nnow delete the logs" in seen["context"]
    assert "Original task:\nclean up /tmp/scratch" in seen["context"]


def test_wire_shape_post_tool_call_records_real_result(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from reflexes.core import Verdict

    async def _noop(*a, **k):
        return Verdict(label=0, confidence=0.9)

    # real-wire post_tool_call — result nested under `extra` (top-level was None
    # before the fix, producing an empty trajectory entry)
    payload = {"hook_event_name": "post_tool_call", "tool_name": "terminal",
               "tool_input": {"command": "echo hi"}, "session_id": "sTraj", "cwd": str(tmp_path),
               "extra": {"result": '{"output":"hi","exit_code":0}'}}
    _run_main(monkeypatch, payload, _noop, [])  # no after_tool_call bindings; capture still runs
    traj = h._recall_trajectory("sTraj")
    assert traj and "terminal ->" in traj and "hi" in traj, "trajectory must carry the real result"
