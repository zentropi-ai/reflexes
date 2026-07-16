"""Deterministic, fail-silent 'is there a newer version?' check.

This is the ONLY part of self-update that is code: it is simple, fast, runs
often, and must never raise or block the agent. The actual update is
agent-driven (see SKILL.md 'Updating Reflexes') — an agent handles the
environment-dependent work (git vs. bundle, sha256 verify, rollback) far more
robustly than a rigid script would.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# GitHub's "latest release" asset URL — stable, no extra hosting to set up.
DEFAULT_LATEST_URL = (
    "https://github.com/zentropi-ai/reflexes/releases/latest/download/latest.json"
)
CACHE_PATH = Path("~/.reflexes/.update-cache.json").expanduser()
CACHE_TTL_SECONDS = 24 * 3600

# Surfacing dedup: hosts show the "update available" notice at most once per this
# window per version, across every session/hook fire — so a long session (or a
# host whose SessionStart re-fires on resume/clear/compact) doesn't nag.
SURFACED_PATH = Path("~/.reflexes/.update-surfaced.json").expanduser()
SURFACE_TTL_SECONDS = 24 * 3600


def _version_key(v: str) -> tuple[int, ...]:
    """Comparable key for simple dotted versions like '0.2.0'. Ignores any
    non-numeric suffix per component (e.g. '0.2.0rc1' -> (0, 2, 0))."""
    out = []
    for chunk in str(v).split(".")[:4]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _read_cache() -> dict | None:
    try:
        data = json.loads(CACHE_PATH.read_text())
        if time.time() - float(data.get("checked_at", 0)) < CACHE_TTL_SECONDS:
            return data.get("latest")
    except Exception:
        pass
    return None


def _write_cache(latest: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({"checked_at": int(time.time()), "latest": latest}))
    except Exception:
        pass


def _fetch_latest(url: str, timeout: float) -> dict:
    import httpx

    r = httpx.get(url, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def check_for_update_status(
    installed: str, *, force: bool = False, timeout: float = 2.0
) -> tuple[str, str | None]:
    """Tri-state update check. Returns (status, notice):
      - ("available", <notice>) — a newer version exists.
      - ("current", None)       — confirmed up to date.
      - ("unreachable", None)   — couldn't fetch/parse the release info, so
                                  currency is UNKNOWN (never claim "up to date").
    Never raises. Uses a 24h cache unless force=True. Override the endpoint with
    REFLEXES_UPDATE_URL.
    """
    url = os.environ.get("REFLEXES_UPDATE_URL", DEFAULT_LATEST_URL)
    latest = None if force else _read_cache()
    if latest is None:
        try:
            latest = _fetch_latest(url, timeout)
            _write_cache(latest)
        except Exception:
            return ("unreachable", None)
    try:
        latest_ver = str(latest["version"])
    except Exception:
        return ("unreachable", None)
    if _version_key(latest_ver) > _version_key(installed):
        notes = latest.get("notes_url", "")
        tail = f" Release notes: {notes}" if notes else ""
        # Neutral, informational — no imperative. Callers frame it for their
        # audience (the doctor prints it to the user; the host hooks wrap it as
        # agent context). Keeps both the new and installed version + notes.
        return (
            "available",
            f"A newer Reflexes version ({latest_ver}) is available; "
            f"you're on {installed}.{tail}",
        )
    return ("current", None)


def check_for_update(installed: str, *, force: bool = False, timeout: float = 2.0) -> str | None:
    """Return a one-line 'update available' notice, or None.

    Fail-silent: None means either "up to date" OR "couldn't check" — callers
    that must tell those apart use check_for_update_status(). Uses a 24h cache
    so the common path does no network I/O; force=True bypasses it.
    """
    return check_for_update_status(installed, force=force, timeout=timeout)[1]


def update_status_line(installed: str) -> str:
    """A one-line human status string for `--check-update`. Distinguishes
    "available", "up to date", and "couldn't check" — critically, it never
    claims currency when the check actually FAILED. Always returns a string;
    never raises."""
    try:
        status, notice = check_for_update_status(installed, force=True)
    except Exception:
        status, notice = "unreachable", None
    if status == "available" and notice:
        return notice
    if status == "current":
        return f"Reflexes {installed} is up to date."
    return (
        f"Couldn't check for updates (offline or the update endpoint is "
        f"unavailable); you're on {installed}."
    )


def _latest_version() -> str:
    """Best-effort: the latest version from the update cache (empty if unknown)."""
    try:
        return str((_read_cache() or {}).get("version", ""))
    except Exception:
        return ""


def _surface_due(version: str) -> bool:
    """True at most once per SURFACE_TTL_SECONDS per version, persisted across
    sessions/hook fires. Records the surface on True. Fail-silent."""
    try:
        prev = json.loads(SURFACED_PATH.read_text())
        if prev.get("version") == version and (
            time.time() - float(prev.get("at", 0)) < SURFACE_TTL_SECONDS
        ):
            return False
    except Exception:
        pass  # missing/corrupt marker, or a different/newer version -> surface
    try:
        SURFACED_PATH.parent.mkdir(parents=True, exist_ok=True)
        SURFACED_PATH.write_text(json.dumps({"version": version, "at": int(time.time())}))
    except Exception:
        pass
    return True


def agent_update_notice(installed: str) -> str | None:
    """Agent-facing "update available" status note — framed as information to
    relay, not a command — surfaced **at most once per 24h per version** across
    every session and hook fire, or None. Fail-silent; never raises. Used by the
    host hooks (Claude Code / Codex SessionStart, Hermes pre_llm_call)."""
    try:
        notice = check_for_update(installed)
    except Exception:
        return None
    if not notice:
        return None
    if not _surface_due(_latest_version()):
        return None
    return (
        f"[Reflexes system status] {notice} You can let the user know; if they'd "
        f"like it, update via SKILL.md → Updating (fetch, verify the sha256, "
        f"back up, re-run the doctor). No need to act unless they want to."
    )
