"""Read + sanitize a provider JSONL transcript.

``errors="replace"`` so a half-written multibyte tail (a poll catching a file
mid-write) decodes to U+FFFD instead of raising; the per-line ``JSONDecodeError``
guard means one bad line is skipped, never the whole file. Each parsed line is
scrubbed of null bytes + lone UTF-16 surrogates at the ingest boundary — source
JSONL is written by external (JS/TS) processes that can slice strings on UTF-16
boundaries and leave an orphaned surrogate, which then fails ``json.dumps`` on the
write path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _scrub_str(s: str) -> str:
    if "\x00" in s:
        s = s.replace("\x00", "")
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogate(s): replace them rather than fail the whole import.
        s = s.encode("utf-8", "replace").decode("utf-8")
    return s


def sanitize_payload(obj: Any) -> Any:
    """Recursively scrub strings in a parsed JSON value (null bytes / lone surrogates)."""
    if isinstance(obj, str):
        return _scrub_str(obj)
    if isinstance(obj, dict):
        return {k: sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_payload(v) for v in obj]
    return obj


def read_session_lines(session_path: Path) -> list[dict]:
    """Read + parse every line of a JSONL transcript; skip (log) bad lines."""
    session_path = Path(session_path)
    lines: list[dict] = []
    parse_errors = 0
    with open(session_path, "r", encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            if line.strip():
                try:
                    lines.append(sanitize_payload(json.loads(line)))
                except json.JSONDecodeError as e:
                    parse_errors += 1
                    logger.warning("%s:%d — JSON parse error: %s", session_path.name, line_num, e)
    if parse_errors:
        logger.warning("%s: %d lines skipped due to parse errors", session_path.name, parse_errors)
    return lines
