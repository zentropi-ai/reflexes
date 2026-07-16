"""End-to-end test of the OpenAI Codex hook integration.

Parallel to ``tests/test_claude_code_e2e.py`` — exercises the full
deny pathway with a synthetic Stop hook payload, but invokes the
``reflexes.integrations.openai_codex`` module (the 24-line delegator)
to prove the wiring works end-to-end through that entry point, not
just through Claude Code's.

Codex shares Claude Code's hook protocol, so the response shape is
identical: ``{"decision": "block", "reason": <policy text>}`` on deny;
``{}`` on pass. This is the regression-guarded version of the manual
"set up codex hooks.json + ask codex something timeline-committal"
smoke test, with no codex CLI dependency.

Skipped without ZENTROPI_API_KEY — exercises the real CoPE endpoint.
Exercises codex's install + run flow against a real CoPE call.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
needs_zentropi = pytest.mark.skipif(
    "ZENTROPI_API_KEY" not in os.environ,
    reason="ZENTROPI_API_KEY not set",
)


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    """Isolated HOME + reflexes.yaml referencing no-timeline-commitments."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".codex").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    reflexes_dir = fake_home / ".reflexes"
    policies_dir = reflexes_dir / "policies"
    policies_dir.mkdir(parents=True)

    (policies_dir / "no-timeline-commitments.md").write_text(
        (Path(__file__).resolve().parent / "fixtures" / "no-timeline-commitments.md").read_text(),
    )

    config_path = reflexes_dir / "reflexes.yaml"
    config_path.write_text(
        "reflexes:\n"
        "  default:\n"
        "    - policy: ./policies/no-timeline-commitments.md\n"
        "      name: no-timeline-commitments\n"
        "      trigger: before_response\n"
        "      action: deny\n"
        "evaluators:\n"
        "  default:\n"
        "    type: zentropi\n"
        "    model: cope-latest\n"
        "    api_key_env: ZENTROPI_API_KEY\n"
    )

    return {"home": fake_home, "config": config_path}


@needs_zentropi
@pytest.mark.integration
def test_codex_stop_hook_denies_timeline_commitment(codex_env):
    """Codex Stop-hook payload with timeline-committal `last_assistant_message`
    → deny response with the policy criteria text.

    Asserts the same shape as the Claude Code e2e test, but invoked through
    ``reflexes.integrations.openai_codex`` to prove the delegation surfaces
    the deny correctly under the codex entry point."""
    payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": "codex-e2e-test-session",
        "transcript_path": str(codex_env["home"] / "transcript.jsonl"),
        "cwd": str(codex_env["home"]),
        "permission_mode": "default",
        "last_assistant_message": (
            "We're planning to ship the new analytics feature in Q3 2024."
        ),
    })

    result = subprocess.run(
        [
            sys.executable, "-m", "reflexes.integrations.openai_codex",
            "--config", str(codex_env["config"]),
        ],
        input=payload,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=30,
    )

    assert result.returncode == 0, (
        f"codex hook subprocess crashed: stderr={result.stderr!r}"
    )

    response = json.loads(result.stdout.strip() or "{}")
    assert response.get("decision") == "block", (
        f"expected deny response with decision=block; got {response!r}"
    )
    reason = response.get("reason", "")
    assert "Label = 1" in reason, (
        f"deny reason missing policy criteria text; got: {reason[:200]!r}"
    )
    assert "timeline" in reason.lower(), (
        f"deny reason doesn't reference the timeline policy; got: {reason[:200]!r}"
    )


@needs_zentropi
@pytest.mark.integration
def test_codex_stop_hook_passes_compliant_response(codex_env):
    """Compliant `last_assistant_message` (no timeline commitment) should
    pass through with `{}` — same contract as Claude Code."""
    payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": "codex-e2e-test-pass",
        "transcript_path": str(codex_env["home"] / "transcript.jsonl"),
        "cwd": str(codex_env["home"]),
        "permission_mode": "default",
        "last_assistant_message": (
            "The release timeline is still being finalized; "
            "I'll share details once they're confirmed."
        ),
    })

    result = subprocess.run(
        [
            sys.executable, "-m", "reflexes.integrations.openai_codex",
            "--config", str(codex_env["config"]),
        ],
        input=payload, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=30,
    )

    assert result.returncode == 0
    response = json.loads(result.stdout.strip() or "{}")
    assert response == {} or "decision" not in response, (
        f"hedged response should pass; got {response!r}"
    )
