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
