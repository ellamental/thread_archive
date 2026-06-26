"""Title + one-line-description extraction for Claude Code import.

Ported from canonical ``streaming/incremental_import/_titles.py`` (+ the
command-title helper from ``importer.py``): derive a thread title or description
from raw CC session lines — the ``custom-title`` / ``ai-title`` rows CC writes,
else the first non-XML line of the first user message (or a bare slash-command's
name). Pure helpers; no store access, so they stay separately testable.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from thread_import.parsers.base import NormalizedMessage
from thread_import.parsers.claude_code import ClaudeCodeParser

# CC records a slash-command invocation as a user turn whose content is
# ``<command-name>/x</command-name>`` (plus optional args). The parser strips those
# tags, leaving the turn with empty content_text — and the next turn is usually
# injected content (a skill doc). Recover the command so it, not the injected doc,
# becomes the title.
_CC_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/[^<\n]+?)\s*</command-name>")
_CC_COMMAND_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)


def _cc_command_title(msg: NormalizedMessage) -> Optional[str]:
    """A slash-command title (e.g. ``/garden``) for a user turn whose raw content
    is a bare CC command invocation, else None. Keeps the injected skill doc that
    follows the command out of the thread list."""
    provider_data = msg.get("provider_data")
    if not isinstance(provider_data, dict):
        return None
    raw_line = provider_data.get("line")
    if not isinstance(raw_line, dict):
        return None
    message = raw_line.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    name_match = _CC_COMMAND_NAME_RE.search(content)
    if not name_match:
        return None
    title = name_match.group(1).strip()
    args_match = _CC_COMMAND_ARGS_RE.search(content)
    if args_match:
        args = args_match.group(1).strip()
        if args:
            title = f"{title} {args}"
    return title[:100]


def _extract_ai_title(lines: list[dict]) -> Optional[str]:
    """The current ``ai-title`` value from a CC JSONL. CC writes a single-row
    ``{"type": "ai-title", "aiTitle": "..."}`` line each time the auto-titler
    refines the title; the **last** such row wins."""
    latest: Optional[str] = None
    for line in lines:
        if line.get("type") == "ai-title":
            value = line.get("aiTitle")
            if isinstance(value, str) and value.strip():
                latest = value.strip()
    return latest


def _extract_custom_title(lines: list[dict]) -> Optional[str]:
    """The latest user rename from a CC JSONL ``custom-title`` row. The **last**
    such row wins; a returned ``""`` is CC's "clear the rename" signal."""
    latest: Optional[str] = None
    for line in lines:
        if line.get("type") == "custom-title":
            value = line.get("customTitle")
            if isinstance(value, str):
                latest = value.strip()
    return latest


def extract_session_title(lines: list[dict]) -> Optional[str]:
    """The effective CC session title: a non-empty user rename (``custom-title``)
    wins over the auto-titler's ``ai-title``, matching CC's reader precedence.
    Returns None when neither is present."""
    custom = _extract_custom_title(lines)
    if custom:
        return custom
    return _extract_ai_title(lines)


def _title_from_messages(messages: list[NormalizedMessage]) -> Optional[str]:
    """First-user-message title fallback: the first non-XML line of the first user
    message (truncated to 100 + "..."), or a bare slash-command's name. Returns
    None when no user message yields a title."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text_blob = msg.get("content_text", "").strip()
        if not text_blob:
            # Bare slash-command (e.g. `/garden`): title it with the command, not
            # the skill doc that gets injected on the next turn.
            cmd_title = _cc_command_title(msg)
            if cmd_title:
                return cmd_title
            continue
        for line in text_blob.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("<") and ">" in stripped:
                continue
            title = stripped[:100]
            if len(stripped) > 100:
                title += "..."
            return title
    return None


def extract_title(lines: list[dict]) -> str:
    """A title for a new thread. Preferred: the session title CC stores (a user
    rename wins over the auto-titler). Falls back to the first non-XML line of the
    first user message, then to ``"Claude Code Session"``."""
    resolved = extract_session_title(lines)
    if resolved:
        return resolved[:100]

    parser = ClaudeCodeParser()
    session_data = {
        "provider": "claude-code",
        "sessions": [{"session_id": "temp", "project": "temp", "lines": lines[:20]}],
    }
    try:
        messages = parser.parse_export(session_data)
    except Exception:
        return "Claude Code Session"
    return _title_from_messages(messages) or "Claude Code Session"


def _user_content_texts(content: Any) -> list[str]:
    """The text strings of a CC user-message ``content``: the string itself when
    content is a str, or every ``{"type": "text"}`` block's text when it is a list."""
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
    return texts


def _description_from_texts(texts: list[str]) -> Optional[str]:
    """The first meaningful (non-blank, non-XML, non-continuation) line across these
    text blocks, truncated to 200 (+"..."). Returns None when none match."""
    for t in texts:
        for text_line in t.split("\n"):
            stripped = text_line.strip()
            if not stripped:
                continue
            if stripped.startswith("<") and ">" in stripped:
                continue
            if stripped.startswith("This session is being continued"):
                continue
            if len(stripped) > 200:
                return stripped[:197] + "..."
            return stripped
    return None


def extract_description(lines: list[dict]) -> Optional[str]:
    """A one-line description from the first user message: its first meaningful
    line of content, truncated to 200 chars. None when none found."""
    for line in lines[:20]:
        if line.get("type") != "user":
            continue
        texts = _user_content_texts(line.get("message", {}).get("content", ""))
        desc = _description_from_texts(texts)
        if desc is not None:
            return desc
    return None
