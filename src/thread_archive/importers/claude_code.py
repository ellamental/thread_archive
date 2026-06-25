"""Claude Code incremental import.

Reduced to the conversation-archive essentials and run as a single atomic
transaction. The watermark logic: a file-size early-out skips an unchanged file
cheaply, and a line-count cursor (``new_lines = all_lines[start_line:]``) feeds only
the grown tail into the importer. Idempotence across re-reads is guaranteed by the
dedup_key membership check in :func:`import_lines`.

**Atomicity:** the whole import — thread create, events, watermark — commits in one
session, so "the watermark advances only after a successful import" holds by
construction.

Deferred (enhancements, not core): compaction continuation/fork detection, subagent
``thread_type='system'``, sidecar hook context, batch-thread adoption,
description/models backfill, and rich title extraction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete
from thread_import import DefaultEventBuilder
from thread_import.parsers.claude_code import ClaudeCodeParser

from ..store import Thread, get_session
from ._events import import_lines
from ._read import read_session_lines
from ._result import IncrementalImportResult
from ._state import create_thread, get_import_state, get_thread_by_source, upsert_import_state

logger = logging.getLogger(__name__)

SOURCE = "claude-code"


def _extract_title(all_lines: list[dict]) -> Optional[str]:
    """A basic title from the first user message's text (truncated). A richer
    extractor (user rename / ai-title) is deferred."""
    for line in all_lines[:20]:
        if line.get("type") != "user":
            continue
        content = line.get("message", {}).get("content")
        text: Optional[str] = content if isinstance(content, str) else None
        if text is None and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    text = block["text"]
                    break
        if text and text.strip():
            return text.strip()[:100]
    return None


def _cc_origin_metadata(all_lines: list[dict], source_id: str) -> Optional[dict]:
    """Origin-project metadata: the real ``cwd`` (first line that carries one) +
    the munged ``project_dir`` from the source_id (subagent lineage stamping
    deferred)."""
    meta: dict[str, Any] = {}
    for line in all_lines:
        cwd = line.get("cwd")
        if isinstance(cwd, str) and cwd:
            meta["cwd"] = cwd
            break
    if ":" in source_id:
        meta["project_dir"] = source_id.rsplit(":", 1)[0]
    return meta or None


def _import_cc(
    session,
    source_id: str,
    all_lines: list[dict],
    current_file_size: int,
    parser: ClaudeCodeParser,
    builder: DefaultEventBuilder,
) -> IncrementalImportResult:
    import_state = get_import_state(session, SOURCE, source_id)

    # File-size watermark: nothing appended → nothing to do.
    if import_state and import_state.last_file_size == current_file_size:
        return IncrementalImportResult(
            lines_processed=0,
            events_created=0,
            thread_id=import_state.thread_id or 0,
            is_new_thread=False,
            last_message_uuid=import_state.last_message_uuid,
        )

    total_lines = len(all_lines)
    start_line = import_state.last_line_count if import_state else 0
    if start_line >= total_lines:
        return IncrementalImportResult(
            lines_processed=0,
            events_created=0,
            thread_id=(import_state.thread_id or 0) if import_state else 0,
            is_new_thread=False,
            last_message_uuid=import_state.last_message_uuid if import_state else None,
        )

    new_lines = all_lines[start_line:]

    # Resolve the thread: the watermark's thread_id wins, else lookup by source.
    thread_id: Optional[int] = (
        import_state.thread_id if (import_state and import_state.thread_id) else None
    )
    if thread_id is None:
        existing = get_thread_by_source(session, SOURCE, source_id)
        thread_id = existing.id if existing else None

    is_new_thread = False
    if thread_id is None:
        thread_id = create_thread(
            session,
            source=SOURCE,
            source_id=source_id,
            title=_extract_title(all_lines),
            source_metadata=_cc_origin_metadata(all_lines, source_id),
        )
        is_new_thread = True

    events_created, last_uuid = import_lines(
        session, thread_id, new_lines, parser, builder, source=SOURCE, source_id=source_id
    )

    # A freshly-created thread that imported nothing (no importable content) is
    # cleaned up so we don't leave an empty thread behind.
    if is_new_thread and events_created == 0:
        session.execute(delete(Thread).where(Thread.id == thread_id))
        thread_id = 0
        is_new_thread = False

    upsert_import_state(
        session,
        source=SOURCE,
        source_id=source_id,
        thread_id=thread_id or None,
        last_line_count=total_lines,
        last_file_size=current_file_size,
        last_message_uuid=last_uuid,
    )

    return IncrementalImportResult(
        lines_processed=len(new_lines),
        events_created=events_created,
        thread_id=thread_id or 0,
        is_new_thread=is_new_thread,
        last_message_uuid=last_uuid,
    )


def import_session_incremental(
    session_path,
    source_id: str,
    parser: Optional[ClaudeCodeParser] = None,
    builder: Optional[DefaultEventBuilder] = None,
    *,
    session=None,
) -> IncrementalImportResult:
    """Import a Claude Code JSONL transcript incrementally.

    Reads the file, then runs the import. When ``session`` is given the caller owns
    the transaction; otherwise the whole import commits atomically in a fresh
    session (so events + watermark land together, or not at all).
    """
    session_path = Path(session_path)
    if not session_path.exists():
        raise FileNotFoundError(f"Session file not found: {session_path}")

    parser = parser or ClaudeCodeParser()
    builder = builder or DefaultEventBuilder()
    current_file_size = session_path.stat().st_size
    all_lines = read_session_lines(session_path)

    if session is not None:
        return _import_cc(session, source_id, all_lines, current_file_size, parser, builder)

    with get_session() as s:
        result = _import_cc(s, source_id, all_lines, current_file_size, parser, builder)
        s.commit()
        return result
