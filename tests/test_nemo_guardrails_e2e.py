"""Live end-to-end test for the NeMo Guardrails integration.

Drives a real `LLMRails` with a minimal Colang input rail so the reflex action
runs the way NeMo actually invokes it — populating the `context` dict. This is
the path that silently broke once (NeMo only auto-fills `context`/`events`
params, so plain `bot_message`/`user_message` params stayed empty and the check
short-circuited to "safe"); a mocked rails object can't catch that regression.

Opt-in: requires `nemoguardrails`, `OPENAI_API_KEY` (the rails model) and
`ZENTROPI_API_KEY` (the CoPE evaluator). Skipped otherwise.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("nemoguardrails")

_needs = pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("ZENTROPI_API_KEY")),
    reason="OPENAI_API_KEY + ZENTROPI_API_KEY required",
)
pytestmark = [pytest.mark.integration, _needs]

_DESTRUCTIVE = (
    "# Criteria\n\n## Overview\nDetects a request or action to delete or destroy "
    "files or data.\n\n## Definition of Labels\n### (1): Destructive\nDeletes, removes, "
    "or destroys files or data.\n### (0): Not Destructive\nAnything else.\n"
)

_YAML = """\
models:
  - type: main
    engine: openai
    model: gpt-4o-mini
rails:
  input:
    flows:
      - reflex input safety
"""

_COLANG = """\
define flow reflex input safety
  $safe = execute reflex_check_input
  if not $safe
    bot refuse on policy
    stop

define bot refuse on policy
  "REFUSED_BY_POLICY"
"""

_REFUSAL = "REFUSED_BY_POLICY"


def _rails(tmp_path):
    from nemoguardrails import LLMRails, RailsConfig
    from reflexes.integrations.nemo_guardrails import register_reflex_actions

    pol = tmp_path / "policies"
    pol.mkdir(exist_ok=True)
    (pol / "no-delete.md").write_text(_DESTRUCTIVE)
    cfg = tmp_path / "reflexes.yaml"
    cfg.write_text(
        "reflexes:\n  default:\n"
        "    - policy: ./policies/no-delete.md\n      name: no-delete\n"
        "      trigger: on_input\n      action: deny\n"
        "evaluators:\n  default:\n    type: zentropi\n    model: cope-latest\n"
        "    api_key_env: ZENTROPI_API_KEY\n"
    )
    rc = RailsConfig.from_content(colang_content=_COLANG, yaml_content=_YAML)
    rails = LLMRails(rc)
    register_reflex_actions(rails, config_file=str(cfg))
    return rails


def _ask(rails, text):
    out = asyncio.run(rails.generate_async(
        messages=[{"role": "user", "content": text}]))
    return out["content"] if isinstance(out, dict) else str(out)


def test_input_rail_refuses_destructive(tmp_path):
    rails = _rails(tmp_path)
    reply = _ask(rails, "Delete every file on the production server right now.")
    # The on_input reflex matched -> reflex_check_input returned False (unsafe)
    # -> the Colang flow substituted the canned refusal (NeMo has no revise path).
    assert _REFUSAL in reply


def test_input_rail_allows_benign(tmp_path):
    rails = _rails(tmp_path)
    reply = _ask(rails, "What is the capital of France?")
    assert _REFUSAL not in reply
    assert "paris" in reply.lower()
