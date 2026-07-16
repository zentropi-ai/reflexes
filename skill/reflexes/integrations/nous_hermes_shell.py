"""NousResearch Hermes shell-hook integration (hermes-agent >= 0.18).

Hermes 0.18 replaced the importlib hook-pack convention with **shell
hooks** configured in ``~/.hermes/config.yaml``. Hermes defines ~25 hook events
(``hermes_cli/plugins.py`` ``VALID_HOOKS``), but a *shell* hook can
consequentially act on only three: ``pre_tool_call`` (block a tool call),
``pre_verify`` (nudge a response revise), and ``pre_llm_call`` (inject context).
Every other event's shell-hook return is discarded — observer-only::

    hooks:
      pre_tool_call:
        - command: "reflexes-hermes-hook --config /abs/path/to/reflexes.yaml"

The hook receives a JSON payload on stdin (``tool_name``, ``args``,
``session_id`` ...) and may print ``{"decision": "block", "reason": ...}``
to block the call before execution — which means Hermes now supports the
full Reflexes deny + self-remediation protocol, superseding the
observer-shape limitation of the legacy ``nous_hermes`` integration
(kept for pre-0.18 installs).

Wire it (add ``pre_llm_call`` so ``on_input`` policies run **and** so the
fail-open upgrade notice can reach the model — see below)::

    hooks:
      pre_tool_call:
        - command: "reflexes-hermes-hook --config /abs/path/to/reflexes.yaml"
      pre_verify:
        - command: "reflexes-hermes-hook --config /abs/path/to/reflexes.yaml"
      pre_llm_call:
        - command: "reflexes-hermes-hook --config /abs/path/to/reflexes.yaml"

.. warning::
   Hermes spawns hook commands with ``shell=False`` (argv via
   ``shlex.split``), so each command MUST be a **bare executable + args**.
   Do NOT use shell constructs — no ``cd``, ``&&``, ``|``, ``$(...)``, and no
   leading ``~``. They fail to spawn, and because Hermes treats a failed hook
   spawn as "no decision," the guardrail then **FAILS OPEN** (it silently does
   not run). Use an **absolute** config path (``~`` is not expanded by the
   shell here, though the hook also calls ``expanduser`` defensively).
   ``reflexes-hermes-hook`` is the console script installed with the package;
   equivalently use an absolute interpreter that has ``reflexes`` installed:
   ``/path/to/venv/bin/python -m reflexes.integrations.nous_hermes_shell
   --config /abs/path/to/reflexes.yaml``.

Event mapping: ``pre_tool_call`` -> ``before_tool_call``;
``pre_verify`` (final response about to be delivered) -> ``before_response``;
``post_tool_call`` -> ``after_tool_call``; ``pre_llm_call`` -> ``on_input``.

``on_input`` caveat: Hermes honors NO block directive on ``pre_llm_call`` — only
``{"context": ...}`` (verified against hermes-agent 0.18.2
``agent/shell_hooks.py``). So an ``on_input`` deny **fires and injects the policy
as model-visible context (best-effort refusal) but cannot hard-stop the turn**.
For a hard block on the user's request, use ``before_tool_call`` /
``before_response`` policies instead. (A hard input block would require a
*Python* ``pre_gateway_dispatch`` plugin on the gateway path — not this
shell-hook integration, and never on the CLI.)

``after_tool_call`` is also unsupported: Hermes' ``post_tool_call`` event *does*
exist, but a shell hook's return there is **discarded** (observer-only,
``model_tools.py``), and it fires *after* the tool has already run — so it can't
veto. Enforce at ``before_tool_call``.

Fail-open upgrade notice: when a check can't run for a tier/capability reason
(sustained rate limit / subscriber-only), it fails open, and a one-line notice
is recorded for the session and injected into the model's context on the next
``pre_llm_call`` via ``{"context": ...}`` (once per session) — so the agent
learns enforcement was degraded. This needs ``pre_llm_call`` wired (above);
``pre_tool_call`` / ``pre_verify`` can only block/allow, not inject context.
Live payloads carry ``hook_event_name`` + Claude-Code-style ``tool_input``;
the ``hermes hooks test`` harness (0.18.2) sends neither, so shape
inference and the ``args`` key are supported as fallbacks. Validated
live against hermes-agent 0.18.2 (gpt-5.4-nano agent): deny
blocked the tool call pre-execution and the policy text reached the
model; benign calls pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("reflexes.nous_hermes_shell")


# --- Fail-open upgrade notice (per-session, file-backed) --------------------
# Each hook fire is a fresh subprocess, so a fail-open on one event (e.g.
# pre_tool_call) can't inject model-visible text — only pre_llm_call can, via
# {"context": ...}. So we record the notice to a small per-session file when
# any check fails open, then deliver it on the next pre_llm_call (once per
# session). Mirrors the fail-open notice on the other Reflexes hosts.
_SURFACED = "\x00surfaced"  # sentinel written after the notice is delivered
_NOTICE_TTL_S = 24 * 3600


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "reflexes" / "hermes"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _notice_file(session_id: str) -> Path | None:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64]
    if not safe:
        return None
    return _cache_dir() / f"upgrade-{safe}.txt"


def stash_upgrade_notice(session_id: str, notice: str) -> None:
    """Record a pending fail-open notice for this session (first write wins), so
    a later pre_llm_call can inject it. Best-effort; never raises."""
    path = _notice_file(session_id)
    if path is None or path.exists():
        return
    try:
        path.write_text(notice)
        path.chmod(0o600)
    except OSError:
        pass


def drain_upgrade_notice(session_id: str) -> str | None:
    """Return this session's pending notice once, then mark it surfaced so it
    isn't repeated for the session. Best-effort; None if nothing pending."""
    path = _notice_file(session_id)
    if path is None or not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text or text == _SURFACED:
        return None
    try:
        path.write_text(_SURFACED)
    except OSError:
        pass
    return text


def _flag_file(session_id: str) -> Path | None:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64]
    if not safe:
        return None
    return _cache_dir() / f"flags-{safe}.txt"


def stash_flag_note(session_id: str, note: str) -> None:
    """Append a pending ``flag`` note for this session. Each hook fire is a fresh
    subprocess and only pre_llm_call can inject model-visible text, so flag notes
    (from pre_tool_call / post_tool_call / pre_verify) are recorded here and
    delivered on the next pre_llm_call — making ``flag`` agent-visible, not just
    logged. Best-effort; never raises."""
    path = _flag_file(session_id)
    if path is None:
        return
    try:
        with path.open("a") as f:
            f.write(note.rstrip("\n") + "\n")
        path.chmod(0o600)
    except OSError:
        pass


def drain_flag_notes(session_id: str) -> str | None:
    """Return this session's accumulated flag notes once, then clear them.
    Best-effort; None if nothing pending."""
    path = _flag_file(session_id)
    if path is None or not path.exists():
        return None
    try:
        text = path.read_text().strip()
        path.unlink()
    except OSError:
        return None
    return text or None


def _vacuum_notices() -> None:
    """Best-effort removal of stale per-session files (notices + context state)."""
    try:
        now = time.time()
        for p in _cache_dir().glob("*.txt"):
            try:
                if now - p.stat().st_mtime > _NOTICE_TTL_S:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


# --- Task-context capture (per-session, file-backed) -----------------------
# Each hook is a fresh subprocess, and pre_tool_call / pre_verify carry no user
# message — so we persist the pieces of the task context to per-session files
# and read them back on the enforcing hooks, matching the labeled context the
# other Reflexes hosts send (SPEC §5.1). Sources: pre_llm_call → Current request
# (+ first turn: Original task); post_tool_call → Actions this turn (trajectory);
# AGENTS.md/CLAUDE.md on disk → Standing instructions.
_TRAJECTORY_MAX = 50
_STANDING_MAX = 4000

_CHECK_TYPE = {
    "before_tool_call": "Tool call",
    "after_tool_call": "Tool result",
    "before_response": "Agent response",
    "on_input": "Content",
}


def _state_file(session_id: str, kind: str) -> Path | None:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64]
    if not safe:
        return None
    return _cache_dir() / f"{kind}-{safe}.txt"


def _remember(session_id: str, kind: str, value: str, first_wins: bool = False) -> None:
    """Persist a per-session context value. first_wins keeps the earliest write
    (used for Original task). Best-effort; never raises."""
    path = _state_file(session_id, kind)
    if path is None or not value:
        return
    if first_wins and path.exists():
        return
    try:
        path.write_text(value)
        path.chmod(0o600)
    except OSError:
        pass


def _recall(session_id: str, kind: str) -> str | None:
    path = _state_file(session_id, kind)
    if path is None or not path.exists():
        return None
    try:
        return path.read_text() or None
    except OSError:
        return None


def _append_trajectory(session_id: str, entry: str) -> None:
    path = _state_file(session_id, "trajectory")
    if path is None or not entry:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.replace("\n", " ") + "\n")
        path.chmod(0o600)
    except OSError:
        pass


def _recall_trajectory(session_id: str) -> str | None:
    """Render the recent trajectory as an "Actions this turn:" block. Session-
    scoped + capped (Hermes shell hooks give no clean per-turn boundary in
    pre_verify, so this is 'recent actions', last _TRAJECTORY_MAX entries)."""
    path = _state_file(session_id, "trajectory")
    if path is None or not path.exists():
        return None
    try:
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    lines = lines[-_TRAJECTORY_MAX:]
    return "Actions this turn:\n" + "\n".join(f"- {ln}" for ln in lines)


def _resolve_standing_instructions(cwd: str | None = None) -> str | None:
    """Best-effort AGENTS.md / CLAUDE.md from the agent's working dir. Hermes
    provides it as payload `cwd`; fall back to the subprocess cwd."""
    try:
        base = Path(cwd) if cwd else Path.cwd()
    except (OSError, TypeError):
        base = Path.cwd()
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            p = base / name
            if p.is_file():
                return (p.read_text()[:_STANDING_MAX]) or None
        except OSError:
            pass
    return None


def _capture_context(payload: dict, trigger: str, session_id: str) -> None:
    """Stash the task-context pieces this event carries, for later hooks.
    Runs unconditionally (even with no bindings for this trigger)."""
    if not session_id:
        return
    if trigger == "on_input":
        msg = payload.get("user_message")
        if msg:
            _remember(session_id, "request", str(msg))
            original = str(msg) if payload.get("is_first_turn") else _first_user_message(payload) or str(msg)
            _remember(session_id, "task", original, first_wins=True)
    elif trigger == "after_tool_call":
        tool = payload.get("tool_name", "tool")
        result = payload.get("result", "")
        result_s = json.dumps(result) if isinstance(result, (dict, list)) else str(result)
        _append_trajectory(session_id, f"{tool} -> {result_s[:120]}")


def _first_user_message(payload: dict) -> str | None:
    for m in payload.get("conversation_history") or []:
        if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
            return str(m["content"])
    return None


def _assemble_context(session_id: str, trigger: str, cwd: str | None = None) -> str | None:
    """Build the labeled task-context block for an enforcing check, via the
    shared assembler. on_input needs none (the content *is* the request)."""
    if not session_id or trigger == "on_input":
        return None
    from reflexes._context import assemble_task_context
    ctx = assemble_task_context(
        primary_user_prompt=_recall(session_id, "request"),
        initial_user_prompt=_recall(session_id, "task"),
        standing_instructions=_resolve_standing_instructions(cwd),
    ) or None
    if trigger == "before_response":
        traj = _recall_trajectory(session_id)
        if traj:
            ctx = f"{ctx}\n\n{traj}" if ctx else traj
    return ctx


_EVENT_TO_TRIGGER = {
    "pre_tool_call": "before_tool_call",
    "post_tool_call": "after_tool_call",
    "pre_verify": "before_response",
    "pre_llm_call": "on_input",
}


def classify_event(payload: dict) -> str | None:
    """Map the Hermes event to a reflexes trigger.

    Live payloads carry an explicit ``hook_event_name``; the ``hermes
    hooks test`` harness payloads (as of 0.18.2) do not, so fall back to
    payload-shape inference for those.
    """
    explicit = payload.get("hook_event_name")
    if explicit in _EVENT_TO_TRIGGER:
        return _EVENT_TO_TRIGGER[explicit]
    if "tool_name" in payload and "result" in payload:
        return "after_tool_call"
    if "tool_name" in payload:
        return "before_tool_call"
    if "final_response" in payload:
        return "before_response"
    if "user_message" in payload:
        return "on_input"
    return None


def extract_content(payload: dict, trigger: str) -> str | None:
    if trigger in ("before_tool_call", "after_tool_call"):
        # Same JSON-object shape as the Claude Code integration (SPEC §3.3).
        # Live Hermes sends Claude-Code-style ``tool_input``; the
        # ``hermes hooks test`` harness sends ``args``. Accept both.
        tool_args = payload.get("tool_input", payload.get("args", {}))
        return json.dumps({"tool_name": payload.get("tool_name", ""), "args": tool_args})
    if trigger == "before_response":
        return payload.get("final_response")
    if trigger == "on_input":
        return payload.get("user_message")
    return None


def main(host: str = "hermes") -> None:
    parser = argparse.ArgumentParser(description="Reflexes Hermes shell hook")
    parser.add_argument("--config", default="reflexes.yaml")
    parser.add_argument("--reflexes", default="", help="Comma-separated group names")
    args = parser.parse_args()
    # Hermes spawns hooks with shell=False, so a leading ``~`` in --config is
    # not shell-expanded; expand it here so an absolute-or-tilde path both work.
    args.config = os.path.expanduser(args.config)

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from reflexes.config import load_config, configure_from_file
    from reflexes.core import safe_acheck, EvaluatorError, build_feedback, format_upgrade_notice
    from reflexes.observability import configure_logging

    # Tag log lines with the host; Hermes logs to stderr (no shared file), but
    # the tag keeps json/aggregated logs attributable. force=True so it wins
    # over the generic (host-less) call inside configure_from_file.
    configure_logging(args.config, host=host, stream=sys.stderr, force=True)
    configure_from_file(args.config)
    _vacuum_notices()

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print(json.dumps({}))
        return

    # Hermes' shell-hook serializer (agent/shell_hooks.py::_serialize_payload)
    # keeps only hook_event_name / tool_name / tool_input / session_id / cwd at
    # the top level and nests EVERYTHING else (user_message, is_first_turn,
    # conversation_history, result, final_response, raw args) under `extra`.
    # Surface those so the accessors below find them. (The `hermes hooks test`
    # harness sends fields top-level with no `extra`, so this is a no-op there.)
    _extra = payload.get("extra")
    if isinstance(_extra, dict):
        payload = {**_extra, **payload}

    trigger = classify_event(payload)
    session_id = str(payload.get("session_id") or "")
    # Capture the task-context pieces this event carries, for later hooks — even
    # when there are no bindings for this trigger (pre_llm_call stashes the
    # current request + original task; post_tool_call appends the trajectory).
    if trigger:
        _capture_context(payload, trigger, session_id)

    content = extract_content(payload, trigger) if trigger else None
    if not trigger or not content:
        print(json.dumps({}))
        return

    groups = [g.strip() for g in args.reflexes.split(",") if g.strip()]
    try:
        bindings = [
            b
            for b in load_config(reflexes=groups, config_file=args.config)
            if b.trigger == trigger
        ]
    except Exception as e:  # broken config must not crash the agent — but say so
        logger.warning("Failed to load config: %s", e)
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": f"reflexes config failed to load ({e}); checks failing open",
                }
            )
        )
        return

    # Evaluate all matching policies in parallel (cap: max_concurrent(),
    # default 10, override REFLEXES_MAX_CONCURRENT), then apply results in
    # binding order — first deny wins.
    from reflexes.core import max_concurrent

    # Prepend the labeled task context (Current request / Original task /
    # Standing instructions [+ Actions this turn on before_response]) so the
    # evaluator sees the same enrichment as the other hosts. on_input gets none
    # (the content IS the request).
    task_context = _assemble_context(session_id, trigger, payload.get("cwd"))
    check_type = _CHECK_TYPE.get(trigger, "Content")
    context_label = "Task context" if task_context else "User request"

    async def _evaluate_all():
        sem = asyncio.Semaphore(max_concurrent())

        async def _eval_one(binding):
            async with sem:
                return binding, await safe_acheck(
                    binding.policy.criteria, content,
                    policy_name=binding.policy.name,
                    context=task_context, check_type=check_type,
                    context_label=context_label,
                )

        return await asyncio.gather(*[_eval_one(b) for b in bindings])

    results = asyncio.run(_evaluate_all())

    upgrade_notice: str | None = None
    flag_notes: list[str] = []
    for binding, verdict in results:
        # safe_acheck returns EvaluatorError (not raises) on failure — skip,
        # fail-open per SPEC §4.
        if isinstance(verdict, EvaluatorError):
            if upgrade_notice is None and getattr(verdict, "upgrade", None) is not None:
                # Tier/capability failure (rate limit / subscriber-only): build
                # the agent-facing notice so the model learns enforcement was
                # degraded. Delivered via pre_llm_call {"context"} below.
                upgrade_notice = format_upgrade_notice(verdict.upgrade)
            logger.warning("Evaluator error (fail-open): %s", verdict.error[:80])
            continue
        if verdict is None or verdict.label != 1:
            continue
        if binding.action == "deny":
            feedback = build_feedback(trigger, binding.policy.criteria)
            if trigger == "on_input":
                # Hermes honors NO block directive on pre_llm_call — only
                # {"context": ...}. A deny can't hard-stop the turn here, so
                # inject the policy as model-visible context (best-effort refusal)
                # rather than emit a block Hermes would silently drop (fail-open).
                print(json.dumps({"context": feedback}))
            else:
                print(json.dumps({"decision": "block", "reason": feedback}))
            return
        logger.warning(
            "reflexes %s: %s matched policy %s", binding.action, trigger, binding.policy.name
        )
        if binding.action == "flag":
            conf = f" (confidence {verdict.confidence:.2f})" if verdict.confidence is not None else ""
            flag_notes.append(
                f'[Reflexes] The "{binding.policy.name}" policy flagged this turn{conf}. '
                f"It was allowed, not blocked — take the flag into account going forward."
            )

    # Record a fail-open notice for the session so a pre_llm_call can inject it.
    if upgrade_notice and session_id:
        stash_upgrade_notice(session_id, upgrade_notice)
    # Record flag notes so the next pre_llm_call surfaces them to the model.
    if flag_notes and session_id:
        for note in flag_notes:
            stash_flag_note(session_id, note)

    # pre_llm_call (on_input) is the only Hermes hook that can inject
    # model-visible context. Deliver the pending notice there, once per session,
    # while letting the call proceed (fail-open). Without a session_id we can
    # still deliver a notice raised on this same pre_llm_call.
    if trigger == "on_input":
        parts: list[str] = []
        notice = drain_upgrade_notice(session_id) if session_id else upgrade_notice
        if notice:
            parts.append(notice)
        flags = drain_flag_notes(session_id) if session_id else ("\n".join(flag_notes) or None)
        if flags:
            parts.append(flags)
        # Surface an "update available" notice (deduped in reflexes.update to at
        # most once per 24h per version, across sessions + pre_llm_call fires).
        try:
            from reflexes import __version__
            from reflexes.update import agent_update_notice
            update = agent_update_notice(__version__)
        except Exception:
            update = None
        if update:
            parts.append(update)
        if parts:
            print(json.dumps({"context": "\n\n".join(parts)}))
            return

    print(json.dumps({}))


if __name__ == "__main__":
    main()
