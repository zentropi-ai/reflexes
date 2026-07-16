"""Tests for policy document parsing (thin-consumer / CoPE model).

A Reflexes policy IS a CoPE criteria document: the whole document is the
criteria sent to the evaluator, and the policy name is the source file's
basename. No frontmatter or fixed section layout is required.
"""

from pathlib import Path

from reflexes.document import parse, Policy, load_policy


COPE_DOC = """\
# Criteria

## Overview

Detects unsourced factual claims in an agent's response.

## Definition of Labels

### (UC): Unsourced Claim Language

#### Includes
- **Unattributed Code Assertions**: "the function returns None" with no file cited.

#### Excludes
- **Attributed Claims**: "per config.py:12, the default is 10".
"""


def test_whole_document_is_criteria():
    # The entire document is the criteria — no section is dropped.
    policy = parse(COPE_DOC)
    assert policy.criteria == COPE_DOC.strip()
    assert "## Overview" in policy.criteria
    assert "#### Excludes" in policy.criteria


def test_name_is_filename_stem_verbatim():
    policy = parse(COPE_DOC, source_path="/x/My_Custom-Policy.md")
    assert policy.name == "My_Custom-Policy"  # underscores + hyphens preserved, no slugification


def test_name_without_source_path():
    assert parse(COPE_DOC).name == "policy"


def test_no_frontmatter_ok():
    # Dave's CoPE policies have no frontmatter — must parse without error.
    policy = parse("# Criteria\n\nSome criteria.", source_path="/x/foo.md")
    assert policy.name == "foo"
    assert policy.criteria == "# Criteria\n\nSome criteria."


def test_leading_frontmatter_is_stripped():
    # A legacy leading frontmatter block is optional metadata — stripped, not
    # sent to the evaluator. The name still comes from the filename.
    doc = "---\nname: legacy\nversion: 1.0.0\n---\n\n# Criteria\n\nBody text.\n"
    policy = parse(doc, source_path="/x/legacy.md")
    assert policy.criteria == "# Criteria\n\nBody text."
    assert "name: legacy" not in policy.criteria
    assert policy.name == "legacy"


def test_starter_policies_parse_as_whole_document():
    starter = (
        Path(__file__).resolve().parents[1]
        / "skill" / "reflexes" / "_starter_policies"
    )
    files = sorted(starter.rglob("*.md"))  # bundles: integrity/ + autonomy/
    assert files, "no starter policies bundled"
    for f in files:
        policy = load_policy(str(f))
        assert policy.name == f.stem                       # name = filename
        assert policy.criteria.strip()                     # whole doc = criteria
        assert "## Definition of Labels" in policy.criteria  # CoPE structure
        assert isinstance(policy, Policy)
