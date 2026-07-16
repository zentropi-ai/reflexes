"""Policy sources — resolve a `PolicyRef` to its criteria text.

One function per scheme, dispatched by `fetch_criteria`. Every source returns
the raw policy document (criteria markdown); the caller (`document.Policy`)
strips frontmatter and caches per `reflexes.policy_cache`.

Schemes:
  - ``file``     — read a local markdown file (no cache; resolved eagerly at
                   config load so a missing file fails fast, as before).
  - ``zentropi`` — fetch a deployed labeler's criteria from the Zentropi Build
                   API (``versions get`` for a pinned version, or the latest
                   deployed version for an unpinned ref). Reuses
                   ``ZENTROPI_API_KEY``.
  - ``github``   — GET a raw file from a **public** repo via
                   ``raw.githubusercontent.com`` (public repos only).

Network fetches retry a few times on transient failures (see ``_get``); terminal
errors (not-found, auth) fail fast. There is intentionally no
``http:``/arbitrary-URL source (SSRF surface): only these two constrained hosts
(the Zentropi API and raw.githubusercontent.com) are ever contacted.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

from reflexes.policy_ref import PolicyRef

logger = logging.getLogger("reflexes.policy_sources")

# Zentropi *Build* API (labeler management/criteria) — distinct from the *Run*
# API (``api.zentropi.ai``) the evaluator posts inference to. Overridable to
# mirror the zentropi-labeler CLI's ``ZENTROPI_BUILD_API_BASE``.
DEFAULT_ZENTROPI_BUILD_API_BASE = "https://zentropi.ai"

_FETCH_TIMEOUT = 15.0

# A cold fetch (nothing cached yet) shouldn't fail on a transient blip, so retry
# a few times on network errors and transient server statuses. Terminal statuses
# (2xx, 404 not-found, 401/403 auth) return immediately — retrying can't help.
# (Once cached, the cache serves subsequent reads; unpinned refetch failures fall
# back to the stale copy in ``policy_cache``.)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_DELAYS = (0.3, 1.0)  # sleeps before retry 2 and retry 3 (3 attempts total)


def _get(url: str, headers: dict[str, str]) -> httpx.Response:
    """HTTP GET with a few retries on transient failures.

    Returns the response for the caller to interpret (including terminal 4xx). On
    a persistent transient status, returns the last response (the caller's status
    check raises). Raises the last exception only if every attempt was a network
    error.
    """
    last_exc: Exception | None = None
    resp: httpx.Response | None = None
    attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
        except httpx.HTTPError as e:
            last_exc = e
            resp = None
        else:
            if resp.status_code not in _RETRYABLE_STATUS:
                return resp  # success or a terminal error — done
        if attempt < attempts - 1:
            logger.debug("policy fetch retry %d for %s", attempt + 1, url)
            time.sleep(_RETRY_DELAYS[attempt])
    if resp is not None:
        return resp  # exhausted on a retryable status; caller will raise on it
    raise last_exc  # every attempt was a network error


class PolicyNotFound(Exception):
    """A hosted policy reference could not be resolved (e.g. a 404 for the
    labeler/version, or a repo path that doesn't exist).

    This is a *configuration* error — a typo or a deleted policy — and is
    treated as fail-closed/loud, distinct from a transient evaluator outage.
    (A missing local *file* raises ``FileNotFoundError``, as it always has.)
    """

    def __init__(self, ref: PolicyRef, detail: str = ""):
        self.ref = ref
        msg = f"Could not resolve policy: {ref.raw}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


# --------------------------------------------------------------------------- #
# file
# --------------------------------------------------------------------------- #

def file_fetch(ref: PolicyRef, base_dir: Path | None = None) -> tuple[str, str]:
    """Read a local policy file. Returns ``(text, source_path)``.

    Resolves ``ref.locator`` relative to ``base_dir`` (the config directory),
    falling back to the current working directory — matching the historical
    ``load_policy`` behavior. Raises ``FileNotFoundError`` if absent.
    """
    base = base_dir or Path.cwd()
    given = Path(ref.locator)
    path = given if given.is_absolute() else base / given
    if path.exists():
        return path.read_text(), str(path)
    if base_dir and given.exists():  # also try relative to cwd if different
        return given.read_text(), ref.locator
    raise FileNotFoundError(f"Could not find policy: {ref.locator}")


# --------------------------------------------------------------------------- #
# zentropi
# --------------------------------------------------------------------------- #

def _zentropi_build_base() -> str:
    return os.environ.get(
        "ZENTROPI_BUILD_API_BASE", DEFAULT_ZENTROPI_BUILD_API_BASE
    ).rstrip("/")


def _zentropi_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    key = os.environ.get("ZENTROPI_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _zentropi_check_status(ref: PolicyRef, resp: httpx.Response) -> None:
    if resp.status_code == 404:
        raise PolicyNotFound(ref, "labeler/version not found (404)")
    if resp.status_code in (401, 403):
        # Auth/authorization is a config error, not a transient outage — fail
        # closed and loud so the user fixes ZENTROPI_API_KEY / access.
        raise PermissionError(
            f"Zentropi denied access fetching policy {ref.raw} "
            f"(HTTP {resp.status_code}). Check ZENTROPI_API_KEY and that the "
            f"labeler is accessible to your account."
        )
    resp.raise_for_status()


def zentropi_fetch(ref: PolicyRef) -> str:
    """Fetch a Zentropi labeler's criteria text.

    Pinned (``@version``) → GET that exact version. Unpinned → list versions and
    use the latest *deployed* one (what production inference resolves to).
    """
    base = _zentropi_build_base()
    labeler_id = ref.locator
    headers = _zentropi_headers()

    if ref.version is not None:
        resp = _get(f"{base}/v1/labelers/{labeler_id}/versions/{ref.version}", headers)
        _zentropi_check_status(ref, resp)
        criteria = (resp.json() or {}).get("criteria_text")
    else:
        resp = _get(f"{base}/v1/labelers/{labeler_id}/versions", headers)
        _zentropi_check_status(ref, resp)
        versions = resp.json() or []
        deployed = [v for v in versions if v.get("is_deployed")]
        if not deployed:
            raise PolicyNotFound(
                ref, "labeler has no deployed version (deploy one first)"
            )
        latest = max(deployed, key=lambda v: v.get("created_at") or "")
        criteria = latest.get("criteria_text")

    if not criteria:
        raise PolicyNotFound(ref, "resolved version has empty criteria")
    return criteria


# --------------------------------------------------------------------------- #
# github
# --------------------------------------------------------------------------- #

def github_fetch(ref: PolicyRef) -> str:
    """Fetch a policy file from a **public** GitHub repo via
    raw.githubusercontent.com.

    ``github:<owner>/<repo>/<path...>[@<git-ref>]``; unpinned → ``HEAD`` (the
    default branch). Public repos only — a private repo returns 404/403 and
    fails as not-found/denied.
    """
    parts = ref.locator.split("/", 2)
    if len(parts) < 3 or not all(parts):
        raise PolicyNotFound(
            ref, "expected github:<owner>/<repo>/<path-to-file>"
        )
    owner, repo, path = parts
    git_ref = ref.version or "HEAD"
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{git_ref}/{path}"

    resp = _get(url, {})
    if resp.status_code == 404:
        raise PolicyNotFound(ref, "file not found on GitHub (404)")
    if resp.status_code in (401, 403):
        raise PermissionError(
            f"GitHub denied access fetching policy {ref.raw} "
            f"(HTTP {resp.status_code}). Only public repos are supported."
        )
    resp.raise_for_status()
    text = resp.text

    if not text.strip():
        raise PolicyNotFound(ref, "GitHub file is empty")
    return text


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

# Schemes whose fetch is worth caching (network-backed). ``file`` reads disk
# directly and is never cached.
CACHEABLE_SCHEMES = frozenset({"zentropi", "github"})


def fetch_criteria(ref: PolicyRef, base_dir: Path | None = None) -> str:
    """Resolve ``ref`` to its raw criteria text (frontmatter not yet stripped)."""
    if ref.scheme == "file":
        text, _ = file_fetch(ref, base_dir)
        return text
    if ref.scheme == "zentropi":
        return zentropi_fetch(ref)
    if ref.scheme == "github":
        return github_fetch(ref)
    raise ValueError(f"Unknown policy scheme: {ref.scheme!r} (in {ref.raw!r})")
