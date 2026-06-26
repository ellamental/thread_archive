"""Shared incremental-import scaffold for the line-stream providers (codex, grok,
antigravity).

All three read one JSONL transcript and differ only in provider-specific steps
supplied as callbacks. The orchestration — file-size watermark, line-count cursor,
thread resolve/create, empty-thread cleanup, watermark persist — is identical and
lives here. Runs as one atomic transaction (events + watermark commit together)
when no session is passed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import delete

from ..store import Thread, get_session
from ._read import read_session_lines
from ._result import IncrementalImportResult
from ._state import (
    adopt_if_unwatermarked,
    create_thread,
    get_import_state,
    get_thread_by_source,
    upsert_import_state,
)


def _run(
    session,
    *,
    source: str,
    source_id: str,
    session_path: Path,
    all_lines: list[dict],
    current_file_size: int,
    prepare,
    has_importable_content,
    make_title,
    import_lines,
    make_source_metadata,
) -> IncrementalImportResult:
    import_state = get_import_state(session, source, source_id)

    if import_state and import_state.last_file_size == current_file_size:
        return IncrementalImportResult(
            0, 0, import_state.thread_id or 0, False, import_state.last_message_uuid
        )

    total_lines = len(all_lines)
    start_line = import_state.last_line_count if import_state else 0
    if start_line >= total_lines:
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
        session.execute(delete(Thread).where(Thread.id == thread_id))
        thread_id = 0
        is_new_thread = False

    upsert_import_state(
        session,
        source=source,
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

    current_file_size = session_path.stat().st_size
    all_lines = read_session_lines(session_path)

    kwargs = dict(
        source=source,
        source_id=source_id,
        session_path=session_path,
        all_lines=all_lines,
        current_file_size=current_file_size,
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
