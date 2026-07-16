#!/usr/bin/env python3
"""Generate the pip wheel FROM the skill (the source of truth).

The skill (`skill/`) is canonical: `skill/reflexes/` is the package,
`skill/SKILL.md` the canonical instructions. This script copies
`skill/SKILL.md` into the package so the wheel ships it (readable via
`python -m reflexes` / importlib), then builds the wheel + sdist.

Usage:  python3 scripts/build_wheel.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
shutil.copyfile(root / "skill" / "SKILL.md", root / "skill" / "reflexes" / "SKILL.md")
print("generated skill/reflexes/SKILL.md from canonical skill/SKILL.md")
subprocess.run([sys.executable, "-m", "build", "--wheel"], cwd=root, check=True)
