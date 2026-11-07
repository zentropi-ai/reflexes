"""Live end-to-end tests for the Microsoft AutoGen integration.

Drives the intervention handler with real AutoGen message objects and a real
CoPE evaluator. This exercises `_extract_content`'s type dispatch against the
actual message classes (the drift-sensitive surface) without needing a model —
the handler evaluates messages directly.

Opt-in: requires `autogen-core` + `ZENTROPI_API_KEY` (the CoPE evaluator).
Skipped otherwise.

Note the documented A gap: AutoGen deny returns `DropMessage` — the message is
silently dropped with no feedback, so there is no self-correction (the runtime
has no revise path). These tests assert the drop, not a revise.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("autogen_core")

_needs = pytest.mark.skipif(
    "ZENTROPI_API_KEY" not in os.environ, reason="ZENTROPI_API_KEY required",
)
pytestmark = [pytest.mark.integration, _needs]

_DESTRUCTIVE = (
    "# Criteria\n\n## Overview\nDetects a request or action to delete or destroy "
    "files or data.\n\n## Definition of Labels\n### (1): Destructive\nDeletes, removes, "
    "or destroys files or data.\n### (0): Not Destructive\nAnything else.\n"
)
_TIMELINE = (
    "# Criteria\n\n## Overview\nDetects a commitment to a specific delivery date, "
    "deadline, or timeline.\n\n## Definition of Labels\n### (1): Timeline Commitment\n"
    "Content commits to a specific date, deadline, or delivery schedule.\n"
    "### (0): No Timeline Commitment\nContent discusses timing vaguely or not at all.\n"
)


def _handler(tmp_path, trigger, policy_text, name):
    from reflexes.integrations.autogen import create_intervention_handler

    pol = tmp_path / "policies"
    pol.mkdir(exist_ok=True)
    (pol / f"{name}.md").write_text(policy_text)
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "reflexes:\n  default:\n"
        f"    - policy: ./policies/{name}.md\n      name: {name}\n"
        f"      trigger: {trigger}\n      action: deny\n"
        "evaluators:\n  default:\n    type: zentropi\n    model: cope-latest\n"
        "    api_key_env: ZENTROPI_API_KEY\n"
    )
    return create_intervention_handler(config_file=str(cfg))


def test_on_input_deny_drops(tmp_path):
    from autogen_core import DropMessage
    from autogen_core.models import UserMessage

    handler = _handler(tmp_path, "on_input", _DESTRUCTIVE, "no-delete")
    msg = UserMessage(content="Delete every file on the production server now.", source="user")
    out = asyncio.run(handler.on_send(msg, message_context=None, recipient=None))
    assert out is DropMessage


def test_benign_input_passes(tmp_path):
    from autogen_core import DropMessage
    from autogen_core.models import UserMessage

    handler = _handler(tmp_path, "on_input", _DESTRUCTIVE, "no-delete")
    msg = UserMessage(content="What is the capital of France?", source="user")
    out = asyncio.run(handler.on_send(msg, message_context=None, recipient=None))
    assert out is not DropMessage


def test_before_response_deny_drops_with_context(tmp_path, monkeypatch):
    import reflexes.integrations.autogen as ag
    from autogen_core import DropMessage
    from autogen_core.models import UserMessage
    from autogen_agentchat.messages import TextMessage

    seen = []
    import reflexes.core as _core
    _orig = _core.safe_acheck

    async def _rec(**kw):
        seen.append(kw.get("context"))
        return await _orig(**kw)

    monkeypatch.setattr(_core, "safe_acheck", _rec)

    handler = _handler(tmp_path, "before_response", _TIMELINE, "no-timeline")

    # First a user message (no on_input binding) so the handler records the
    # originating request for task context.
    asyncio.run(handler.on_send(
        UserMessage(content="When exactly do we launch the dashboard?", source="user"),
        message_context=None, recipient=None))
    # Then the agent's committal response -> before_response deny -> dropped.
    resp = TextMessage(content="The launch date is March 3, 2027.", source="assistant")
    out = asyncio.run(handler.on_response(resp, sender=None, recipient=None))
    assert out is DropMessage

    # B: the originating request reached the evaluator as labeled context.
    joined = " ".join(c or "" for c in seen)
    assert "Current request:" in joined
    assert "launch the dashboard" in joined
