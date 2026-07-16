"""Policy references — parse a binding's ``policy:`` value into a `PolicyRef`.

A ``policy:`` value in ``reflexes.yaml`` is a *policy reference* of the form
``<scheme>:<locator>``. The ``file`` scheme (the default when no registered
scheme prefix is present) resolves to a local markdown file; ``zentropi:`` and
``github:`` resolve to hosted policies. Whatever the scheme, resolution yields a
policy document (criteria text) — see ``reflexes.policy_sources``.

Grammar::

    zentropi:<labeler_id>[@<version_id>]
    github:<owner>/<repo>/<path...>[@<git-ref>]      # git-ref = tag | sha | branch
    file:<path>                                       # explicit; same as a bare path
    <path>                                            # bare path → scheme "file"

There is deliberately **no** ``http:``/arbitrary-URL scheme (SSRF surface);
``github:`` covers the "policy versioned in a repo" case with one constrained,
well-known host.

A trailing ``@<version>`` pins a hosted ref to an immutable version (recommended
for reproducibility); without it the ref is *unpinned* (``zentropi:`` → the
latest deployed version; ``github:`` → the default branch). ``@`` is only
significant for hosted schemes, so local file paths that happen to contain ``@``
are unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass

# Only these scheme prefixes are recognized. Anything else — ``./policies/x.md``,
# ``/abs/x.md``, a Windows ``C:\...`` drive letter — is treated as a file path.
REGISTERED_SCHEMES = frozenset({"file", "zentropi", "github"})

# Schemes whose ``@version`` suffix denotes a pinnable version.
_VERSIONED_SCHEMES = frozenset({"zentropi", "github"})


@dataclass(frozen=True)
class PolicyRef:
    """A parsed ``policy:`` reference."""

    scheme: str          # "file" | "zentropi" | "github"
    locator: str         # scheme-specific: path | labeler_id | owner/repo/path
    version: str | None  # labeler version_id / git tag|sha|branch; None = unpinned
    raw: str             # the original string, for messages/logging

    @property
    def is_pinned(self) -> bool:
        return self.version is not None

    @property
    def is_local(self) -> bool:
        return self.scheme == "file"


def parse_ref(ref: str) -> PolicyRef:
    """Parse a ``policy:`` value into a `PolicyRef`.

    A string is hosted iff it starts with a registered scheme name followed by
    ``:``. Everything else resolves to ``file`` (so existing bare-path configs
    are unaffected).
    """
    raw = ref
    text = ref.strip()

    scheme = "file"
    locator = text
    if ":" in text:
        head, rest = text.split(":", 1)
        if head in REGISTERED_SCHEMES:
            scheme = head
            locator = rest

    version: str | None = None
    if scheme in _VERSIONED_SCHEMES and "@" in locator:
        locator, version = locator.rsplit("@", 1)
        version = version or None  # trailing bare "@" → unpinned, not "" version

    return PolicyRef(scheme=scheme, locator=locator, version=version, raw=raw)
