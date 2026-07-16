"""`reflexes doctor` — post-install verification.

Exists because the most dangerous failure mode of a policy layer is
SILENT FAIL-OPEN: a config typo, an unreadable policy file, or an
unreachable evaluator each degrade to "every check passes" (fail-open
is normative per SPEC §4). Doctor makes that state visible on demand.

Checks:
  1. config      — reflexes.yaml found, parses, policies load
  2. bindings    — at least one binding; triggers/actions valid
  3. evaluator   — configured, credentialed, and answers a live probe
  4. hosts       — which agent hosts on this machine have Reflexes wired
Exit code: 0 all good, 1 warnings only, 2 failures.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

_OK, _WARN, _FAIL = "ok", "warn", "fail"


def _find_config(explicit: str | None) -> Path | None:
    candidates = (
        [Path(explicit)] if explicit else [
            Path.home() / ".reflexes" / "reflexes.yaml",
            Path.cwd() / "reflexes.yaml",
        ]
    )
    for p in candidates:
        if p.is_file():
            return p
    return None


def _check_config(config_path: Path | None, groups: list[str]) -> tuple[str, str, list]:
    if config_path is None:
        return (_FAIL, "no reflexes.yaml found (~/.reflexes/reflexes.yaml or ./reflexes.yaml)", [])
    from reflexes.config import load_config

    try:
        bindings = load_config(reflexes=groups, config_file=config_path)
    except Exception as e:
        return (_FAIL, f"{config_path}: failed to load — {e} (all checks would fail open!)", [])
    if not bindings:
        return (_WARN, f"{config_path}: parses, but no bindings in selected groups", [])
    summary = ", ".join(f"{b.policy.name}@{b.trigger}:{b.action}" for b in bindings)
    return (_OK, f"{config_path}: {len(bindings)} binding(s) — {summary}", bindings)


def _check_logging(config_path: Path | None) -> tuple[str, str]:
    """Report the diagnostic logging posture. `debug` is a WARN: it captures
    message CONTENT to disk, which is the privacy footgun the WARNING default
    exists to avoid. Other levels are informational."""
    if config_path is None:
        return (_OK, "default (warning) — enforcement events only, no content")
    from reflexes.config import read_log_settings
    level, fmt = read_log_settings(config_path)
    fmt_note = " (json)" if fmt == "json" else ""
    if level == "debug":
        return (_WARN, f"level=debug{fmt_note} — message CONTENT is being written to the log; use only for troubleshooting")
    if level in ("off", "none"):
        return (_OK, f"disabled{fmt_note} — nothing logged (including fail-open events)")
    return (_OK, f"level={level}{fmt_note} — enforcement events only, no content")


def _check_evaluator(config_path: Path | None) -> tuple[str, str]:
    if config_path is None:
        return (_FAIL, "skipped (no config)")
    import reflexes.core as core
    from reflexes.config import configure_from_file
    from reflexes.core import safe_acheck, EvaluatorError

    configure_from_file(str(config_path))
    if core._evaluator is None:
        # Surface the specific cause (e.g. "Anthropic API key required. Set
        # ANTHROPIC_API_KEY…") that configure() stashed, not just a generic line.
        reason = core._evaluator_error or "check evaluators: section + API key env"
        return (_FAIL, f"evaluator failed to configure — {reason}")
    from reflexes.evaluators.zentropi import ZentropiEvaluator
    if isinstance(core._evaluator, ZentropiEvaluator) and not core._evaluator.api_key:
        return (
            _WARN,
            "no ZENTROPI_API_KEY set — the default Zentropi CoPE evaluator "
            "needs one, so policies will NOT fire yet. Get a free key at "
            "https://zentropi.ai and put it where this host picks up environment "
            "variables so it's visible here (e.g. Codex reads `~/.codex/.env`) — "
            "not in a shared config file — or swap to an evaluator you already "
            "have a key for (see 'Swap the Evaluator'). Re-run this "
            "doctor afterward to confirm enforcement is live.",
        )
    probe_criteria = "Label = 1 when the content contains the word REFLEXES_DOCTOR_PROBE.\nLabel = 0 otherwise."
    t0 = time.monotonic()
    try:
        verdict = asyncio.run(safe_acheck(probe_criteria, "REFLEXES_DOCTOR_PROBE"))
    except EvaluatorError as e:
        return (_FAIL, f"evaluator unreachable: {e}")
    ms = (time.monotonic() - t0) * 1000
    # safe_acheck RETURNS an EvaluatorError (it doesn't raise) when the probe
    # fails — e.g. a transient API error. Treat that as unreachable (fail-open
    # risk), not a crash.
    if isinstance(verdict, EvaluatorError):
        return (_FAIL, f"evaluator unreachable: {verdict.error}")
    if verdict is None:
        return (_FAIL, "evaluator returned no verdict (would fail open at runtime)")
    if verdict.label != 1:
        return (_WARN, f"evaluator reachable ({ms:.0f}ms) but probe verdict unexpected (label={verdict.label})")
    return (_OK, f"evaluator reachable, live probe verdict correct ({ms:.0f}ms)")


def _codex_hooks_enabled(config_toml: Path) -> bool | None:
    """Is `hooks = true` set under the [features] table in Codex's config.toml?
    Returns True/False, or None if the file can't be read. A bare top-level
    `hooks = true` does NOT count — Codex ignores it."""
    try:
        text = config_toml.read_text()
    except OSError:
        return None
    try:
        import tomllib  # 3.11+
        return tomllib.loads(text).get("features", {}).get("hooks") is True
    except ModuleNotFoundError:
        pass  # 3.10 — fall back to a text scan
    except Exception:
        return None
    in_features = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_features = s == "[features]"
        elif in_features and s.replace(" ", "").startswith("hooks=true"):
            return True
    return False


def _check_hosts() -> list[tuple[str, str, str]]:
    """Best-effort detection of Reflexes wiring in known agent hosts."""
    results = []
    home = Path.home()

    def _wired(path: Path, needle: str = "reflexes") -> bool:
        try:
            return needle in path.read_text()
        except OSError:
            return False

    # Honor the hosts' config-dir env vars so `doctor` inspects the SAME
    # location the host actually reads (isolated installs use these).
    cc_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    cc = cc_dir / "settings.json"
    if cc.exists():
        results.append(("Claude Code", _OK if _wired(cc) else _WARN,
                        f"hooks wired in {cc}" if _wired(cc)
                        else f"installed but no reflexes hooks in {cc} — ask your agent to 'install reflexes'"))
    cx_dir = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else home / ".codex"
    cx = cx_dir / "hooks.json"
    if cx.exists():
        if not _wired(cx):
            results.append(("OpenAI Codex", _WARN, f"installed but no reflexes hooks in {cx}"))
        else:
            # hooks.json alone is NOT enough. Codex loads hooks only when
            # `[features] hooks = true` is set, AND the user has TRUSTED the exact
            # hook definitions (`/hooks`).
            # We can inspect the first; we CANNOT see Codex's trust state — so we
            # never claim "enforcing" for Codex, only "configured", and warn.
            enabled = _codex_hooks_enabled(cx_dir / "config.toml")
            if enabled is False:
                results.append(("OpenAI Codex", _WARN,
                                f"hooks wired in {cx} but ~/.codex/config.toml is missing "
                                "`[features]`→`hooks = true` — Codex will IGNORE the hooks and "
                                "NOT enforce. Add it under the [features] table."))
            elif enabled is None:
                results.append(("OpenAI Codex", _WARN,
                                f"hooks wired in {cx} but couldn't read ~/.codex/config.toml to "
                                "confirm `[features] hooks = true` (Codex needs it to load hooks)."))
            else:  # configured + enabled — but trust/enforcement can't be verified here
                results.append(("OpenAI Codex", _WARN,
                                f"hooks wired in {cx} and `[features] hooks` enabled — but Codex "
                                "also requires you to TRUST command hooks, which can't be verified "
                                "from here. In Codex, run `/hooks`, trust the Reflexes definitions, "
                                "restart, then confirm a known policy actually fires. (Until then, "
                                "untrusted hooks are silently skipped — fail-open. Editing a hook "
                                "re-requires trust.)"))
    hm = home / ".hermes" / "config.yaml"
    if (home / ".hermes").exists():
        wired = hm.exists() and _wired(hm)
        results.append(("Hermes", _OK if wired else _WARN,
                        "shell hook wired in ~/.hermes/config.yaml (approve it if you haven't)" if wired
                        else "installed but no reflexes hook in ~/.hermes/config.yaml"))
    oc = home / ".openclaw"
    if oc.exists():
        wired = any(oc.glob("extensions/*reflexes*")) or any(oc.glob("extensions/reflexes*"))
        results.append(("OpenClaw", _OK if wired else _WARN,
                        "plugin installed under ~/.openclaw/extensions" if wired
                        else "installed but reflexes plugin not found — `openclaw plugins install @zentropi/reflexes-openclaw`"))
    if not results:
        results.append(("hosts", _WARN, "no known agent hosts detected (Claude Code / Codex / Hermes / OpenClaw)"))
    return results


def run_doctor(
    config: str | None = None,
    groups: list[str] | None = None,
    check_update: bool = False,
) -> int:
    groups = groups or []
    icons = {_OK: "✓", _WARN: "!", _FAIL: "✗"}
    worst = _OK

    def emit(status: str, area: str, msg: str) -> None:
        nonlocal worst
        order = [_OK, _WARN, _FAIL]
        if order.index(status) > order.index(worst):
            worst = status
        print(f"  {icons[status]} {area:<10} {msg}")

    print("reflexes doctor\n")
    config_path = _find_config(config)
    # Global kill switch: if `enabled: false`, say so LOUDLY and stop — a disabled
    # safety layer must never read as "all good" (that's the whole point of the
    # doctor). Distinct from a fail-open: this is off on purpose.
    from reflexes.config import is_enabled
    if config_path is not None and not is_enabled(config_path):
        print(f"  ⚠  REFLEXES IS DISABLED — `enabled: false` in {config_path}.")
        print("     NO policies are being enforced. Set `enabled: true` (or remove")
        print("     the line) and restart the host to re-enable.")
        print("\nDISABLED — enforcement is off by config.")
        return 1
    status, msg, _bindings = _check_config(config_path, groups)
    emit(status, "config", msg)
    ev_status, ev_msg = _check_evaluator(config_path)
    emit(ev_status, "evaluator", ev_msg)
    lstatus, lmsg = _check_logging(config_path)
    emit(lstatus, "logging", lmsg)
    for host, hstatus, hmsg in _check_hosts():
        emit(hstatus, host, hmsg)

    print()
    if worst == _FAIL:
        print("FAIL — at least one check would leave you unprotected (fail-open).")
        rc = 2
    elif worst == _WARN:
        print("OK with warnings.")
        rc = 1
    else:
        print("All checks passed.")
        rc = 0

    # Fail-silent, cached update notice — never affects the exit code.
    try:
        from reflexes import __version__
        from reflexes.update import check_for_update
        notice = check_for_update(__version__, force=check_update)
        if notice:
            print(f"\n  ↑ {notice}")
    except Exception:
        pass
    return rc
