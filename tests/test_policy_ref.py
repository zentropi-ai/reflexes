"""Grammar tests for policy reference parsing (`reflexes.policy_ref`)."""
from __future__ import annotations

import pytest

from reflexes.policy_ref import PolicyRef, parse_ref


@pytest.mark.parametrize(
    "raw, scheme, locator, version",
    [
        # Bare paths → file (back-compat; these are the existing config shape).
        ("./policies/x.md", "file", "./policies/x.md", None),
        ("/abs/y.md", "file", "/abs/y.md", None),
        ("policies/z.md", "file", "policies/z.md", None),
        # Explicit file: scheme.
        ("file:./z.md", "file", "./z.md", None),
        # Windows drive letter must NOT be read as a scheme.
        (r"C:\win\path.md", "file", r"C:\win\path.md", None),
        # Zentropi, unpinned + pinned.
        ("zentropi:lab123", "zentropi", "lab123", None),
        ("zentropi:lab123@v5", "zentropi", "lab123", "v5"),
        ("zentropi:lab-abc_123@ver_9", "zentropi", "lab-abc_123", "ver_9"),
        # GitHub, unpinned + pinned (tag / sha / semver-tag).
        ("github:acme/repo/pols/p.md", "github", "acme/repo/pols/p.md", None),
        ("github:acme/repo/pols/p.md@v1.2.0", "github", "acme/repo/pols/p.md", "v1.2.0"),
        ("github:o/r/a/b/c.md@deadbeef", "github", "o/r/a/b/c.md", "deadbeef"),
    ],
)
def test_parse_ref(raw, scheme, locator, version):
    ref = parse_ref(raw)
    assert isinstance(ref, PolicyRef)
    assert ref.scheme == scheme
    assert ref.locator == locator
    assert ref.version == version
    assert ref.raw == raw


def test_is_pinned_and_is_local():
    assert parse_ref("zentropi:l@v1").is_pinned is True
    assert parse_ref("zentropi:l").is_pinned is False
    assert parse_ref("./p.md").is_local is True
    assert parse_ref("zentropi:l").is_local is False
    assert parse_ref("github:o/r/p.md").is_local is False


def test_at_sign_only_pins_hosted_schemes():
    # A local path that happens to contain '@' is not a pin.
    ref = parse_ref("./policies/weird@name.md")
    assert ref.scheme == "file"
    assert ref.locator == "./policies/weird@name.md"
    assert ref.version is None


def test_trailing_bare_at_is_unpinned():
    ref = parse_ref("zentropi:lab123@")
    assert ref.scheme == "zentropi"
    assert ref.locator == "lab123"
    assert ref.version is None


def test_unknown_scheme_prefix_is_treated_as_file():
    # An unregistered prefix (no http scheme!) is not special.
    ref = parse_ref("http://example.com/p.md")
    assert ref.scheme == "file"
    assert ref.locator == "http://example.com/p.md"

    ref2 = parse_ref("s3:bucket/key.md")
    assert ref2.scheme == "file"


def test_whitespace_is_stripped_but_raw_preserved():
    ref = parse_ref("  zentropi:lab123@v1  ")
    assert ref.scheme == "zentropi"
    assert ref.locator == "lab123"
    assert ref.version == "v1"
    assert ref.raw == "  zentropi:lab123@v1  "
