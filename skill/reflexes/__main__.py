"""Entry point for `python3 -m reflexes` and the `reflexes` console script.

In v0.1, `reflexes init` was retired. Install is now an agent-driven
procedure documented in `SKILL.md` (the user asks an AI agent to install
Reflexes; the agent reads SKILL.md and follows the numbered steps using
its own Bash + Python tools). This entry point exists for backward-
compat: anyone who runs `reflexes` or `reflexes init` from muscle memory
gets a clear pointer to SKILL.md instead of a `command-not-found`.
"""
from __future__ import annotations

import sys


_USAGE = """\
Reflexes — open standard for AI agent policy checks.

The Python `reflexes init` command was retired in v0.1. Install
is now agent-driven via SKILL.md.

  Quickstart: ask your AI agent to "install reflexes" — it will fetch
  SKILL.md and follow the numbered install procedure.

  Get SKILL.md (and the rest of the skill) from the source repo —
  https://github.com/zentropi-ai/reflexes (skill/SKILL.md) — or from
  https://reflexes.sh/SKILL.md.

  Post-install verification:

      reflexes doctor        # config parses, evaluator live, hooks wired

  For Python framework wiring (OpenAI Agents SDK, Claude Agent SDK,
  Google ADK, LangGraph, NeMo Guardrails, NousResearch Hermes), see
  README.md "Quick Install — via Python Frameworks."

Hook subprocess entry points (used by Claude Code / OpenAI Codex hooks
configured per SKILL.md):

  python3 -m reflexes.integrations.claude_code  --config ...
  python3 -m reflexes.integrations.openai_codex --config ...
"""


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args and args[0] == "doctor":
        from reflexes.doctor import run_doctor
        import argparse
        parser = argparse.ArgumentParser(prog="reflexes doctor")
        parser.add_argument("--config", default=None, help="Path to reflexes.yaml")
        parser.add_argument("--reflexes", default="", help="Comma-separated group names")
        parser.add_argument("--check-update", action="store_true",
                            help="Only check for a newer Reflexes version (fast; no evaluator probe).")
        opts = parser.parse_args(args[1:])
        if opts.check_update:
            # Mirror scripts/reflexes-doctor --check-update: fast, update-only,
            # fail-silent, always exit 0. Uses the shared status helper so it
            # never claims "up to date" on a failed check.
            try:
                from reflexes import __version__
                from reflexes.update import update_status_line
                print(update_status_line(__version__))
            except Exception:
                pass
            return 0
        groups = [g.strip() for g in opts.reflexes.split(",") if g.strip()]
        return run_doctor(config=opts.config, groups=groups)
    if args and args[0] == "init":
        sys.stderr.write(
            "[reflexes] `reflexes init` was retired in v0.1. The install\n"
            "[reflexes] procedure is now agent-driven via SKILL.md — ask your\n"
            "[reflexes] AI agent to 'install reflexes' and it will follow the\n"
            "[reflexes] numbered steps in SKILL.md (github.com/zentropi-ai/reflexes).\n"
        )
        return 2
    sys.stdout.write(_USAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
