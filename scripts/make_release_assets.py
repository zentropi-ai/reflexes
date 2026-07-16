#!/usr/bin/env python3
"""Build the release assets for a Reflexes skill release:

  - reflexes-skill-<version>.tar.gz   (the skill/ bundle an agent downloads)
  - latest.json                        (the version pointer the update client reads)

Both are written to <out>/ (default: dist/) and uploaded as GitHub Release
assets by the release workflow on tag push. The update client
(``reflexes.update``) fetches the latest release's latest.json via
github.com/zentropi-ai/reflexes/releases/latest/download/latest.json, so
publishing the release is what makes a new version discoverable.

Usage:
  python scripts/make_release_assets.py --tag v0.1.0 [--released 2026-08-01] [--out dist]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skill"
REPO = "zentropi-ai/reflexes"


def read_version() -> str:
    ns: dict = {}
    exec((SKILL / "reflexes" / "_version.py").read_text(), ns)
    return ns["__version__"]


def _tar_filter(ti: tarfile.TarInfo):
    if "__pycache__" in ti.name or ti.name.endswith((".pyc", ".pyo")):
        return None
    return ti


def build_bundle(out: Path, version: str) -> Path:
    # Ensure the package copy of SKILL.md is current before bundling.
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_wheel.py")],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out.mkdir(parents=True, exist_ok=True)
    tar_path = out / f"reflexes-skill-{version}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(SKILL, arcname="skill", filter=_tar_filter)
    return tar_path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="Release tag, e.g. v0.1.0")
    ap.add_argument("--released", default=None, help="ISO date; defaults to today")
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()

    version = read_version()
    out = ROOT / a.out
    tar = build_bundle(out, version)
    digest = sha256(tar)

    latest = {
        "version": version,
        "tag": a.tag,
        "released": a.released or date.today().isoformat(),
        "min_compatible_config": "0.1.0",
        "bundle_url": f"https://github.com/{REPO}/releases/download/{a.tag}/{tar.name}",
        "sha256": digest,
        "notes_url": f"https://github.com/{REPO}/releases/tag/{a.tag}",
    }
    payload = json.dumps(latest, indent=2) + "\n"
    (out / "latest.json").write_text(payload)

    print(f"bundle:      {tar.relative_to(ROOT)}  ({tar.stat().st_size} bytes)")
    print(f"sha256:      {digest}")
    print(f"latest.json: {(out / 'latest.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
