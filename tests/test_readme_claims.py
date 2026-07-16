"""Structural drift guards: README claims must match repo state.

These tests keep the README from claiming things the repo doesn't back:
the documentation must point at files that exist (SPEC.md, FRAMEWORKS.md,
SKILL.md) and must carry the self-host CoPE pointer required at launch.

This is the "docs say X, repo proves it" gate.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"


def _load_readme() -> str:
    assert README_PATH.exists(), f"README.md missing at {README_PATH}"
    return README_PATH.read_text(encoding="utf-8")


def test_readme_documents_required_top_level_files():
    readme = _load_readme()
    for required in ("SPEC.md", "FRAMEWORKS.md"):
        assert required in readme, (
            f"README missing reference to `{required}`; documentation drift"
        )


def test_skill_md_present_at_repo_root():
    """SKILL.md is a launch-day deliverable — agent-readable install
    instructions. Must exist at repo root and be syntactically valid
    YAML frontmatter + body.
    """
    skill_path = REPO_ROOT / "skill" / "SKILL.md"
    assert skill_path.exists(), "SKILL.md missing at repo root"
    body = skill_path.read_text(encoding="utf-8")
    assert body.startswith("---\n"), "SKILL.md missing YAML frontmatter"
    # `name: reflexes` (the comprehensive skill covering install + verify
    # + operate). No surveyed comprehensive-SKILL skill uses the
    # `install-` prefix. The prior `name: install-reflexes` (install-only)
    # and earlier `name: zentropi-reflexes` (reference-doc) shapes are
    # both retired.
    assert "name: reflexes" in body, (
        "SKILL.md frontmatter missing `name: reflexes` field"
    )


def test_readme_documents_self_host_path():
    """README must include a Self-host CoPE pointer alongside the API
    path so security-sensitive enterprises can find it.
    """
    readme = _load_readme()
    assert "zentropi-ai/cope-b-a4b" in readme, (
        "README missing HuggingFace weights pointer for self-host section"
    )
    assert "apache-2.0" in readme.lower(), (
        "README missing license pointer for self-host section"
    )
