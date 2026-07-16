from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from reflexes.document import Policy, load_policy, resolve_policy

VALID_TRIGGERS = {"on_input", "before_response", "before_tool_call", "after_tool_call"}
VALID_ACTIONS = {"deny", "log", "flag"}


@dataclass
class ReflexBinding:
    """A policy bound to a trigger and action — one entry in a config group."""
    policy: Policy
    trigger: str
    action: str


def _load_raw(config_file: str | Path) -> dict:
    """Load and return the raw yaml dict from a config file."""
    path = Path(config_file)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")
    with open(path) as f:
        return yaml.safe_load(f)


def load_config(
    reflexes: list[str],
    config_file: str | Path = "reflexes.yaml",
) -> list[ReflexBinding]:
    """Load and merge reflex config groups.

    Reads from the 'reflexes' top-level key. Always includes 'default'
    first (if it exists), then the named groups in order. Last writer
    wins for the same (policy, trigger) pair.
    """
    raw = _load_raw(config_file)
    config_dir = Path(config_file).parent.resolve()

    reflex_groups = raw.get("reflexes", {})
    if not reflex_groups:
        raise ValueError(f"No 'reflexes' section in {config_file}")

    # Build ordered list of groups: default first, then requested
    groups = []
    if "default" in reflex_groups and "default" not in reflexes:
        groups.append("default")
    groups.extend(reflexes)

    # Merge: last writer wins per (policy_ref, trigger) pair
    merged: dict[tuple[str, str], ReflexBinding] = {}
    for group_name in groups:
        if group_name not in reflex_groups:
            raise ValueError(f"Unknown reflex config group: {group_name}")
        for entry in reflex_groups[group_name]:
            trigger = entry["trigger"]
            action = entry["action"]
            if trigger not in VALID_TRIGGERS:
                raise ValueError(
                    f"Invalid trigger '{trigger}' in group '{group_name}'. "
                    f"Valid triggers: {sorted(VALID_TRIGGERS)}"
                )
            if action not in VALID_ACTIONS:
                raise ValueError(
                    f"Invalid action '{action}' in group '{group_name}'. "
                    f"Valid actions: {sorted(VALID_ACTIONS)}"
                )
            policy_ref = entry["policy"]
            name = entry.get("name")
            if not name or not str(name).strip():
                raise ValueError(
                    f"Binding for policy '{policy_ref}' (trigger '{trigger}') in "
                    f"group '{group_name}' is missing the required 'name' field. "
                    f"Add a `name:` to each reflexes.yaml binding."
                )
            policy = resolve_policy(policy_ref, str(name).strip(), base_dir=config_dir)
            merged[(policy_ref, trigger)] = ReflexBinding(
                policy=policy,
                trigger=trigger,
                action=action,
            )

    return list(merged.values())


def configure_from_file(config_file: str | Path = "reflexes.yaml"):
    """Read the 'evaluators' section from reflexes.yaml and configure the
    default evaluator (the first one listed).

    Example:
        evaluators:
          cope:
            type: zentropi
            model: cope-latest

          gpt-oss:
            type: openai
            model: openai/gpt-oss-safeguard-20b
            endpoint: https://api.groq.com/openai/v1/chat/completions
            api_key_env: GROQ_API_KEY
    """
    from reflexes.core import configure

    path = Path(config_file)
    if not path.exists():
        return

    raw = _load_raw(config_file)
    if not raw:
        # Empty or content-free config (e.g. an empty file, or the
        # `--config /dev/null` runtime smoke). Nothing to configure —
        # leave the default evaluator untouched rather than crashing.
        return

    evaluators = raw.get("evaluators")
    if not evaluators:
        return

    # Use 'default' if it exists, otherwise the first listed
    if "default" in evaluators:
        cfg = evaluators["default"]
    else:
        cfg = next(iter(evaluators.values()))

    # Resolve API key: explicit value, env var name, or let evaluator use its default
    api_key = cfg.get("api_key")
    if not api_key and "api_key_env" in cfg:
        api_key = os.environ.get(cfg["api_key_env"])

    configure(
        evaluator=cfg.get("type", "zentropi"),
        api_key=api_key,
        endpoint=cfg.get("endpoint"),
        model=cfg.get("model"),
    )
