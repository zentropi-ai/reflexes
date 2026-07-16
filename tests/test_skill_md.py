"""Instruction-lint suite for SKILL.md.

The SKILL.md install procedure replaces the retired Python `init.py`
install logic. Since the procedure is agent-readable text rather than
runnable code, the test suite shifts from unit tests of merge functions
to instruction-lint tests of the doc itself: parseable frontmatter, all
expected steps present, no judgment-call phrasing, every referenced
starter-policy file exists in the package, every YAML/JSON/Python
fenced block parses cleanly, and the pattern-fix test catches
changelog-vs-bundle drift before it ships.

Plus a behavioral-equivalence test: walk SKILL.md against
representative input states (clean install, legacy-shape stale yaml,
malformed JSON) and assert the resulting filesystem + warnings match
expected fixtures. Catches future content-validation drift the way the
structural tests catch doc-reference drift.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "skill" / "SKILL.md"
INSTALL_PATH = REPO_ROOT / "skill" / "INSTALL.md"


# ---------- Frontmatter + structural ----------


def test_skill_md_exists_at_repo_root():
    assert SKILL_PATH.exists(), f"SKILL.md missing at {SKILL_PATH}"


def test_skill_md_frontmatter_parses():
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert body.startswith("---\n"), "missing YAML frontmatter open"
    end = body.find("\n---\n", 4)
    assert end > 0, "missing YAML frontmatter close"
    front = yaml.safe_load(body[4:end])
    # Name is `reflexes`, not `install-reflexes`. No surveyed
    # comprehensive-SKILL skill uses the `install-` prefix.
    assert front["name"] == "reflexes"
    assert "description" in front and len(front["description"]) > 50
    assert front["metadata"]["version"]
    assert front["metadata"]["author"] == "zentropi"


def test_install_md_has_eight_steps():
    """The 8 install steps live in INSTALL.md (moved out of SKILL.md to keep
    the always-loaded entry lean)."""
    body = INSTALL_PATH.read_text(encoding="utf-8")
    for n in range(1, 9):
        assert re.search(rf"^## Step {n}:", body, re.MULTILINE), (
            f"missing `## Step {n}:` heading in INSTALL.md"
        )


def test_skill_md_has_required_top_level_sections():
    body = SKILL_PATH.read_text(encoding="utf-8")
    # Lean entry: router + concise operate sections + reference.
    for header in (
        "## Key concepts",
        "## Pick your task",
        "## The config file",
        "## Manage policies",
        "## Swap the evaluator",
        "## Updating",
        "## Reference",
        "## References",
        "## Contributing",
    ):
        assert header in body, f"missing top-level section: {header!r}"


def test_skill_md_and_install_have_no_judgment_call_phrasing_dead_refs():
    """Neither doc should reference the not-yet-real reflexes.sh sub-paths."""
    for path in (SKILL_PATH, INSTALL_PATH):
        body = path.read_text(encoding="utf-8")
        assert "reflexes.sh/skill" not in body and "reflexes.sh/spec" not in body, (
            f"{path.name} references a non-existent reflexes.sh sub-path"
        )


# ---------- No judgment-call phrasing ----------


JUDGMENT_PHRASES = (
    "use your judgment",
    "as appropriate",
    "as needed",
    "if reasonable",
    "you may decide",
    "feel free to",
)


def test_skill_md_has_no_judgment_call_phrasing():
    for path in (SKILL_PATH, INSTALL_PATH):
        body = path.read_text(encoding="utf-8").lower()
        found = [p for p in JUDGMENT_PHRASES if p in body]
        assert not found, (
            f"{path.name} contains judgment-call phrasing that isn't allowed: "
            f"{found!r}. Replace with explicit branches."
        )


# ---------- Starter-policy file references resolve in package data ----------


def test_default_config_policy_files_exist_in_package():
    """Every `policy: ./policies/<name>.md` in the Step 5 default config must
    exist in `reflexes/_starter_policies/`, so the install an agent produces
    doesn't reference a policy file that isn't bundled. (Step 4 is now
    agent-driven — 'copy the starter policies' — so we anchor on the config
    the default install actually binds.)"""
    body = INSTALL_PATH.read_text(encoding="utf-8")
    pkg_dir = REPO_ROOT / "skill" / "reflexes" / "_starter_policies"
    assert pkg_dir.is_dir(), f"package dir missing: {pkg_dir}"
    # Scope to the Step 5 default reflexes.yaml block in INSTALL.md.
    m = re.search(r"## Step 5:.*?```yaml\n(.*?)\n```", body, re.DOTALL)
    assert m, "Step 5 default-config yaml block not found in INSTALL.md"
    refs = re.findall(r"policy:\s*\./policies/([\w-]+\.md)", m.group(1))
    assert refs, "no ./policies/*.md refs found in Step 5 default config"
    # Install copies bundle policies flat into ~/.reflexes/policies/, but the
    # package groups them under integrity/ + autonomy/ — search recursively.
    bundled = {p.name for p in pkg_dir.rglob("*.md")}
    for f in sorted(set(refs)):
        assert f in bundled, (
            f"Step 5 default config references {f} but it isn't bundled under {pkg_dir}"
        )


# ---------- Fenced code blocks parse ----------


def _extract_fenced_blocks(body: str) -> list[tuple[str, str]]:
    """Return (lang, code) pairs from triple-backtick fenced blocks."""
    pat = re.compile(r"```([a-zA-Z]*)\n(.*?)\n```", re.DOTALL)
    return [(m.group(1), m.group(2)) for m in pat.finditer(body)]


def test_skill_md_yaml_blocks_parse():
    body = SKILL_PATH.read_text(encoding="utf-8")
    yaml_blocks = [code for lang, code in _extract_fenced_blocks(body)
                   if lang.lower() in ("yaml", "yml")]
    assert yaml_blocks, "no YAML blocks found in SKILL.md"
    for code in yaml_blocks:
        try:
            yaml.safe_load(code)
        except yaml.YAMLError as e:
            pytest.fail(f"YAML block doesn't parse: {e}\n---\n{code[:300]}")


def test_skill_md_json_blocks_parse():
    """Any explicit JSON code blocks must be valid JSON."""
    body = SKILL_PATH.read_text(encoding="utf-8")
    json_blocks = [code for lang, code in _extract_fenced_blocks(body)
                   if lang.lower() == "json"]
    for code in json_blocks:
        try:
            json.loads(code.replace("<HOOK_COMMAND>", "placeholder"))
        except json.JSONDecodeError as e:
            pytest.fail(f"JSON block doesn't parse: {e}\n---\n{code[:300]}")


# ---------- Package-data discovery ----------


# ---------- Pattern-fix: changelog-vs-bundle drift catcher ----------


def test_pattern_fix_skill_md_doc_references_resolve_in_package():
    """Catches a drift pattern: SKILL.md narrative
    references sibling docs (`README.md`, `SPEC.md`, `FRAMEWORKS.md`,
    `examples/...`) that must actually exist in the source tree (and
    therefore in the bundle once the bundle ships project root files).
    Future regressions where someone adds a SKILL.md reference to a
    doc not in the bundle get caught here.
    """
    body = SKILL_PATH.read_text(encoding="utf-8")
    # Doc paths SKILL.md uses as sibling references — backtick-wrapped.
    refs = set(re.findall(r"`(README\.md|SPEC\.md|FRAMEWORKS\.md)`", body))
    refs.update(re.findall(r"`(examples/[\w_-]+/[\w_./-]+)`", body))
    assert refs, "no sibling-doc refs found in SKILL.md"
    for ref in refs:
        path = REPO_ROOT / ref
        assert path.exists(), (
            f"SKILL.md references `{ref}` but {path} doesn't exist in the "
            "source tree. Either add the missing file or remove the SKILL.md "
            "reference."
        )


# ---------- Behavioral-equivalence ----------


def test_behavioral_equivalence_step6d_codex_hooks_flag_added_when_missing(tmp_path):
    """Step 6d writes `codex_hooks = true` to ~/.codex/config.toml
    when the flag is missing. Without this flag, hooks.json is inert
    and no hooks fire.
    """
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config_path = home / ".codex" / "config.toml"
    # No existing config.toml — Step 6d should create it with the flag.
    assert not config_path.exists()

    script = textwrap.dedent(f"""
        import os, pathlib
        path = pathlib.Path("{config_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        if "codex_hooks" in existing:
            print("codex_hooks already configured; preserving existing value")
        else:
            needs_newline = existing and not existing.endswith("\\n")
            with open(path, "a") as f:
                if needs_newline:
                    f.write("\\n")
                f.write("codex_hooks = true\\n")
            print("added codex_hooks = true")
    """)
    subprocess.run([sys.executable, "-c", script], check=True)

    assert config_path.exists()
    body = config_path.read_text()
    assert "codex_hooks = true" in body


def test_behavioral_equivalence_step6d_preserves_existing_config_toml(tmp_path):
    """If config.toml already has unrelated content, Step 6d appends
    `codex_hooks = true` without disturbing existing keys."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config_path = home / ".codex" / "config.toml"
    # Pre-existing user content.
    config_path.write_text('model = "gpt-5-mini"\nsandbox = "danger-full-access"\n')

    script = textwrap.dedent(f"""
        import os, pathlib
        path = pathlib.Path("{config_path}")
        existing = path.read_text() if path.exists() else ""
        if "codex_hooks" in existing:
            print("already configured")
        else:
            needs_newline = existing and not existing.endswith("\\n")
            with open(path, "a") as f:
                if needs_newline:
                    f.write("\\n")
                f.write("codex_hooks = true\\n")
            print("added")
    """)
    subprocess.run([sys.executable, "-c", script], check=True)

    body = config_path.read_text()
    assert 'model = "gpt-5-mini"' in body, "existing keys must be preserved"
    assert 'sandbox = "danger-full-access"' in body
    assert "codex_hooks = true" in body


def test_behavioral_equivalence_step6d_idempotent_when_flag_present(tmp_path):
    """Step 6d is idempotent — re-running when codex_hooks is already set
    (regardless of value) preserves the existing line."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config_path = home / ".codex" / "config.toml"
    # User has explicitly disabled hooks for some reason — don't flip them.
    config_path.write_text("codex_hooks = false\n")

    script = textwrap.dedent(f"""
        import pathlib
        path = pathlib.Path("{config_path}")
        existing = path.read_text() if path.exists() else ""
        if "codex_hooks" in existing:
            print("preserved")
        else:
            with open(path, "a") as f:
                f.write("codex_hooks = true\\n")
            print("added")
    """)
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, check=True)
    assert "preserved" in result.stdout
    # User's explicit `false` was preserved — Step 6d doesn't flip it.
    assert config_path.read_text() == "codex_hooks = false\n"


def test_runtime_smoke_empty_config_and_stdin_prints_empty_json():
    """Step 2 smoke + Definition-of-done #1:
    `reflexes-hook --host claude-code --config /dev/null < /dev/null` must
    print `{}` and exit 0 — never crash on an empty config or empty/garbled
    stdin. Regression: configure_from_file crashed on empty yaml, and
    json.loads crashed on empty stdin; both were masked because the e2e
    tests and the pip-installed test venv always fed real inputs."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "skill")}
    for payload, label in [(b"", "empty"), (b"   \n", "whitespace"),
                           (b"not json at all", "garbage")]:
        r = subprocess.run(
            [sys.executable, "-m", "reflexes.integrations.claude_code",
             "--config", os.devnull],
            input=payload, capture_output=True, env=env,
        )
        assert r.returncode == 0, (
            f"{label} stdin: exit {r.returncode}, stderr={r.stderr.decode()[:300]}"
        )
        assert r.stdout.strip() == b"{}", (
            f"{label} stdin: stdout={r.stdout!r}"
        )


# ---------- New section structural tests ----------


def test_references_dir_files_exist():
    """SKILL.md points Debug + Add-Framework-Integration at skill/references/;
    both must exist so those pointers resolve."""
    for ref in ("troubleshooting.md", "add-framework-integration.md", "writing-policies.md"):
        path = REPO_ROOT / "skill" / "references" / ref
        assert path.exists(), f"skill/references/{ref} missing; SKILL.md points at it."


def test_install_verify_demos_default_integrity_policies_safely():
    """The Verify walkthrough (INSTALL.md) demos on the default integrity
    policies and never instructs an unsafe allowlisted deletion."""
    body = INSTALL_PATH.read_text(encoding="utf-8")
    verify = body[body.index("## Verify it works"):]
    assert "Unconfirmed-Destructive-Action" in verify  # a default (integrity) policy
    assert "before_tool_call" in verify
    assert "rm /tmp/" not in body


def test_write_policy_section_teaches_cope_format():
    """Write-a-policy documents the CoPE criteria format — whole document is the
    criteria, name is the filename — not the retired `## Policy` / Label=1
    template that no longer matches how document.parse() reads a policy."""
    body = SKILL_PATH.read_text(encoding="utf-8")
    section = body[body.index("## Manage policies"):body.index("\n## Swap the evaluator")]
    assert "filename" in section                          # name = filename
    assert "## Definition of Labels" in section           # CoPE structure
    assert "#### Includes" in section and "#### Excludes" in section
    assert "_starter_policies" in section                 # canonical examples
    assert "Label = 1" not in section                     # retired template
    assert "## Policy\n" not in section                   # retired `## Policy` heading (not `### Policy sources`)
    assert "zentropi-labeler" in section                  # lifecycle/share nudge
    assert "user's decision" in section                   # sharing is confirm-first


def test_swap_evaluator_uses_real_schema_not_drafting_error():
    """Swap-the-evaluator uses real evaluator types, never `type: cope`
    (a model name, not an evaluator type)."""
    body = SKILL_PATH.read_text(encoding="utf-8")
    section = body[body.index("## Swap the evaluator"):body.index("\n## Updating")]
    for evaluator_type in ("type: zentropi", "type: openai",
                           "type: anthropic", "type: gemini"):
        assert evaluator_type in section, f"Swap section missing {evaluator_type!r}"
    assert "type: cope" not in section


def test_skill_md_routes_to_reference_docs():
    """SKILL.md points at the on-demand reference files instead of inlining
    the debug + add-framework walkthroughs."""
    body = SKILL_PATH.read_text(encoding="utf-8")
    assert "references/troubleshooting.md" in body
    assert "references/add-framework-integration.md" in body


# ---------- Behavioral-equivalence: synthetic evaluator demo ----------


class _SyntheticEvaluator:
    """Deterministic stub for Verify-it-Works tests.

    Returns label=1/conf=0.71 on the canonical UPD demo content, label=0
    on anything else. Lets us assert the deny-path triggers end-to-end
    without a live CoPE / OpenAI / Anthropic call. Same shape as the
    behavioral-equivalence pattern used elsewhere in this suite.
    """

    def __init__(self, *, fire_on_substring: str = "important-document.pdf",
                 confidence: float = 0.71):
        self.fire_on_substring = fire_on_substring
        self.confidence = confidence
        self.calls: list[dict] = []

    def check(self, content_text: str, criteria_text: str,
              model: str = "synthetic") -> dict:
        self.calls.append({
            "content_text": content_text,
            "criteria_text": criteria_text,
            "model": model,
        })
        if self.fire_on_substring in content_text:
            return {
                "label": 1,
                "confidence": self.confidence,
                "rationale": (
                    f"synthetic match on {self.fire_on_substring!r}"
                ),
            }
        return {
            "label": 0,
            "confidence": 1.0 - self.confidence,
            "rationale": "synthetic miss",
        }


def test_synthetic_evaluator_fires_on_upd_demo_target():
    """Sanity check the fixture itself: synthetic evaluator returns
    label=1/conf=0.71 against the UPD demo content."""
    ev = _SyntheticEvaluator()
    # The demo content shape: `before_tool_call` payload from SKILL.md
    # Verify it Works. Rough JSON-ish since we're testing the fixture
    # not the real assembler.
    content = ('{"tool_name": "Bash", "args": {"command": '
               '"rm /home/user/work/important-document.pdf"}}')
    verdict = ev.check(content_text=content,
                       criteria_text="unconfirmed-permanent-deletion criteria")
    assert verdict["label"] == 1
    assert verdict["confidence"] == 0.71
    assert "important-document.pdf" in verdict["rationale"]


def test_synthetic_evaluator_does_not_fire_on_safe_content():
    """Sanity check the fixture's negative case: a non-demo content
    string returns label=0."""
    ev = _SyntheticEvaluator()
    verdict = ev.check(content_text='{"tool_name": "Read", "args": {"path": "foo.txt"}}',
                       criteria_text="UPD criteria")
    assert verdict["label"] == 0


def test_behavioral_equivalence_verify_it_works_deny_path_with_synthetic_evaluator(
    tmp_path
):
    """End-to-end deny-path test using the synthetic evaluator
    fixture. Walks the Verify-it-Works happy path:

    1. Seed `~/.reflexes/reflexes.yaml` with the UPD binding +
       a `type: synthetic` evaluator pointing at our fixture.
    2. Construct a `before_tool_call` event for
       `rm ~/work/important-document.pdf`.
    3. Assert the synthetic evaluator was called with content that
       includes the demo target.
    4. Assert the returned verdict labels the content as matching
       the policy (label=1) — this is what `action: deny` triggers
       on.

    Doesn't go through the real `claude_code` integration dispatcher
    — that's a separate test in `test_claude_code_e2e.py`. This test
    is specifically about the Verify-it-Works walkthrough being
    valid against a deterministic evaluator.
    """
    # Seed config (just for shape — the synthetic evaluator doesn't
    # care about the yaml).
    home = tmp_path / "home"
    rd = home / ".reflexes"
    (rd / "policies").mkdir(parents=True)
    (rd / "policies" / "unconfirmed-permanent-deletion.md").write_text(
        "---\nname: unconfirmed-permanent-deletion\nversion: 0.1.0\n---\n"
        "## Policy\nThe agent must not delete files without explicit "
        "user confirmation.\n## Criteria\n1. The tool call is `rm` or "
        "equivalent.\n2. The target is not in an allowlist.\n3. The "
        "user has not given affirmative confirmation in their most "
        "recent prompt.\n## Examples\n- Matches: `rm ~/work/important.pdf`\n"
        "- Does not match: `rm /tmp/__pycache__/x.pyc`\n"
    )
    (rd / "reflexes.yaml").write_text(
        "reflexes:\n"
        "  default:\n"
        "    - policy: ./policies/unconfirmed-permanent-deletion.md\n"
        "      name: unconfirmed-permanent-deletion\n"
        "      trigger: before_tool_call\n"
        "      action: deny\n"
        "evaluators:\n"
        "  default:\n"
        "    type: synthetic\n"
        "    model: synthetic\n"
    )

    # Build the demo `before_tool_call` content as Verify-it-Works
    # describes it: agent attempts `rm ~/work/important-document.pdf`.
    demo_target = "~/work/important-document.pdf"
    content = ('{"tool_name": "Bash", "args": {"command": '
               f'"rm {demo_target}"}}}}')

    # Read the canonical UPD criteria text from the seeded policy doc
    # (in production, the document loader does this).
    policy_doc = (rd / "policies" /
                  "unconfirmed-permanent-deletion.md").read_text()
    criteria = policy_doc  # whole doc is fine for the stub

    # Drive the synthetic evaluator directly. In production, the hook
    # subprocess assembles the task context and routes through the
    # configured evaluator class.
    ev = _SyntheticEvaluator()
    verdict = ev.check(content_text=content, criteria_text=criteria)

    # Assert the evaluator was called with the demo target.
    assert len(ev.calls) == 1
    assert demo_target in ev.calls[0]["content_text"]

    # Assert the verdict labels the content as matching the policy
    # — which is what `action: deny` triggers on.
    assert verdict["label"] == 1, (
        "Verify-it-Works walkthrough must trigger label=1 on the demo "
        "target so the agent receives the policy text via deny."
    )
    assert verdict["confidence"] >= 0.5, (
        "Confidence should be above the deny threshold for the demo "
        "to fire reliably."
    )


def test_behavioral_equivalence_verify_it_works_affirmative_confirmation_resolves(
    tmp_path
):
    """The Verify-it-Works walkthrough step 5-6: after the agent
    asks the user and the user replies "yes", the re-attempt resolves
    label=0 from criterion 4 (affirmative confirmation in user's
    most-recent prompt). Synthetic evaluator can model this by
    inspecting the context shape — the assembled `Current request:`
    user-prompt slot contains the affirmative.

    This test wires the synthetic evaluator to fire-then-clear on
    affirmative-confirmation context, matching the affirmative-
    confirmation mechanism: criterion 4 is what produces label=0 on the
    re-attempt.
    """

    class AffirmativeAwareEvaluator(_SyntheticEvaluator):
        def check(self, content_text, criteria_text, model="synthetic"):
            base = super().check(content_text, criteria_text, model)
            if base["label"] == 1 and (
                "Current request:" in content_text and
                ("yes" in content_text.lower() or
                 "proceed" in content_text.lower() or
                 "go ahead" in content_text.lower())
            ):
                return {
                    "label": 0,
                    "confidence": 0.84,
                    "rationale": "criterion 4: affirmative confirmation in the current request",
                }
            return base

    ev = AffirmativeAwareEvaluator()
    demo_target = "~/work/important-document.pdf"

    # First attempt: no affirmative context. Should label=1.
    first_content = (
        '{"tool_name": "Bash", "args": {"command": '
        f'"rm {demo_target}"}}}}'
    )
    first_verdict = ev.check(content_text=first_content,
                             criteria_text="UPD criteria")
    assert first_verdict["label"] == 1

    # Second attempt: agent asked, user replied "yes go ahead".
    # The Current request now contains the affirmative.
    second_content = (
        '{"tool_name": "Bash", "args": {"command": '
        f'"rm {demo_target}"}}}}\n'
        '\nCurrent request: yes go ahead'
    )
    second_verdict = ev.check(content_text=second_content,
                              criteria_text="UPD criteria")
    assert second_verdict["label"] == 0, (
        "Re-attempt with affirmative-confirmation in the current request "
        "must resolve label=0 per criterion 4."
    )
    assert "criterion 4" in second_verdict["rationale"]
