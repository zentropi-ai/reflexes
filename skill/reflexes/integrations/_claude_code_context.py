"""Host-context resolution for the Claude Code integration.

Resolves the three context sources used by ``reflexes._context.assemble_task_context``
for Claude Code specifically: project CLAUDE.md (or AGENTS.md if CLAUDE.md
is just a pointer), user-global ~/.claude/CLAUDE.md, and the initial
user prompt parsed from the session transcript JSONL.

The actual PRIMARY/BACKGROUND framing lives in the shared
``reflexes._context`` module; this file only handles Claude-Code-specific
disk + transcript reads.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("reflexes.claude_code.context")


# Per-source character caps. The shared assembler caps again at
# its own per-section budgets, but capping at read time avoids
# loading 100KB of CLAUDE.md into memory only to drop most of it.
PROJECT_INSTRUCTIONS_READ_CAP = 8000
USER_INSTRUCTIONS_READ_CAP = 4000
INITIAL_PROMPT_READ_CAP = 4000

# A CLAUDE.md that's just a "@AGENTS.md" pointer should resolve to the
# referenced file. Threshold is small — substantive CLAUDE.md files are
# always longer than this.
CLAUDE_MD_POINTER_THRESHOLD = 200


def _truncate(text: str, max_chars: int) -> str:
    """Head-truncate ``text`` at the read boundary (the shared assembler
    will re-truncate at its tighter per-section budgets)."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def read_project_instructions(cwd: str | None) -> str | None:
    """Read CLAUDE.md (or AGENTS.md if CLAUDE.md is just a pointer) at cwd.

    Returns the section body (already truncated) or None if neither file
    exists. The Claude Code convention is that CLAUDE.md may contain
    `@AGENTS.md` to delegate; we follow the reference one level deep.
    """
    if not cwd:
        return None
    base = Path(cwd)
    if not base.is_dir():
        return None

    claude_md = base / "CLAUDE.md"
    agents_md = base / "AGENTS.md"

    body = None
    if claude_md.exists():
        try:
            body = claude_md.read_text(encoding="utf-8")
        except OSError as e:
            logger.debug("Could not read %s: %s", claude_md, e)
            body = None

        # If CLAUDE.md is short and references @AGENTS.md, follow it.
        if (
            body is not None
            and len(body) < CLAUDE_MD_POINTER_THRESHOLD
            and "@AGENTS.md" in body
            and agents_md.exists()
        ):
            try:
                body = agents_md.read_text(encoding="utf-8")
            except OSError as e:
                logger.debug("Could not read %s: %s", agents_md, e)

    elif agents_md.exists():
        try:
            body = agents_md.read_text(encoding="utf-8")
        except OSError as e:
            logger.debug("Could not read %s: %s", agents_md, e)
            body = None

    if not body or not body.strip():
        return None
    return _truncate(body, PROJECT_INSTRUCTIONS_READ_CAP)


def read_user_global_instructions() -> str | None:
    """Read ``~/.claude/CLAUDE.md`` if it exists, else None."""
    path = Path.home() / ".claude" / "CLAUDE.md"
    if not path.exists():
        return None
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug("Could not read %s: %s", path, e)
        return None
    if not body.strip():
        return None
    return _truncate(body, USER_INSTRUCTIONS_READ_CAP)


def _extract_text_from_message_content(content) -> str | None:
    """Pull a plain-text rendering out of a Claude Code message content field.

    Content may be a string, or a list of blocks with type+text/etc.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def read_initial_user_prompt(transcript_path: str | None) -> str | None:
    """Return the first user-message text from the session transcript JSONL.

    The transcript file is owned by Claude Code; it appends one JSON
    object per line in chronological order. We scan top-down for the
    first entry of type "user" and pull the message text out. Internal
    "queue-operation" / "ai-title" / "last-prompt" entries are skipped.
    Best-effort: returns None if the file is missing, empty, or has no
    user message yet.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                text = _extract_text_from_message_content(msg.get("content"))
                if text:
                    return _truncate(text, INITIAL_PROMPT_READ_CAP)
    except OSError as e:
        logger.debug("Could not read transcript %s: %s", transcript_path, e)
    return None


def _combine_standing_instructions(project: str | None, user_global: str | None) -> str | None:
    """Combine project + user-global instructions into one labeled string."""
    parts = []
    if project:
        parts.append(f"Project (CLAUDE.md):\n{project}")
    if user_global:
        parts.append(f"User-global (~/.claude/CLAUDE.md):\n{user_global}")
    if not parts:
        return None
    return "\n\n".join(parts)


def assemble_for_claude_code(
    *,
    cwd: str | None,
    transcript_path: str | None,
    recent_user_prompt: str | None,
) -> str:
    """Resolve all Claude Code context sources and call the shared assembler.

    Returns the PRIMARY/BACKGROUND-framed task context block from
    ``reflexes._context.assemble_task_context``, ready to pass as the
    ``context`` argument to ``safe_acheck`` with
    ``context_label="Task context"``.
    """
    from reflexes._context import assemble_task_context

    standing = _combine_standing_instructions(
        read_project_instructions(cwd),
        read_user_global_instructions(),
    )
    initial = read_initial_user_prompt(transcript_path)
    return assemble_task_context(
        primary_user_prompt=recent_user_prompt,
        initial_user_prompt=initial,
        standing_instructions=standing,
    )
