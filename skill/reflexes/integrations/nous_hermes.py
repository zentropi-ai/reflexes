"""NousResearch Hermes Agent integration for Reflexes — DEPRECATED (pre-0.18).

.. deprecated::
   This module targets the **pre-0.18 importlib hook packs**
   (``~/.hermes/hooks/<name>/{HOOK.yaml, handler.py}``), whose events are
   session/agent-loop-level only. The observer-shape / "deny one turn late"
   limitation described below is a property of *that* old hook system, **not
   of Hermes in general**. Hermes >= 0.18 ships per-call **shell hooks**
   (``pre_tool_call``, ``pre_llm_call``, ``pre_verify`` in
   ``~/.hermes/config.yaml``) that support real pre-execution deny + context
   injection at full Claude Code parity — use
   ``reflexes.integrations.nous_hermes_shell`` for 0.18+. This module is kept
   only for legacy pre-0.18 installs.

Hermes (pre-0.18) loads hooks from ``~/.hermes/hooks/<name>/{HOOK.yaml,
handler.py}`` via importlib at startup. Each handler exports::

    def handle(event_type: str, context: Dict[str, Any]) -> Optional[Any]

Those hook events are session/agent-loop-level, NOT per-call:

    gateway:startup, session:start, session:end, session:reset,
    agent:start, agent:step (each turn), agent:end, command:*

In that old system there is no ``pre_tool_call`` / ``pre_llm_call`` /
``post_llm_call`` event, so per-call deny-before-execution isn't achievable
through the importlib hooks alone. ``agent:step`` is the closest analog and
fires after the tool ran — Reflexes observes the turn's tool calls + the LLM
response and:

  * **flag / log actions:** work fully — the integration logs the match
    and (if Hermes' ``emit_collect`` is invoked for ``agent:step``)
    returns a structured note Hermes can surface to the user.
  * **deny action:** becomes "deny on next turn" — Reflexes still
    evaluates and writes a policy-violation note into a side-channel
    (the user-visible warning log + the structured return value); the
    actual block is one round-trip late. This is the documented
    asymmetry; per-call denial waits on either Hermes shipping
    pre-tool-call events upstream or on the Reflexes-as-MCP-server
    integration shape (v0.2 candidates).

This module is the policy-evaluation core. Legacy pre-0.18 installs wrap
it with a thin ``handler.py`` shim in ``~/.hermes/hooks/reflexes/`` that
imports and delegates here (see the Usage example below), so package
upgrades flow automatically without the user re-copying files.

Usage from a Hermes hook directory::

    # ~/.hermes/hooks/reflexes/handler.py
    from reflexes.integrations.nous_hermes import handle as _handle

    def handle(event_type, context):
        return _handle(event_type, context)

Config resolution: integration reads ``$REFLEXES_CONFIG_FILE`` if set,
otherwise defaults to ``~/.reflexes/reflexes.yaml``. Falls back to no-op
(returns None) if the config can't be loaded — keeps Hermes' "callback
crashes are logged and skipped" contract honored at our boundary too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from reflexes.core import safe_acheck, EvaluatorError
from reflexes.config import load_config, ReflexBinding, configure_from_file
from reflexes._context import assemble_task_context

logger = logging.getLogger("reflexes.nous_hermes")

# Lazy module-level cache for parsed bindings. Hermes loads handlers at
# startup; we don't want to re-parse reflexes.yaml on every agent:step.
# Keyed by config path so a user editing the file (rare) and restarting
# Hermes picks up changes naturally.
_BINDINGS_CACHE: dict[str, list[ReflexBinding]] = {}


def _resolve_config_path() -> Path:
    """Where to load reflexes.yaml from.

    Order: REFLEXES_CONFIG_FILE env var → ``~/.reflexes/reflexes.yaml``.
    """
    env_path = os.environ.get("REFLEXES_CONFIG_FILE")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".reflexes" / "reflexes.yaml"


def _load_bindings_safely() -> list[ReflexBinding]:
    """Load + cache bindings; return [] on any failure (no crash, no log
    spam — Hermes' contract is callback crashes are logged + skipped).
    """
    cfg_path = str(_resolve_config_path())
    if cfg_path in _BINDINGS_CACHE:
        return _BINDINGS_CACHE[cfg_path]
    try:
        configure_from_file(cfg_path)
        bindings = load_config(reflexes=[], config_file=cfg_path)
    except Exception as e:
        logger.warning(
            "reflexes config load failed (%s); Hermes integration is no-op", e
        )
        _BINDINGS_CACHE[cfg_path] = []
        return []
    _BINDINGS_CACHE[cfg_path] = bindings
    return bindings


def _extract_user_message(context: dict) -> str | None:
    """Best-effort extraction of the most-recent user message from the
    Hermes ``agent:step`` context.

    Hermes' per-event context schema isn't pinned in source;
    this defensively probes the obvious shapes:
      - ``context["messages"]`` — list of message dicts
      - ``context["user_message"]`` — string
      - ``context["last_user_message"]`` — string
      - ``context["state"]["messages"]`` — nested list
    Returns None if none of these patterns yield a string.
    """
    if not isinstance(context, dict):
        return None
    for key in ("user_message", "last_user_message"):
        v = context.get(key)
        if isinstance(v, str) and v.strip():
            return v
    messages = context.get("messages") or context.get("state", {}).get("messages") or []
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict):
                role = msg.get("role") or msg.get("type")
                if role in ("user", "human"):
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        return content
    return None


def _extract_assistant_message(context: dict) -> str | None:
    """Best-effort extraction of the latest assistant message text."""
    if not isinstance(context, dict):
        return None
    for key in ("response", "assistant_message", "last_assistant_message", "text"):
        v = context.get(key)
        if isinstance(v, str) and v.strip():
            return v
    messages = context.get("messages") or context.get("state", {}).get("messages") or []
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict):
                role = msg.get("role") or msg.get("type")
                if role in ("assistant", "ai"):
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        return content
    return None


def _extract_tool_calls(context: dict) -> list[dict]:
    """Best-effort extraction of tool calls made during this turn.

    Returns a list of ``{"name": str, "args": dict, "result": str | None}``
    dicts. Empty list if none found or context shape doesn't match.
    """
    if not isinstance(context, dict):
        return []
    raw = context.get("tool_calls") or context.get("tools_called") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or tc.get("tool_name") or ""
        args = tc.get("args") or tc.get("arguments") or {}
        # Args may arrive as a JSON string (Hermes is OpenAI-compatible
        # in many places); parse defensively, fall back to raw.
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                pass
        result = tc.get("result") or tc.get("output") or tc.get("response")
        if not isinstance(result, str) and result is not None:
            try:
                result = json.dumps(result)
            except (TypeError, ValueError):
                result = str(result)
        out.append({"name": name, "args": args, "result": result})
    return out


async def _evaluate_step(
    bindings: list[ReflexBinding], context: dict
) -> list[dict]:
    """Run the relevant evaluators against a single agent:step's
    observable content. Returns a list of structured findings for any
    matched policies. Used both for log-only emission and for the
    return-value path Hermes ``emit_collect`` consumes.
    """
    user_msg = _extract_user_message(context)
    ctx = assemble_task_context(primary_user_prompt=user_msg) or None
    findings: list[dict] = []

    # Group bindings by trigger.
    by_trigger: dict[str, list[ReflexBinding]] = {}
    for b in bindings:
        by_trigger.setdefault(b.trigger, []).append(b)

    # before_tool_call + after_tool_call: evaluate per tool call.
    tool_calls = _extract_tool_calls(context)
    for tc in tool_calls:
        before_tool = by_trigger.get("before_tool_call", [])
        if before_tool:
            content = json.dumps({"tool_name": tc["name"], "args": tc["args"]})
            findings += await _check_bindings(
                before_tool, content, ctx, "Tool call",
                deny_note="deny-on-next-turn (tool already executed)",
            )
        after_tool = by_trigger.get("after_tool_call", [])
        if after_tool and tc["result"] is not None:
            content = f"{tc['name']} returned: {tc['result']}"
            findings += await _check_bindings(
                after_tool, content, ctx, "Tool result",
                deny_note=None,
            )

    # before_response: evaluate the assistant message.
    assistant = _extract_assistant_message(context)
    if assistant:
        before_response = by_trigger.get("before_response", [])
        if before_response:
            findings += await _check_bindings(
                before_response, assistant, ctx, "Agent response",
                deny_note="deny-on-next-turn (response already produced)",
            )

    # on_input: evaluate the user message (deferred to session:start
    # ideally; do here as fallback when no earlier hook fired).
    if user_msg:
        on_input = by_trigger.get("on_input", [])
        if on_input:
            findings += await _check_bindings(
                on_input, user_msg, None, "Content",
                deny_note="deny-on-next-turn (input already accepted)",
            )

    return findings


async def _check_bindings(
    bindings: list[ReflexBinding],
    content: str,
    context: str | None,
    check_type: str,
    *,
    deny_note: str | None,
) -> list[dict]:
    """Evaluate bindings against content, return structured findings.

    Each finding is a dict like::
        {"policy": str, "action": str, "trigger": str,
         "confidence": float, "criteria": str, "asymmetry_note": str | None}

    Hermes integration semantics: deny is observer-shape (the tool/response
    already happened), so the finding's ``asymmetry_note`` documents the
    one-turn-late nature explicitly. Flag/log fire fully.
    """
    findings: list[dict] = []
    for binding in bindings:
        verdict = await safe_acheck(
            policy=binding.policy.criteria,
            content=content,
            policy_name=binding.policy.name,
            context=context,
            check_type=check_type,
        )
        if isinstance(verdict, EvaluatorError):
            logger.warning(
                "evaluator failed for %s: %s",
                verdict.policy_name, verdict.error[:100]
            )
            continue
        if verdict.label != 1:
            continue
        log_msg = (
            "Reflex %s matched (action=%s, confidence=%.2f)"
            % (binding.policy.name, binding.action, verdict.confidence)
        )
        if binding.action == "deny":
            logger.warning("%s [%s]", log_msg, deny_note or "")
        elif binding.action == "flag":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)
        findings.append({
            "policy": binding.policy.name,
            "action": binding.action,
            "trigger": binding.trigger,
            "confidence": verdict.confidence,
            "criteria": binding.policy.criteria,
            "asymmetry_note": deny_note if binding.action == "deny" else None,
        })
    return findings


def handle(event_type: str, context: dict | None = None) -> Any:
    """Hermes hook entry point.

    Bind to ``agent:step`` (per-turn observation) for the launch slate.
    Optionally also bind to ``session:start`` / ``session:end`` for
    cache-warming or cleanup, but those are no-ops in v0.1.

    Returns:
      * ``None`` when no policy matched (Hermes ``emit`` discards;
        ``emit_collect`` skips None returns).
      * ``{"reflex_findings": [...]}`` when one or more policies matched.
        Hermes' ``emit_collect`` callers can inspect this; ``emit``
        callers see it discarded but the warnings are still logged.
    """
    if event_type not in ("agent:step", "agent:start", "agent:end",
                          "session:start", "session:end", "session:reset"):
        return None
    if event_type != "agent:step":
        # Only agent:step has the per-turn observable content we evaluate.
        # Other lifecycle events are accepted (no-op) for forward-compat.
        return None

    bindings = _load_bindings_safely()
    if not bindings:
        return None

    try:
        findings = asyncio.run(_evaluate_step(bindings, context or {}))
    except Exception as e:
        # Honor Hermes' "callback crashes are logged and skipped" contract
        # at our boundary too — never let a Reflexes failure crash Hermes.
        logger.warning("nous_hermes hook handler failed: %s", e)
        return None

    if not findings:
        return None
    return {"reflex_findings": findings}
