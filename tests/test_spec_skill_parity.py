"""SPEC.md ↔ SKILL.md parity test.

SKILL.md's Reference section is a verified mirror of SPEC.md, not a
free-floating duplication. This
test parses both files, extracts the trigger / action enums + the
policy-markdown frontmatter shape, and asserts the SKILL.md cheat-
sheet matches SPEC.md's authoritative definitions.

SPEC.md stays canonical for narrative. SKILL.md stays the quick-
lookup the install/operate agent reads. If they drift, this test
fails and the maintainer reconciles.

What this test does NOT check:
- The one-line definitions for each enum (those are SKILL.md's
  rephrasing of SPEC.md's narrative — not a parity concern).
- The policy markdown body-sections list (SPEC.md §3.2 has
  "Policy / Criteria / Examples" but the Reference cheat-sheet
  only lists the frontmatter shape).
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "skill" / "SKILL.md"
SPEC_PATH = REPO_ROOT / "SPEC.md"


# ---------- helpers ----------


def _section(text: str, header_regex: str) -> str:
    """Return the body of a markdown section starting at header_regex
    and ending at the next same-level header (or EOF)."""
    m = re.search(header_regex, text, re.MULTILINE)
    assert m, f"section not found for regex: {header_regex!r}"
    start = m.end()
    # find next header at same or shallower depth
    level = m.group(0).count("#")
    tail = text[start:]
    next_m = re.search(rf"^#{{1,{level}}} ", tail, re.MULTILINE)
    return tail[: next_m.start()] if next_m else tail


def _extract_backticked_tokens(body: str, separator: str = "|") -> set[str]:
    r"""Extract `tokens` from inline backtick-wrapped lists like
    `on_input | before_tool_call | ...` or one-line bullets like
    `**\`on_input\`**`. Returns the set of unique tokens."""
    tokens = set()
    for m in re.finditer(r"`([a-z_]+)`", body):
        tokens.add(m.group(1))
    return tokens


# ---------- trigger taxonomy parity ----------


def test_trigger_taxonomy_matches_spec():
    """SKILL.md Reference §Trigger taxonomy must enumerate the same
    triggers SPEC.md §5 defines. Order-insensitive; one-line
    descriptions are SKILL.md's prerogative."""
    skill = SKILL_PATH.read_text()
    spec = SPEC_PATH.read_text()

    # SKILL.md: section header is "### Triggers"
    skill_section = _section(skill, r"^### Triggers")
    skill_triggers = _extract_backticked_tokens(skill_section)

    # SPEC.md: section header is "## 5. Trigger Taxonomy"
    spec_section = _section(spec, r"^## 5\. Trigger Taxonomy")
    spec_triggers = _extract_backticked_tokens(spec_section)

    # The canonical four. Anything narrower means SKILL.md is missing
    # a trigger; anything broader means SKILL.md added one SPEC.md
    # doesn't sanction.
    expected = {"on_input", "before_tool_call", "after_tool_call",
                "before_response"}

    skill_in_canonical = skill_triggers & expected
    spec_in_canonical = spec_triggers & expected

    assert skill_in_canonical == expected, (
        f"SKILL.md Reference missing canonical triggers: "
        f"{expected - skill_in_canonical}. Got: {skill_in_canonical}"
    )
    assert spec_in_canonical == expected, (
        f"SPEC.md §5 missing canonical triggers (regression): "
        f"{expected - spec_in_canonical}. Got: {spec_in_canonical}"
    )


# ---------- action taxonomy parity ----------


def test_action_taxonomy_matches_spec():
    """SKILL.md Reference §Action taxonomy must enumerate the same
    actions SPEC.md §7 defines."""
    skill = SKILL_PATH.read_text()
    spec = SPEC_PATH.read_text()

    skill_section = _section(skill, r"^### Actions")
    skill_actions = _extract_backticked_tokens(skill_section)

    spec_section = _section(spec, r"^## 7\. Action Taxonomy")
    spec_actions = _extract_backticked_tokens(spec_section)

    # SPEC.md §7 defines three canonical actions: deny, flag, log.
    # (Not `allow` — that's a colloquial name; the spec uses `log`
    # for the proceed-silently-with-INFO-log variant.)
    expected = {"deny", "flag", "log"}

    skill_in_canonical = skill_actions & expected
    spec_in_canonical = spec_actions & expected

    assert skill_in_canonical == expected, (
        f"SKILL.md Reference missing canonical actions: "
        f"{expected - skill_in_canonical}. Got: {skill_in_canonical}"
    )
    assert spec_in_canonical == expected, (
        f"SPEC.md §7 missing canonical actions (regression): "
        f"{expected - spec_in_canonical}. Got: {spec_in_canonical}"
    )


# ---------- policy format parity ----------


def test_policy_format_matches_spec():
    """SKILL.md Reference §Policy format and SPEC.md §3 agree on the CoPE
    thin-consumer model: the whole document is the criteria, the policy name is
    the filename, and no frontmatter is required."""
    skill = SKILL_PATH.read_text()
    spec = SPEC_PATH.read_text()

    skill_section = _section(skill, r"^### Policy format")
    spec_section = _section(spec, r"^## 3\. Policy Document Format")

    for section, where in ((skill_section, "SKILL.md §Policy format"),
                           (spec_section, "SPEC.md §3")):
        low = section.lower()
        assert "filename" in low, f"{where} must state the name is the filename"
        assert "criteria" in low, f"{where} must describe the criteria"
        assert "cope" in low, f"{where} must reference the CoPE format"


# ---------- structural: SKILL.md Reference section points at SPEC.md ----------


def test_skill_reference_section_credits_spec_as_canonical():
    """The Reference section should state SPEC.md is canonical and
    the cheat-sheet is a verified mirror. Otherwise the cheat-sheet
    becomes the de facto canonical doc and SPEC.md drifts."""
    skill = SKILL_PATH.read_text()
    ref_section = _section(skill, r"^## Reference")

    # Must mention SPEC.md
    assert "SPEC.md" in ref_section, (
        "Reference section must credit SPEC.md as canonical."
    )
