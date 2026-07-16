"""Drift guard: host maturity tiers stay consistent across the docs.

Tier labels live in two places — FRAMEWORKS.md's at-a-glance tables (the source
of truth) and README.md's maturity table + status blurb. When a host is promoted
or demoted, all of them must move together. This test fails if they disagree.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRAMEWORKS = (REPO / "FRAMEWORKS.md").read_text(encoding="utf-8")
README = (REPO / "README.md").read_text(encoding="utf-8")

_TIER = {"🟢": "Stable", "🟡": "Beta", "🔴": "Experimental"}
_EMOJI_RE = re.compile("|".join(map(re.escape, _TIER)))


def _canon(host: str) -> str:
    """Normalize a host label to a comparable key (drops bold/backticks, a
    trailing parenthetical like `(≥0.18)`, the `NousResearch` prefix, and
    punctuation): 'NousResearch Hermes (≥0.18)' -> 'hermes'."""
    h = re.sub(r"\(.*?\)", "", host)
    h = re.sub(r"nousresearch", "", h, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", h.lower())


def _table_tiers(md: str) -> dict[str, str]:
    """host_canon -> tier, from any markdown table row whose cells carry a
    tier emoji (🟢/🟡/🔴). Rows without one (headers, other tables) are ignored."""
    out: dict[str, str] = {}
    for line in md.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        m = _EMOJI_RE.search(line)
        if not m:
            continue
        host = _canon(line.strip().strip("|").split("|")[0])
        if host and host != "host":
            out[host] = _TIER[m.group(0)]
    return out


def test_readme_table_tiers_match_frameworks():
    fw = _table_tiers(FRAMEWORKS)
    rd = _table_tiers(README)
    assert fw, "no tiers parsed from FRAMEWORKS.md — parser or doc structure changed"
    assert rd, "no tiers parsed from README.md — parser or doc structure changed"
    for host, tier in rd.items():
        assert host in fw, f"README lists host '{host}' with a tier, but it's absent from FRAMEWORKS tables"
        assert fw[host] == tier, (
            f"tier mismatch for '{host}': README says {tier}, FRAMEWORKS says {fw[host]}"
        )


def test_readme_status_blurb_matches_stable_set():
    """The README status blurb names the Stable hosts; that set must equal the
    Stable set in FRAMEWORKS's tables (catches 'forgot to update the blurb')."""
    stable = {h for h, t in _table_tiers(FRAMEWORKS).items() if t == "Stable"}
    flat = re.sub(r"\s+", " ", re.sub(r"\n>\s?", " ", README))  # unwrap the blockquote
    m = re.search(r"release\.\*\*\s*(.+?)\s+are Stable", flat)
    assert m, "could not find the README '... are Stable' status blurb"
    named = {_canon(x) for x in re.split(r",|\band\b", m.group(1)) if _canon(x)}
    assert named == stable, (
        f"README status blurb Stable set {sorted(named)} != FRAMEWORKS Stable set {sorted(stable)}"
    )
