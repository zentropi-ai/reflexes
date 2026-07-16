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


def _enabled_from_raw(raw: dict) -> bool:
    """A top-level `enabled: false` disables the whole system. Absent, or any
    value other than the boolean `false`, means enabled (the safe default)."""
    return raw.get("enabled", True) is not False


def is_enabled(config_file: str | Path = "reflexes.yaml") -> bool:
    """Global on/off switch. `enabled: false` at the top of reflexes.yaml turns
    ALL enforcement off (no bindings load, every hook no-ops → allow). Defaults
    to enabled; a read error never disables (fail safe — stay on)."""
    try:
        return _enabled_from_raw(_load_raw(config_file))
    except Exception:
        return True


# Diagnostic logging is configured entirely from the `logging:` block in
# reflexes.yaml (no env vars). The default is WARNING — enough to surface
# enforcement problems (fail-open, evaluator errors) and `log`-action hits,
# but NOT message content, which is only ever emitted at DEBUG. `off` silences
# everything. Unknown values fail safe to the default (never accidentally
# verbose).
VALID_LOG_LEVELS = {"off", "none", "error", "warning", "info", "debug"}
VALID_LOG_FORMATS = {"text", "json"}
DEFAULT_LOG_LEVEL = "warning"
DEFAULT_LOG_FORMAT = "text"


def _log_settings_from_raw(raw: dict) -> tuple[str, str]:
    """Extract `(level, format)` from a raw config dict, normalized and
    validated. Anything malformed falls back to the safe defaults — an
    unreadable `logging:` block must never leave content capture on."""
    block = raw.get("logging") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        return DEFAULT_LOG_LEVEL, DEFAULT_LOG_FORMAT
    # YAML 1.1 parses `off`/`no` as the boolean False — the user meant the
    # `off` level, so honor it. `on`/`yes` (True) map to nothing sensible and
    # fall through to the default. `format: off` isn't meaningful, so only the
    # level gets the boolean coercion.
    raw_level = block.get("level", DEFAULT_LOG_LEVEL)
    level = "off" if raw_level is False else str(raw_level).strip().lower()
    fmt = str(block.get("format", DEFAULT_LOG_FORMAT)).strip().lower()
    if level not in VALID_LOG_LEVELS:
        level = DEFAULT_LOG_LEVEL
    if fmt not in VALID_LOG_FORMATS:
        fmt = DEFAULT_LOG_FORMAT
    return level, fmt


def read_log_settings(config_file: str | Path = "reflexes.yaml") -> tuple[str, str]:
    """Read `(level, format)` from reflexes.yaml's `logging:` block. Defaults to
    `("warning", "text")`; a read/parse error never raises and never returns a
    more-verbose level than the default (fail safe — stay quiet)."""
    try:
        return _log_settings_from_raw(_load_raw(config_file))
    except Exception:
        return DEFAULT_LOG_LEVEL, DEFAULT_LOG_FORMAT


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
    if not _enabled_from_raw(raw):
        return []  # globally disabled (`enabled: false`) — no bindings; hooks no-op (allow)
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

    # Configure diagnostic logging from the `logging:` block. This is the
    # setup chokepoint every host funnels through, so wiring it here gives
    # a uniform logging regime across all hosts with one call. Guarded so an
    # entrypoint that owns a log file (the Claude Code hook) can configure
    # first with force=True and win; here we never force.
    from reflexes.observability import configure_logging
    configure_logging(config_file)

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
        # No `evaluators:` block → the documented default is CoPE (Zentropi).
        # Configure it explicitly so `_evaluator` is set, exactly as if the user
        # had written `evaluators: {default: {type: zentropi}}`. Returning here
        # instead (the old behavior) left `_evaluator` None, which a host's
        # startup gate reads as a fail-open — a FALSE alarm, since `_get_evaluator`
        # would lazily instantiate the same default and enforcement works. A
        # genuinely missing key still surfaces at eval time (401 → EvaluatorError)
        # and via the doctor, so the safe signal isn't lost — only the false one.
        configure(evaluator="zentropi")
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
