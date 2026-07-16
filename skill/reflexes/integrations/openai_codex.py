"""OpenAI Codex integration for Reflexes.

Codex uses the same hook protocol as Claude Code — JSON on stdin,
JSON on stdout. This module reuses the claude_code integration.

Trigger mapping:
  on_input         -> UserPromptSubmit hook
  before_tool_call -> PreToolUse hook
  after_tool_call  -> PostToolUse hook
  before_response  -> Stop hook

Usage:
  python3 -m reflexes.integrations.openai_codex --config reflexes.yaml --reflexes safety

Hook config goes in hooks.json:
  User level:    ~/.codex/hooks.json
  Project level: <repo>/.codex/hooks.json
"""

# Codex uses the same hook protocol as Claude Code — reuse its main, but tag
# log lines as `codex` (Codex and Claude Code on one machine share debug.log).
from reflexes.integrations.claude_code import main as _cc_main


def main(host: str = "codex"):
    _cc_main(host=host)


if __name__ == "__main__":
    main()
