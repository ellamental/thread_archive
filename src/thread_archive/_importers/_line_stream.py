"""Shared incremental-import scaffold for the line-stream providers (codex, grok,
antigravity).

All three read one JSONL transcript and differ only in provider-specific steps
supplied as callbacks. The orchestration — the append-proving cursor (:mod:`._cursor`),
thread resolve/create, empty-thread cleanup, watermark persist — is identical and
lives here. Runs as one atomic transaction (events + watermark commit together)
when no session is passed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from .._store import get_session
from ._cursor import resolve_source_cursor
from ._read import parse_session_lines, read_source_bytes
from ._result import IncrementalImportResult
from ._state import (
    adopt_if_unwatermarked,
    create_thread,
    discard_new_thread,
    get_import_state,
    get_thread_by_source,
    upsert_import_state,
)

logger = logging.getLogger(__name__)


def _run(
    session,
    *,
    source: str,
    source_id: str,
    session_path: Path,
    all_lines: list[dict],
    source_bytes: bytes,
    prepare,
    has_importable_content,
    make_title,
    import_lines,
    make_source_metadata,
) -> IncrementalImportResult:
    import_state = get_import_state(session, source, source_id)
    cursor = resolve_source_cursor(import_state, source_bytes, source=source, source_id=source_id)
    current_file_size = len(source_bytes)

    if cursor.unchanged and import_state:
        # Byte-identical to the last import; stamp the digest if the watermark predates it.
        import_state.last_content_hash = cursor.content_hash
        return IncrementalImportResult(
            0, 0, import_state.thread_id or 0, False, import_state.last_message_uuid
        )

    total_lines = len(all_lines)
    start_line = cursor.start_line
    if start_line >= total_lines:
        # Changed bytes, no new parsed lines (a torn tail the writer hasn't finished).
        # Carry size + digest forward so the completed line reads as an append next
        # poll; the line cursor tracks what parsed, so that line lands then.
        if import_state:
            import_state.last_line_count = total_lines
            import_state.last_file_size = current_file_size
            import_state.last_content_hash = cursor.content_hash
        return IncrementalImportResult(
            0,
            0,
            (import_state.thread_id or 0) if import_state else 0,
            False,
            import_state.last_message_uuid if import_state else None,
        )

    new_lines = all_lines[start_line:]
    ctx = prepare(all_lines, session_path) if prepare is not None else None

    thread_id: Optional[int] = (
        import_state.thread_id if (import_state and import_state.thread_id) else None
    )
    if thread_id is None:
        existing = get_thread_by_source(session, source, source_id)
        thread_id = existing.id if existing else None

    if import_state is None and adopt_if_unwatermarked(
        session, source=source, source_id=source_id,
        thread_id=thread_id, total_lines=total_lines, file_size=current_file_size,
        content_hash=cursor.content_hash,
    ):
        return IncrementalImportResult(0, 0, thread_id or 0, False, None)

    is_new_thread = False
    if thread_id is None:
        if not has_importable_content(new_lines):
            # Nothing worth a thread — record the watermark so the watcher doesn't
            # re-scan, and stop.
            upsert_import_state(
                session,
                source=source,
                source_id=source_id,
                thread_id=None,
                last_line_count=total_lines,
                last_file_size=current_file_size,
                last_content_hash=cursor.content_hash,
                last_message_uuid=None,
            )
            return IncrementalImportResult(len(new_lines), 0, 0, False)
        thread_id = create_thread(
            session,
            source=source,
            source_id=source_id,
            title=make_title(all_lines, ctx),
            source_metadata=make_source_metadata(ctx) if make_source_metadata else None,
        )
        is_new_thread = True

    events_created, last_uuid = import_lines(session, thread_id, all_lines, new_lines, ctx)

    if is_new_thread and events_created == 0:
        # Row AND staged truth record — no ghost threads/<id>.jsonl on commit.
        discard_new_thread(session, thread_id)
        thread_id = 0
        is_new_thread = False

    upsert_import_state(
        session,
        source=source,
        source_id=source_id,
        thread_id=thread_id or None,
        last_line_count=total_lines,
        last_file_size=current_file_size,
        last_content_hash=cursor.content_hash,
        last_message_uuid=last_uuid,
    )

    return IncrementalImportResult(
        lines_processed=len(new_lines),
        events_created=events_created,
        thread_id=thread_id or 0,
        is_new_thread=is_new_thread,
        last_message_uuid=last_uuid,
    )


def import_line_stream_session(
    *,
    source: str,
    source_id: str,
    session_path,
    session=None,
    not_found_msg: str,
    has_importable_content: Callable[[list[dict]], bool],
    make_title: Callable[[list[dict], object], str],
    import_lines: Callable[..., tuple[int, Optional[str]]],
    prepare: Optional[Callable[[list[dict], Path], object]] = None,
    make_source_metadata: Optional[Callable[[object], Optional[dict]]] = None,
) -> IncrementalImportResult:
    """Import a single-JSONL line-stream provider transcript incrementally.

    Callbacks:
      - ``prepare(all_lines, path) -> ctx`` — read provider metadata once (or None).
      - ``has_importable_content(new_lines) -> bool``
      - ``make_title(all_lines, ctx) -> str``
      - ``make_source_metadata(ctx) -> dict | None`` — optional.
      - ``import_lines(session, thread_id, all_lines, new_lines, ctx) -> (events, last_uuid)``.
    """
    session_path = Path(session_path)
    if not session_path.exists():
        raise FileNotFoundError(not_found_msg)

    source_bytes = read_source_bytes(session_path)
    all_lines = parse_session_lines(source_bytes, session_path.name)

    kwargs = dict(
        source=source,
        source_id=source_id,
        session_path=session_path,
        all_lines=all_lines,
        source_bytes=source_bytes,
        prepare=prepare,
        has_importable_content=has_importable_content,
        make_title=make_title,
        import_lines=import_lines,
        make_source_metadata=make_source_metadata,
    )

    if session is not None:
        return _run(session, **kwargs)
    with get_session() as s:
        result = _run(s, **kwargs)
        s.commit()
        return result
