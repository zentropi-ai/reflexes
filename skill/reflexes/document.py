from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from reflexes.policy_ref import PolicyRef, parse_ref


# A leading YAML frontmatter block, if present, is optional metadata — strip it
# so it isn't sent to the evaluator. Reflexes doesn't require or read it.
_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n.*?\n---\s*\n", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


@dataclass
class Policy:
    """A policy is CoPE criteria plus an identity.

    The criteria is the whole document (frontmatter stripped) sent to the
    evaluator. For a local ``file`` policy the criteria is read eagerly at
    resolution; for a hosted policy (``zentropi:``/``github:``) it is fetched
    lazily on first ``.criteria`` access and cached (see ``policy_cache`` /
    ``policy_sources``). The ``name`` is a required binding field; triggers and
    actions are binding config (reflexes.yaml), not part of the policy.
    """

    name: str
    ref: PolicyRef
    base_dir: Path | None = None
    source_path: str | None = None
    _criteria: str | None = None

    @property
    def criteria(self) -> str:
        if self._criteria is None:
            from reflexes import policy_cache, policy_sources
            raw = policy_cache.get_or_fetch(
                self.ref,
                lambda: policy_sources.fetch_criteria(self.ref, self.base_dir),
            )
            self._criteria = _strip_frontmatter(raw)
        return self._criteria


def parse(text: str, source_path: str | None = None) -> Policy:
    """Parse a CoPE policy document from text. The entire document IS the
    criteria (a CoPE policy is its own criteria text); the name is the file
    basename. Used for local/inline documents."""
    name = Path(source_path).stem if source_path else "policy"
    ref = PolicyRef(
        scheme="file", locator=source_path or "", version=None,
        raw=source_path or "policy",
    )
    return Policy(
        name=name, ref=ref, source_path=source_path,
        _criteria=_strip_frontmatter(text),
    )


def resolve_policy(ref_str: str, name: str, base_dir: Path | None = None) -> Policy:
    """Resolve a binding's ``policy:`` value + its (required) ``name`` into a
    `Policy`.

    Local ``file`` refs are read eagerly here (fail-fast at config load if the
    file is missing, matching prior behavior). Hosted refs are left lazy — their
    criteria is fetched on first ``.criteria`` access and cached.
    """
    ref = parse_ref(ref_str)
    policy = Policy(name=name, ref=ref, base_dir=base_dir)
    if ref.scheme == "file":
        from reflexes import policy_sources
        text, src = policy_sources.file_fetch(ref, base_dir)
        policy._criteria = _strip_frontmatter(text)
        policy.source_path = src
    return policy


def load_policy(ref: str, base_dir: Path | None = None) -> Policy:
    """Load a policy from a reference, deriving a name from the reference.

    Backward-compatible entry point (name inferred, not required). ``config.py``
    uses ``resolve_policy`` with the binding's required ``name`` instead.
    """
    parsed = parse_ref(ref)
    if parsed.scheme in ("file", "github"):
        name = Path(parsed.locator).stem
    else:
        name = parsed.locator
    return resolve_policy(ref, name, base_dir)
