"""IDE-context extraction and timestamp helpers for the Claude Code parser.

These are stateless, pure helpers lifted out of ``ClaudeCodeParser`` /
``claude_code.py``: the IDE-context tag extraction (opened files, selections)
plus the small timestamp-normalization helpers. They take their inputs
explicitly and carry no instance state. ``claude_code.py`` re-imports them so
``parent.<name>`` access and existing import sites keep resolving to the same
objects; ``claude_code_blocks.py`` imports ``_extract_ide_context`` from here
at module level (it previously did a deferred import from ``claude_code`` to
dodge a circular import — that cycle is gone now).

Behavior is identical to the in-module originals: every emitted block shape and
return value is unchanged.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from thread_import.timestamps import parse_timestamp, parse_timestamp_iso


def _parse_iso_timestamp(ts: Any) -> Optional[str]:
    """Parse ISO timestamp to normalized format."""
    return parse_timestamp_iso(ts)


def _timestamp_to_order(ts: str) -> int:
    """Convert ISO timestamp to microsecond-precision order value."""
    dt = parse_timestamp(ts)
    return int(dt.timestamp() * 1_000_000) if dt else 0


# Regex patterns for IDE context tags
_IDE_OPENED_FILE_PATTERN = re.compile(
    r'<ide_opened_file>(.*?)</ide_opened_file>',
    re.DOTALL
)
_IDE_SELECTION_PATTERN = re.compile(
    r'<ide_selection>(.*?)</ide_selection>',
    re.DOTALL
)

# Regex patterns for command context tags (local command execution)
_COMMAND_TAGS_PATTERN = re.compile(
    r'<(command-name|command-message|command-args|local-command-stdout|local-command-caveat|local-command-stderr)>(.*?)</\1>',
    re.DOTALL
)
# Generic pattern for other system/context tags to strip from display
_SYSTEM_CONTEXT_PATTERN = re.compile(
    r'<(system-reminder|file_contents|context|selection|environment_details)>(.*?)</\1>',
    re.DOTALL
)


def _extract_ide_context(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract IDE context tags from message text.

    Claude Code embeds IDE context like opened files and selections as XML-like
    tags in user messages. This function extracts them as structured blocks
    and returns the cleaned text.

    Returns:
        Tuple of (cleaned_text, list of ide_context blocks)
    """
    ide_blocks: List[Dict[str, Any]] = []
    seq = 0

    # Extract <ide_opened_file> tags
    for match in _IDE_OPENED_FILE_PATTERN.finditer(text):
        content = match.group(1).strip()
        # Parse the content - typically "The user opened the file {path} in the IDE..."
        file_path = None
        if "opened the file " in content:
            # Extract path between "opened the file " and " in the IDE"
            path_match = re.search(r'opened the file ([^\s]+)', content)
            if path_match:
                file_path = path_match.group(1)

        ide_blocks.append({
            "type": "ide_context",
            "context_type": "opened_file",
            "file_path": file_path,
            "raw_content": content,
            "seq": seq,
        })
        seq += 1

    # Extract <ide_selection> tags
    for match in _IDE_SELECTION_PATTERN.finditer(text):
        content = match.group(1).strip()
        ide_blocks.append({
            "type": "ide_context",
            "context_type": "selection",
            "raw_content": content,
            "seq": seq,
        })
        seq += 1

    # Remove the tags from the text
    cleaned = _IDE_OPENED_FILE_PATTERN.sub('', text)
    cleaned = _IDE_SELECTION_PATTERN.sub('', cleaned)
    # Also strip command context tags and system context tags
    cleaned = _COMMAND_TAGS_PATTERN.sub('', cleaned)
    cleaned = _SYSTEM_CONTEXT_PATTERN.sub('', cleaned)
    cleaned = cleaned.strip()

    return cleaned, ide_blocks
