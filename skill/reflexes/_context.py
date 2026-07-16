"""Cross-framework task-context assembly for evaluator enrichment.

Each integration resolves three context sources using whatever's
idiomatic for its host (disk reads for subprocess hosts, in-process
attribute access for Python frameworks). This module then composes
the resolved strings into a single block with self-describing section
labels ("Current request:", "Original task:", "Standing instructions:"),
the current request first so the evaluator knows which signal is
load-bearing (SPEC §5.1).

The most-recent user request is the primary
signal. Standing instructions and the original task are supporting
context. The assembled block is intended to be passed as the
``context`` argument to ``safe_acheck`` with
``context_label="Task context"`` — the existing ``_assemble_content``
preamble in ``core.py`` then frames it as "evaluate the {check_type},
not the Task context."
"""
from __future__ import annotations


# Per-section size budgets (chars). Weighted toward the primary slot —
# the anti-pollution behavior ensures the most-recent prompt
# must not be starved by a large CLAUDE.md or initial task brief.
PRIMARY_PROMPT_MAX = 4000          # the user's current/most-recent request
INITIAL_PROMPT_MAX = 1500          # initial task brief (background)
STANDING_INSTRUCTIONS_MAX = 1500   # CLAUDE.md / Agent.instructions / etc.


def _truncate(text: str, max_chars: int) -> str:
    """Head-truncate ``text`` to ``max_chars`` with a marker."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[...truncated]"


def assemble_task_context(
    *,
    primary_user_prompt: str | None,
    initial_user_prompt: str | None = None,
    standing_instructions: str | None = None,
) -> str:
    """Assemble a task-context block with self-describing section labels.

    Designed so the evaluator can distinguish the load-bearing signal
    (the user's current request, rendered first) from supporting
    context. Each section is included only when its source has content.
    Returns "" if nothing was provided — callers should treat empty as
    "no enrichment available" and pass None to the evaluator.

    Section labels (self-describing; see SPEC §5.1):
      - "Current request:"       — the user's most-recent request
      - "Original task:"         — the first user message of the session
      - "Standing instructions:" — host-native standing instructions

    Args:
        primary_user_prompt: The user's most-recent request. Always
            rendered first, under "Current request:". Truncated to
            PRIMARY_PROMPT_MAX (the most generous budget so it is never
            starved by large background sections).
        initial_user_prompt: The first user message of the session, if
            available and distinct from the current request. Rendered
            under "Original task:". Truncated to INITIAL_PROMPT_MAX.
            Suppressed if identical to primary (avoids double-printing
            short single-turn sessions).
        standing_instructions: Framework-native standing instructions
            (CLAUDE.md body for Claude Code, Agent.instructions for
            OpenAI Agents, agent.instruction for Google ADK, etc.).
            Rendered under "Standing instructions:". Truncated most
            aggressively at STANDING_INSTRUCTIONS_MAX so a large
            CLAUDE.md cannot crowd out the current request.
    """
    sections: list[str] = []

    if primary_user_prompt:
        sections.append(
            f"Current request:\n"
            f"{_truncate(primary_user_prompt, PRIMARY_PROMPT_MAX)}"
        )

    if (
        initial_user_prompt
        and initial_user_prompt.strip() != (primary_user_prompt or "").strip()
    ):
        sections.append(
            f"Original task:\n"
            f"{_truncate(initial_user_prompt, INITIAL_PROMPT_MAX)}"
        )
    if standing_instructions:
        sections.append(
            f"Standing instructions:\n"
            f"{_truncate(standing_instructions, STANDING_INSTRUCTIONS_MAX)}"
        )

    return "\n\n".join(sections)
