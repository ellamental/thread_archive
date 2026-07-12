"""Claude Cowork (local agent-mode) session import.

Cowork writes a Claude-Code-shaped ``audit.jsonl`` per agent task under
``~/Library/Application Support/Claude/local-agent-mode-sessions/<user>/<org>/local_<id>/``,
with a sibling ``local_<id>.json`` carrying the human title. Two quirks vs a plain
CC session, handled by :func:`_normalize_cowork_line`:

- every line carries an ``_audit_timestamp``; ``timestamp`` (which the parser keys
  ``created_at`` + dedup off) is only on a subset — so backfill it; and
- each user submission appears twice (once at submit, once as an ``isReplay`` echo
  with its own timestamp that defeats the parser's dedup) — so replays are rewritten
  to a type the parser ignores, keeping the line-count watermark aligned with the file.

The normalized lines then run through the standard CC import path under
``source="cowork"`` (so continuation detection, which is CC-only, is skipped), with
the title taken from the metadata file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Optional

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser

from .._store import get_session
from ._read import parse_session_lines_counted, read_source_bytes
from ._result import IncrementalImportResult
from .claude_code import _import_cc

logger = logging.getLogger(__name__)

SOURCE = "cowork"


def _normalize_cowork_line(line: dict) -> dict:
    """Backfill ``timestamp`` from ``_audit_timestamp``; neuter ``isReplay`` echoes.

    A replay is rewritten to ``cowork_replay`` (a type the CC parser ignores) rather
    than dropped, so the line-count import watermark stays aligned with the raw file."""
    if line.get("isReplay"):
        return {"type": "cowork_replay", "_audit_timestamp": line.get("_audit_timestamp")}
    ts = line.get("timestamp")
    audit_ts = line.get("_audit_timestamp")
    if not ts and audit_ts:
        return {**line, "timestamp": audit_ts}
    return line


def _read_cowork_title(metadata_path: Optional[Path]) -> Optional[str]:
    """The human title from a sibling ``local_{id}.json`` Cowork metadata file."""
    if metadata_path is None or not metadata_path.exists():
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Cowork metadata read error %s: %s", metadata_path.name, e)
        return None
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:200]
    return None


def import_cowork_session_incremental(
    audit_path,
    source_id: str,
    metadata_path=None,
    parser: Optional[ClaudeCodeParser] = None,
    builder: Optional[DefaultEventBuilder] = None,
    *,
    session=None,
) -> IncrementalImportResult:
    """Import a Cowork ``audit.jsonl`` incrementally (normalize → CC import path)."""
    audit_path = Path(audit_path)
    if not audit_path.exists():
        raise FileNotFoundError(f"Cowork audit file not found: {audit_path}")

    parser = parser or ClaudeCodeParser()
    builder = builder or DefaultEventBuilder()
    # The cursor proves its append against the raw file bytes; the lines it cursors are
    # the normalized ones (a 1:1 map over the parsed lines, so the counts stay aligned).
    source_bytes = read_source_bytes(audit_path)
    # A bare-scalar JSONL line parses to a non-dict; skip it so `.get()` can't crash.
    parsed, parse_errors = parse_session_lines_counted(source_bytes, audit_path.name)
    lines = [_normalize_cowork_line(ln) for ln in parsed if isinstance(ln, dict)]
    title = _read_cowork_title(Path(metadata_path)) if metadata_path is not None else None

    if session is not None:
        result = _import_cc(
            session, source_id, lines, source_bytes, parser, builder,
            source=SOURCE, title_override=title,
        )
        return replace(result, parse_errors=parse_errors) if parse_errors else result
    with get_session() as s:
        result = _import_cc(
            s, source_id, lines, source_bytes, parser, builder,
            source=SOURCE, title_override=title,
        )
        s.commit()
        return replace(result, parse_errors=parse_errors) if parse_errors else result
