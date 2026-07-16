"""Tests for config loading and merging."""

import os
import pytest
from pathlib import Path

from reflexes.config import load_config


@pytest.fixture
def config_dir(tmp_path):
    """Create a temp directory with policies and a reflexes.yaml."""
    policies = tmp_path / "policies"
    policies.mkdir()

    (policies / "injection.md").write_text("""\
---
name: injection
version: 0.1.0
tags: [safety]
---

# Injection

## Policy

Label = 1 when injection detected.
""")

    (policies / "toxicity.md").write_text("""\
---
name: toxicity
version: 0.1.0
tags: [safety]
---

# Toxicity

## Policy

Label = 1 when toxic.
""")

    (policies / "pii.md").write_text("""\
---
name: pii
version: 0.1.0
tags: [privacy]
---

# PII

## Policy

Label = 1 when PII detected.
""")

    (tmp_path / "reflexes.yaml").write_text("""\
reflexes:
  default:
    - policy: ./policies/injection.md
      name: injection
      trigger: on_input
      action: deny

  group-a:
    - policy: ./policies/toxicity.md
      name: toxicity
      trigger: before_response
      action: flag

  group-b:
    - policy: ./policies/injection.md
      name: injection
      trigger: on_input
      action: log

  group-c:
    - policy: ./policies/pii.md
      name: pii
      trigger: on_input
      action: flag
    - policy: ./policies/pii.md
      name: pii
      trigger: before_response
      action: deny

  bad-trigger:
    - policy: ./policies/injection.md
      name: injection
      trigger: on_sneeze
      action: deny

  bad-action:
    - policy: ./policies/injection.md
      name: injection
      trigger: on_input
      action: explode

evaluators:
  cope:
    type: zentropi
    model: cope-latest
""")

    return tmp_path


def _min_config(tmp_path, prefix=""):
    """A minimal one-binding reflexes.yaml; `prefix` prepends e.g. 'enabled: false\\n'."""
    pol = tmp_path / "policies"
    pol.mkdir(exist_ok=True)
    (pol / "p.md").write_text("# Criteria\n\n## Overview\nx\n")
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        prefix
        + "reflexes:\n  default:\n    - policy: ./policies/p.md\n      name: p\n"
        "      trigger: before_tool_call\n      action: deny\n"
        "evaluators:\n  default:\n    type: zentropi\n"
    )
    return cfg


def test_enabled_false_disables_all_bindings(tmp_path):
    """`enabled: false` is the global kill switch — load_config returns NO
    bindings, so every hook no-ops (allow)."""
    from reflexes.config import is_enabled
    cfg = _min_config(tmp_path, "enabled: false\n")
    assert is_enabled(cfg) is False
    assert load_config(reflexes=[], config_file=cfg) == []


def test_enabled_true_or_absent_loads_normally(tmp_path):
    from reflexes.config import is_enabled
    cfg = _min_config(tmp_path, "enabled: true\n")
    assert is_enabled(cfg) is True
    assert len(load_config(reflexes=[], config_file=cfg)) == 1
    cfg = _min_config(tmp_path, "")  # absent → default enabled
    assert is_enabled(cfg) is True
    assert len(load_config(reflexes=[], config_file=cfg)) == 1


def test_is_enabled_missing_file_defaults_enabled(tmp_path):
    """A read error must never silently disable — stay on (fail safe)."""
    from reflexes.config import is_enabled
    assert is_enabled(tmp_path / "nope.yaml") is True


def test_only_literal_false_disables_garbled_values_stay_on(tmp_path):
    """Safety default: ONLY the boolean `false` disables. A null, empty, or
    typo'd value (e.g. `enabled: flase`) must fail safe to enabled, never off."""
    from reflexes.config import is_enabled
    for prefix in ("enabled:\n", "enabled: null\n", "enabled: flase\n", "enabled: 0\n"):
        cfg = _min_config(tmp_path, prefix)
        assert is_enabled(cfg) is True, f"{prefix!r} must stay enabled (fail safe)"
        assert len(load_config(reflexes=[], config_file=cfg)) == 1
        cfg.unlink(); (tmp_path / "policies" / "p.md").unlink(); (tmp_path / "policies").rmdir()


def test_read_log_settings_defaults_and_parsing(tmp_path):
    from reflexes.config import read_log_settings
    # Absent logging: block → safe defaults.
    assert read_log_settings(_min_config(tmp_path, "")) == ("warning", "text")
    (tmp_path / "reflexes.yaml").unlink()
    (tmp_path / "policies" / "p.md").unlink(); (tmp_path / "policies").rmdir()
    # Explicit values are read and normalized (case-insensitive).
    cfg = _min_config(tmp_path, "logging:\n  level: DEBUG\n  format: JSON\n")
    assert read_log_settings(cfg) == ("debug", "json")


def test_read_log_settings_fails_safe(tmp_path):
    """A garbled level/format, or an unreadable file, must never yield a
    more-verbose level than the WARNING default."""
    from reflexes.config import read_log_settings
    cfg = _min_config(tmp_path, "logging:\n  level: chatty\n  format: yaml\n")
    assert read_log_settings(cfg) == ("warning", "text")
    assert read_log_settings(tmp_path / "nope.yaml") == ("warning", "text")


def test_no_content_logged_above_debug():
    """INVARIANT: message content is only ever emitted at DEBUG. No logger call
    at INFO or above may pass the evaluated content or assembled user/task
    context. Guards the safety promise behind the WARNING default."""
    import re
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent / "skill" / "reflexes"
    # logger.info/warning/error/critical/exception(...) lines that reference a
    # content-bearing variable name are forbidden.
    call = re.compile(r"\.(info|warning|error|critical|exception)\(", re.I)
    content_var = re.compile(r"\b(content|user_context|task_context|raw_input)\b")
    offenders = []
    for py in root.rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if call.search(line) and content_var.search(line):
                offenders.append(f"{py.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, "content must be DEBUG-only; offending log calls:\n" + "\n".join(offenders)


def test_log_action_surfaces_at_warning_not_info():
    """INVARIANT: the `log` action must emit at WARNING, not INFO. The default
    logging level is WARNING, so an INFO `log`-action record would be silently
    swallowed — defeating the action. Guard: no integration logs a policy
    `matched` line at INFO."""
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent / "skill" / "reflexes" / "integrations"
    offenders = []
    for py in root.glob("*.py"):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if "logger.info(" in line and "matched" in " ".join(lines[i:i + 3]):
                offenders.append(f"{py.name}:{i + 1}")
    assert not offenders, "log-action must emit at WARNING, not INFO:\n" + "\n".join(offenders)


def test_load_default_only(config_dir):
    os.chdir(config_dir)
    bindings = load_config(reflexes=[], config_file="reflexes.yaml")
    assert len(bindings) == 1
    assert bindings[0].policy.name == "injection"
    assert bindings[0].trigger == "on_input"
    assert bindings[0].action == "deny"


def test_load_with_group(config_dir):
    os.chdir(config_dir)
    bindings = load_config(reflexes=["group-a"], config_file="reflexes.yaml")
    assert len(bindings) == 2
    names = {b.policy.name for b in bindings}
    assert names == {"injection", "toxicity"}


def test_last_writer_wins(config_dir):
    os.chdir(config_dir)
    bindings = load_config(reflexes=["group-b"], config_file="reflexes.yaml")
    injection = [b for b in bindings if b.policy.name == "injection"][0]
    assert injection.action == "log"


def test_same_policy_different_triggers(config_dir):
    os.chdir(config_dir)
    bindings = load_config(reflexes=["group-c"], config_file="reflexes.yaml")
    pii_bindings = [b for b in bindings if b.policy.name == "pii"]
    assert len(pii_bindings) == 2
    triggers = {b.trigger for b in pii_bindings}
    assert triggers == {"on_input", "before_response"}


def test_unknown_group_raises(config_dir):
    os.chdir(config_dir)
    with pytest.raises(ValueError, match="Unknown"):
        load_config(reflexes=["nonexistent"], config_file="reflexes.yaml")


def test_invalid_trigger_raises(config_dir):
    os.chdir(config_dir)
    with pytest.raises(ValueError, match="Invalid trigger"):
        load_config(reflexes=["bad-trigger"], config_file="reflexes.yaml")


def test_invalid_action_raises(config_dir):
    os.chdir(config_dir)
    with pytest.raises(ValueError, match="Invalid action"):
        load_config(reflexes=["bad-action"], config_file="reflexes.yaml")
