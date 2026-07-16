"""Drift guard: every shipped README funnels installation through SKILL.md.

The single source of truth for how to install Reflexes is SKILL.md (which
dispatches to INSTALL.md). READMEs are human-facing overviews — they must point
at that canonical procedure rather than growing their own divergent install
steps. This test fails if a shipped README documents installation without
routing to SKILL, which is how install docs drift apart.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that are not part of the shipped project (attic, agent scratch,
# build artifacts, caches). Their READMEs are out of scope.
_EXCLUDED = {"_attic", ".garvis", ".claude", "node_modules", "dist", ".git",
             ".pytest_cache", "build", ".venv", "venv"}


def _shipped_readmes() -> list[Path]:
    out = []
    for p in REPO_ROOT.rglob("README*.md"):
        if any(part in _EXCLUDED for part in p.relative_to(REPO_ROOT).parts):
            continue
        out.append(p)
    return out


def _has_install_section(text: str) -> bool:
    # Any markdown heading whose text contains "install" — catches "## Install",
    # "## Installation", and titled variants like "## Quick Install — via Python
    # Frameworks" / "## Quick Install — via Agents". Keyed off headings (not the
    # word anywhere) so a passing mention of `pip install` in prose doesn't
    # count as documenting an install procedure.
    return bool(re.search(r"(?mi)^#{1,6}\s+.*\binstall\b", text))


def test_shipped_readmes_exist():
    readmes = _shipped_readmes()
    assert readmes, "no shipped READMEs found — discovery is probably broken"
    # The two we know ship today; guards the exclusion filter from over-matching.
    names = {p.relative_to(REPO_ROOT).as_posix() for p in readmes}
    assert "README.md" in names
    assert "integrations-ts/openclaw/README.md" in names


@pytest.mark.parametrize("readme", _shipped_readmes(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_install_section_routes_to_skill(readme: Path):
    """Any README that documents installation must reference SKILL.md so readers
    are funneled to the canonical, agent-driven procedure."""
    text = readme.read_text(encoding="utf-8")
    if not _has_install_section(text):
        pytest.skip(f"{readme.name} has no install section")
    assert "SKILL" in text, (
        f"{readme.relative_to(REPO_ROOT)} documents installation but never points "
        f"at SKILL.md — install docs must route to the single source of truth."
    )
