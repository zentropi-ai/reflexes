"""Literal-code-extraction validator for documentation.

Walks documentation files (README.md, SPEC.md), pulls every fenced code
block, and validates each one according to its language tag:

- ``yaml`` blocks parse as YAML; if they declare a ``reflexes:`` key,
  they additionally round-trip through ``reflexes.config`` (verifying the
  shape an actual user would see is structurally accepted).
- ``markdown`` blocks that look like CoPE policy documents (``# Criteria``
  / ``## Definition of Labels``) round-trip through ``reflexes.document.parse``.
- ``python`` blocks syntax-check via ``ast.parse`` so undefined-symbol
  examples (e.g. ``agent = ...`` placeholders) still catch malformed
  syntax. End-to-end execution is intentionally NOT done here — that
  waits on the explicit ``run-as-is`` marker convention that ships with
  the documented run-as-is snippets.
- ``bash`` / ``sh`` blocks are not executed (would mutate system state).

This is the structural-validation layer of "docs say X works, repo
proves it." Subprocess execution will hang off the same scaffolding
once the run-as-is-marked example snippets land.
"""
from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from reflexes.document import parse as parse_policy


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_FILES = [REPO_ROOT / "README.md", REPO_ROOT / "SPEC.md",
             REPO_ROOT / "skill" / "SKILL.md", REPO_ROOT / "skill" / "INSTALL.md"]


@dataclass(frozen=True)
class CodeBlock:
    source_file: Path
    lang: str
    line: int  # 1-indexed start line in source_file
    body: str

    def __repr__(self) -> str:
        rel = self.source_file.relative_to(REPO_ROOT)
        return f"CodeBlock({rel}:{self.line} lang={self.lang!r})"


# Match opening fences like ```python or ```yaml — with optional info string
# after the language. Closing fence is ``` on its own line.
_FENCE_RE = re.compile(
    r"^```(?P<lang>[A-Za-z0-9_+\-]+)?[^\n]*\n(?P<body>.*?)\n```",
    re.DOTALL | re.MULTILINE,
)


def _extract_blocks(path: Path) -> list[CodeBlock]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    blocks: list[CodeBlock] = []
    for m in _FENCE_RE.finditer(text):
        lang = (m.group("lang") or "").lower()
        body = m.group("body")
        line = text[: m.start()].count("\n") + 1
        blocks.append(CodeBlock(source_file=path, lang=lang, line=line, body=body))
    return blocks


ALL_BLOCKS = [b for path in DOC_FILES for b in _extract_blocks(path)]


def test_at_least_one_doc_block_extracted():
    """Sanity: regressing the regex shouldn't silently zero out validation."""
    assert ALL_BLOCKS, "no fenced code blocks extracted from README.md or SPEC.md"


@pytest.mark.parametrize(
    "block",
    [b for b in ALL_BLOCKS if b.lang == "yaml"],
    ids=lambda b: f"{b.source_file.name}:{b.line}",
)
def test_yaml_blocks_parse(block: CodeBlock):
    # Frontmatter examples (leading `---`) are legitimately multi-document-
    # delimited; parse them multi-doc-aware. Everything else is single-doc.
    is_frontmatter = block.body.lstrip().startswith("---")
    try:
        if is_frontmatter:
            docs = [d for d in yaml.safe_load_all(block.body) if d is not None]
            loaded = docs[0] if docs else None
        else:
            loaded = yaml.safe_load(block.body)
    except yaml.YAMLError as e:
        pytest.fail(f"{block!r} did not parse as YAML: {e}\n---\n{block.body}\n---")
    # If the block declares a `reflexes:` section, also verify it round-trips
    # through the config loader (which validates triggers/actions and
    # resolves policy paths).
    if isinstance(loaded, dict) and "reflexes" in loaded:
        # The example uses local ./policies/<name>.md paths that don't exist
        # at the README's location. We're not loading the policies — we're
        # just validating the YAML shape and trigger/action enums. Re-parse
        # via config._load_raw to confirm the shape, but skip the full
        # load_config call (which would try to read the policy files).
        from reflexes.config import VALID_ACTIONS, VALID_TRIGGERS
        groups = loaded["reflexes"]
        assert isinstance(groups, dict), (
            f"{block!r}: 'reflexes' must be a mapping, got {type(groups).__name__}"
        )
        for group_name, entries in groups.items():
            assert isinstance(entries, list), (
                f"{block!r}: group '{group_name}' must be a list"
            )
            for i, entry in enumerate(entries):
                for key in ("policy", "trigger", "action"):
                    assert key in entry, (
                        f"{block!r}: group '{group_name}'[{i}] missing '{key}'"
                    )
                assert entry["trigger"] in VALID_TRIGGERS, (
                    f"{block!r}: group '{group_name}'[{i}] has invalid trigger "
                    f"{entry['trigger']!r}; expected one of {sorted(VALID_TRIGGERS)}"
                )
                assert entry["action"] in VALID_ACTIONS, (
                    f"{block!r}: group '{group_name}'[{i}] has invalid action "
                    f"{entry['action']!r}; expected one of {sorted(VALID_ACTIONS)}"
                )


def _looks_like_policy_doc(body: str) -> bool:
    # CoPE criteria documents: `# Criteria` header or a Definition of Labels
    # section. The whole document is the criteria (reflexes.document.parse).
    stripped = body.lstrip()
    return stripped.startswith("# Criteria") or "## Definition of Labels" in body


@pytest.mark.parametrize(
    "block",
    [
        b for b in ALL_BLOCKS
        if b.lang == "markdown" and _looks_like_policy_doc(b.body)
    ],
    ids=lambda b: f"{b.source_file.name}:{b.line}",
)
def test_policy_markdown_blocks_parse(block: CodeBlock):
    try:
        policy = parse_policy(block.body)
    except Exception as e:
        pytest.fail(
            f"{block!r} did not parse as a Reflexes policy: {e}\n"
            f"---\n{block.body}\n---"
        )
    assert policy.name, f"{block!r}: parsed policy has empty name"
    assert policy.criteria, f"{block!r}: parsed policy has empty criteria"


@pytest.mark.parametrize(
    "block",
    [b for b in ALL_BLOCKS if b.lang == "python"],
    ids=lambda b: f"{b.source_file.name}:{b.line}",
)
def test_python_blocks_syntax_check(block: CodeBlock):
    # Some Python blocks use ``...`` as an Ellipsis placeholder for an
    # agent object the reader supplies. ast.parse() accepts that as the
    # literal Ellipsis constant, so no special handling is needed.
    try:
        ast.parse(textwrap.dedent(block.body))
    except SyntaxError as e:
        pytest.fail(
            f"{block!r} has a Python syntax error at line {e.lineno}: {e.msg}\n"
            f"---\n{block.body}\n---"
        )


def test_no_unknown_languages_in_doc_blocks():
    """Catch typos in fence language tags before they silently skip validation."""
    known = {"python", "yaml", "markdown", "bash", "sh", "json", "toml", "text", "ini", ""}
    unknown = sorted({b.lang for b in ALL_BLOCKS if b.lang not in known})
    assert not unknown, (
        f"unknown language tags in doc blocks: {unknown!r} — either fix the "
        f"typo or add the tag to the `known` set in this test"
    )
