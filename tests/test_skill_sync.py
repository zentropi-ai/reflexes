"""Skill-as-source-of-truth invariants.

The skill (`skill/`) is canonical: `skill/reflexes/` is the package and
`skill/SKILL.md` the instructions. The pip wheel is generated FROM it
(scripts/build_wheel.py). There is no package<->bundle copy to sync.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skill" / "SKILL.md"
INSTALL = ROOT / "skill" / "INSTALL.md"


def test_install_has_definition_of_done_gate():
    s = INSTALL.read_text()
    assert "not installing Reflexes" in s            # copying the folder isn't installing
    assert "pip install reflexes" in s               # library path
    assert "bundled in this skill" in s              # CLI runtime is bundled, no pip
    assert "uv run" in s


def test_skill_bundles_starter_policies():
    root = ROOT / "skill" / "reflexes" / "_starter_policies"
    integrity = {p.stem for p in (root / "integrity").glob("*.md")}
    autonomy = {p.stem for p in (root / "autonomy").glob("*.md")}
    assert {"Constraint-Violation", "Unconfirmed-Destructive-Action", "Sycophancy"} == integrity
    assert {"Early-Stopping", "Laziness-Detection", "Unsourced-Claims"} == autonomy


def test_bundled_hook_scripts_exist_and_declare_deps():
    scripts = ROOT / "skill" / "scripts"
    for name in ("reflexes-hook", "reflexes-doctor"):
        s = (scripts / name).read_text()
        assert "uv run --script" in s
        assert "pyyaml" in s and "httpx" in s


def test_readme_install_wiring_parity():
    """Canonical library-wiring calls must appear verbatim in BOTH the README
    (PyPI page) and skill/INSTALL.md, so they can't drift."""
    readme = (ROOT / "README.md").read_text()
    install = INSTALL.read_text()
    for snippet in (
        'with_reflexes(agent, reflexes=["default"], config_file="reflexes.yaml")',
        'middleware_from_config(reflexes=["default"], config_file="reflexes.yaml")',
    ):
        assert snippet in readme and snippet in install, f"wiring drift: {snippet}"
