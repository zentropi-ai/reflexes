"""End-to-end test of the Claude Code hook integration.

Exercises the full deny pathway: synthetic Stop hook payload containing
a timeline-committal `last_assistant_message` → Reflexes hook subprocess
→ CoPE evaluation → deny response containing the policy criteria text
that Claude Code natively re-injects so the agent can self-correct on
its next turn.

Skipped without ZENTROPI_API_KEY — the test exercises the real CoPE
endpoint, not a mock. The regression-guarded version of the manual
end-to-end validation.
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
def claude_code_env(tmp_path, monkeypatch):
    """Isolated HOME + reflexes.yaml referencing no-timeline-commitments."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    # Bundle a real policy + reflexes.yaml so load_config finds something.
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
def test_claude_code_stop_hook_denies_timeline_commitment(claude_code_env):
    """Stop-hook payload with a timeline-committal `last_assistant_message`
    should produce a deny response with the policy criteria text.

    This is the formal regression-guarded version of the manual
    end-to-end validation. Asserts:
      - subprocess exits 0 (no host crash)
      - stdout is JSON
      - the response contains a `block` decision with the policy text
      - the policy text is the no-timeline-commitments criteria
    """
    payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": "cc-e2e-test-session",
        "transcript_path": str(claude_code_env["home"] / "transcript.jsonl"),
        "cwd": str(claude_code_env["home"]),
        "permission_mode": "default",
        "last_assistant_message": (
            "We're planning to ship the new analytics feature in Q3 2024."
        ),
    })

    result = subprocess.run(
        [
            sys.executable, "-m", "reflexes.integrations.claude_code",
            "--config", str(claude_code_env["config"]),
        ],
        input=payload,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=30,
    )

    assert result.returncode == 0, (
        f"hook subprocess crashed: stderr={result.stderr!r}"
    )

    response = json.loads(result.stdout.strip() or "{}")
    # Stop-event deny response shape (from build_deny_response):
    #   {"decision": "block", "reason": <feedback>}
    assert response.get("decision") == "block", (
        f"expected deny response with decision=block; got {response!r}"
    )
    reason = response.get("reason", "")
    # The feedback message MUST contain the policy criteria text so the
    # agent can read why it was denied and self-correct on next turn.
    assert "Label = 1" in reason, (
        f"deny reason missing policy criteria text; got: {reason[:200]!r}"
    )
    assert "timeline" in reason.lower(), (
        f"deny reason doesn't reference the timeline policy; got: {reason[:200]!r}"
    )


@needs_zentropi
@pytest.mark.integration
def test_claude_code_stop_hook_passes_compliant_response(claude_code_env):
    """Compliant `last_assistant_message` (no timeline commitment) should
    pass through with `{}` — no deny, no flag.
    """
    payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": "cc-e2e-test-pass",
        "transcript_path": str(claude_code_env["home"] / "transcript.jsonl"),
        "cwd": str(claude_code_env["home"]),
        "permission_mode": "default",
        "last_assistant_message": (
            "The release timeline is still being finalized; "
            "I'll share details once they're confirmed."
        ),
    })

    result = subprocess.run(
        [
            sys.executable, "-m", "reflexes.integrations.claude_code",
            "--config", str(claude_code_env["config"]),
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
