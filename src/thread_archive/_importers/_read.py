"""Read + sanitize a provider JSONL transcript.

``errors="replace"`` so a half-written multibyte tail (a poll catching a file
mid-write) decodes to U+FFFD instead of raising; the per-line ``JSONDecodeError``
guard means one bad line is skipped, never the whole file. Each parsed line is
scrubbed of null bytes + lone UTF-16 surrogates at the ingest boundary — source
JSONL is written by external (JS/TS) processes that can slice strings on UTF-16
boundaries and leave an orphaned surrogate, which then fails ``json.dumps`` on the
write path.

The importers read the file's **bytes** once (:func:`read_source_bytes`) and parse
from that buffer (:func:`parse_session_lines`), because the same buffer is what
:mod:`._cursor` digests to prove the source was appended to rather than rewritten.
Reading twice would race a live writer: the bytes the cursor verified must be the
bytes the lines were parsed from.
"""

from __future__ import annotations

import io
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


def read_source_bytes(session_path: Path) -> bytes:
    """The transcript's raw bytes — the one read the importers do per poll."""
    return Path(session_path).read_bytes()


def parse_session_lines(data: bytes, name: str = "<transcript>") -> list[dict]:
    """Parse every line of a JSONL transcript's bytes; skip (log) bad lines.

    ``io.StringIO(..., newline=None)`` gives the same universal-newline line split
    as ``open()`` in text mode: only ``\\n`` (post-translation) ends a line, so a
    raw U+2028 inside a JSON string doesn't tear a valid line in half the way
    ``str.splitlines()`` would.
    """
    lines: list[dict] = []
    parse_errors = 0
    text = data.decode("utf-8", errors="replace")
    for line_num, line in enumerate(io.StringIO(text, newline=None), 1):
        if line.strip():
            try:
                lines.append(sanitize_payload(json.loads(line)))
            except json.JSONDecodeError as e:
                parse_errors += 1
                logger.warning("%s:%d — JSON parse error: %s", name, line_num, e)
    if parse_errors:
        logger.warning("%s: %d lines skipped due to parse errors", name, parse_errors)
    return lines


def read_session_lines(session_path: Path) -> list[dict]:
    """Read + parse every line of a JSONL transcript; skip (log) bad lines."""
    session_path = Path(session_path)
    return parse_session_lines(read_source_bytes(session_path), session_path.name)
