"""On-disk cache for hosted policy criteria.

Keyed by an immutable ``(scheme, locator, version)`` identity:

- **Pinned** refs (``@version`` / ``@sha``) are immutable → cached **permanently**
  (fetch once, never refresh).
- **Unpinned** refs (``latest`` / a branch) use a **TTL** with **write-through**
  and **fail-safe-to-stale**: serve the cached copy if fresh, otherwise fetch and
  update the entry; if a refetch errors but a cached copy exists, use the stale
  copy rather than disable enforcement. Fetches are bounded to ~once per TTL
  regardless of how often a policy fires.

TTL default is 1h (``POLICY_CACHE_TTL_SECONDS``), overridable via the
``REFLEXES_POLICY_CACHE_TTL`` env var — mirroring the ``update.py`` cache pattern.
Writes are atomic (temp file + ``os.replace``) because the subprocess-per-call
hosts each load bindings concurrently.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from reflexes.policy_ref import PolicyRef

logger = logging.getLogger("reflexes.policy_cache")

CACHE_DIR = Path("~/.reflexes/.policy-cache").expanduser()
POLICY_CACHE_TTL_SECONDS = 3600  # 1 hour


def _ttl_seconds() -> int:
    raw = os.environ.get("REFLEXES_POLICY_CACHE_TTL")
    if raw:
        try:
            n = int(raw)
            if n >= 0:
                return n
        except ValueError:
            pass
    return POLICY_CACHE_TTL_SECONDS


def _cache_path(ref: PolicyRef) -> Path:
    identity = f"{ref.scheme}:{ref.locator}@{ref.version or ''}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{ref.scheme}-{digest}.json"


def _read_entry(path: Path) -> dict | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(entry, dict) and isinstance(entry.get("criteria"), str):
            return entry
    except Exception:
        pass
    return None


def _write_entry(path: Path, entry: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)  # atomic on POSIX + Windows
    except Exception as e:  # caching is best-effort; never fail a check over it
        logger.debug("policy cache write failed for %s: %s", path.name, e)


def get_or_fetch(ref: PolicyRef, fetch: Callable[[], str]) -> str:
    """Return ``ref``'s criteria text, using and maintaining the cache.

    ``fetch`` is a zero-arg callable that performs the network fetch (raising on
    failure). Not called when a valid cache entry can be served.
    """
    # File refs are read from disk directly; nothing to cache here.
    from reflexes.policy_sources import CACHEABLE_SCHEMES
    if ref.scheme not in CACHEABLE_SCHEMES:
        return fetch()

    path = _cache_path(ref)
    entry = _read_entry(path)

    if ref.is_pinned:
        # Immutable → cache permanently.
        if entry is not None:
            return entry["criteria"]
        criteria = fetch()
        _write_entry(path, {"criteria": criteria})
        return criteria

    # Unpinned → TTL + write-through + fail-safe.
    now = time.time()
    if entry is not None and (now - float(entry.get("fetched_at", 0))) < _ttl_seconds():
        return entry["criteria"]
    try:
        criteria = fetch()
    except Exception:
        if entry is not None:
            logger.warning(
                "policy refetch failed for %s; serving cached copy", ref.raw
            )
            return entry["criteria"]
        raise
    _write_entry(path, {"criteria": criteria, "fetched_at": now})
    return criteria
