"""Claude Code incremental import.

Reduced to the conversation-archive essentials and run as a single atomic
transaction. The watermark logic lives in :mod:`._cursor`: a content digest proves the
file was only appended to, and the line cursor (``new_lines = all_lines[start_line:]``)
then feeds just the grown tail into the importer — a source rewritten under the cursor
rewinds and re-imports whole instead. Idempotence across re-reads (and across a
rewind) is guaranteed by the dedup_key membership check in :func:`import_lines`.

**Atomicity:** the whole import — thread create, events, watermark — commits in one
session, so "the watermark advances only after a successful import" holds by
construction.

Deferred (enhancements, not core): compaction continuation/fork detection, subagent
session handling, sidecar hook context, batch-thread adoption, description/models
backfill, and rich title extraction.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser

from .._store import Thread, get_session
from ._continuation import resolve_continuation_thread
from ._cursor import resolve_source_cursor
from ._events import import_lines
from ._read import parse_session_lines, read_source_bytes
from ._result import IncrementalImportResult
from ._sidecar import import_sidecar_lines, read_sidecar_lines
from ._state import (
    adopt_if_unwatermarked,
    create_thread,
    discard_new_thread,
    get_import_state,
    get_thread_by_source,
    lookup_parent_thread,
    set_thread_models_from_events,
    update_thread_description,
    update_thread_title,
    upsert_import_state,
)
from ._titles import extract_description, extract_session_title, extract_title

logger = logging.getLogger(__name__)

SOURCE = "claude-code"


def _is_subagent_source_id(source_id: str) -> bool:
    """True when this CC source_id names a Task-tool subagent transcript.

    The watcher hands subagent files (``<project>/<uuid>/subagents/agent-*.jsonl``)
    the same ``{project}:{stem}`` source_id as any other session; the stem keeps
    Claude Code's ``agent-`` prefix, which is the marker that survives into the
    import path. Subagent runs file as hidden ``thread_type='system'`` threads —
    captured and searchable, but kept out of the archive sidebar."""
    return source_id.rsplit(":", 1)[-1].startswith("agent-")


def _cc_origin_metadata(all_lines: list[dict], source_id: str, *, session=None) -> Optional[dict]:
    """Origin-project metadata for a CC thread: the real ``cwd`` (first line that
    carries one) + the munged ``project_dir`` from the source_id.

    For a subagent transcript also stamp ``is_subagent`` plus the spawning session
    (``parent_session_id`` from the lines' ``sessionId``) and the subagent's own
    ``agent_id`` (from ``agentId``), so the run can be joined back to its parent
    thread. When a ``session`` is supplied and the parent resolves to no thread, the
    gap is logged rather than lost silently."""
    meta: dict[str, Any] = {}
    for line in all_lines:
        cwd = line.get("cwd")
        if isinstance(cwd, str) and cwd:
            meta["cwd"] = cwd
            break
    if ":" in source_id:
        meta["project_dir"] = source_id.rsplit(":", 1)[0]
    if _is_subagent_source_id(source_id):
        meta["is_subagent"] = True
        for line in all_lines:
            if "agent_id" not in meta and line.get("agentId"):
                meta["agent_id"] = line["agentId"]
            if "parent_session_id" not in meta and line.get("sessionId"):
                meta["parent_session_id"] = line["sessionId"]
            if "agent_id" in meta and "parent_session_id" in meta:
                break
        parent_sid = meta.get("parent_session_id")
        project_dir = meta.get("project_dir")
        if session is not None and parent_sid and project_dir:
            parent_source_id = f"{project_dir}:{parent_sid}"
            if lookup_parent_thread(session, parent_source_id) is None:
                logger.warning(
                    "Subagent %s: parent_session_id %s resolves to no thread (%s) — "
                    "run can't be joined back to its parent",
                    source_id, parent_sid, parent_source_id,
                )
    return meta or None


def _import_cc(
    session,
    source_id: str,
    all_lines: list[dict],
    source_bytes: bytes,
    parser: ClaudeCodeParser,
    builder: DefaultEventBuilder,
    *,
    source: str = SOURCE,
    title_override: Optional[str] = None,
) -> IncrementalImportResult:
    import_state = get_import_state(session, source, source_id)
    cursor = resolve_source_cursor(import_state, source_bytes, source=source, source_id=source_id)
    current_file_size = len(source_bytes)

    if cursor.unchanged and import_state:
        # Byte-identical to the last import. Stamp the digest if this watermark predates
        # it, so the next poll's append proof has something to check against.
        import_state.last_content_hash = cursor.content_hash
        return IncrementalImportResult(
            lines_processed=0,
            events_created=0,
            thread_id=import_state.thread_id or 0,
            is_new_thread=False,
            last_message_uuid=import_state.last_message_uuid,
        )

    total_lines = len(all_lines)
    start_line = cursor.start_line
    if start_line >= total_lines:
        # The file changed but yielded no new parsed lines (a torn tail line the writer
        # hasn't finished; a rewrite down to nothing). Advance size + digest so the
        # completed line still reads as an append next poll — the line cursor tracks
        # what actually parsed, so the line itself imports then.
        if import_state:
            import_state.last_line_count = total_lines
            import_state.last_file_size = current_file_size
            import_state.last_content_hash = cursor.content_hash
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
        existing = get_thread_by_source(session, source, source_id)
        thread_id = existing.id if existing else None

    if import_state is None and adopt_if_unwatermarked(
        session, source=source, source_id=source_id,
        thread_id=thread_id, total_lines=total_lines, file_size=current_file_size,
        content_hash=cursor.content_hash,
    ):
        return IncrementalImportResult(0, 0, thread_id or 0, False, None)

    is_subagent = _is_subagent_source_id(source_id)

    # Continuation / fork merge: a fresh CC session that continues an existing thread
    # (created on context compaction) merges into it instead of fragmenting into its
    # own thread. Subagents are independent transcripts; cloth-shaped sources (source
    # != "claude-code") don't carry CC's compaction markers, so both skip it.
    merged_continuation = False
    if thread_id is None and source == SOURCE and not is_subagent:
        parent_id = resolve_continuation_thread(session, all_lines, source_id, parser, builder)
        if parent_id is not None:
            thread_id = parent_id
            merged_continuation = True

    is_new_thread = False
    if thread_id is None:
        title = title_override or extract_title(all_lines)
        if is_subagent:
            title = f"🤖 {title}"
        thread_id = create_thread(
            session,
            source=source,
            source_id=source_id,
            title=title,
            thread_type="system" if is_subagent else "conversation",
            description=extract_description(all_lines),
            source_metadata=_cc_origin_metadata(all_lines, source_id, session=session),
        )
        is_new_thread = True

    events_created, last_uuid = import_lines(
        session, thread_id, new_lines, parser, builder,
        source=source, source_id=source_id, cross_pass_dedup=merged_continuation,
    )

    # A freshly-created thread that imported nothing (no importable content) is
    # cleaned up so we don't leave an empty thread behind — row AND staged truth
    # record, so no ghost threads/<id>.jsonl survives the commit.
    if is_new_thread and events_created == 0:
        discard_new_thread(session, thread_id)
        thread_id = 0
        is_new_thread = False
    elif thread_id and events_created:
        # Keep metadata current. New threads already carry title/description from
        # create; an existing thread re-syncs a rename / AI title and backfills a
        # missing description. Models are re-derived from the (now-written) events
        # for both, so the sidebar model filter stays current.
        if not is_new_thread:
            refreshed = extract_session_title(all_lines)
            if refreshed:
                update_thread_title(session, thread_id, refreshed[:100])
            thread = session.get(Thread, thread_id)
            if thread is not None and not thread.description:
                desc = extract_description(all_lines)
                if desc:
                    update_thread_description(session, thread_id, desc)
        set_thread_models_from_events(session, thread_id)

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


def import_session_incremental(
    session_path,
    source_id: str,
    parser: Optional[ClaudeCodeParser] = None,
    builder: Optional[DefaultEventBuilder] = None,
    *,
    source: str = SOURCE,
    session=None,
) -> IncrementalImportResult:
    """Import a Claude Code JSONL transcript incrementally.

    Reads the file, then runs the import. When ``session`` is given the caller owns
    the transaction; otherwise the whole import commits atomically in a fresh
    session (so events + watermark land together, or not at all).

    ``source`` is the thread/import-state source label. It defaults to
    ``"claude-code"`` and is rarely overridden — the one case is a *host store that
    writes Claude-Code-shaped JSONL under a different identity* (e.g. cloth's CLI
    transcripts), which a deployment watcher imports under its own source so the
    threads carry the right provenance rather than masquerading as claude-code.
    """
    session_path = Path(session_path)
    if not session_path.exists():
        raise FileNotFoundError(f"Session file not found: {session_path}")

    parser = parser or ClaudeCodeParser()
    builder = builder or DefaultEventBuilder()
    source_bytes = read_source_bytes(session_path)
    all_lines = parse_session_lines(source_bytes, session_path.name)
    sidecar_lines = read_sidecar_lines(session_path)

    def _run(s) -> IncrementalImportResult:
        result = _import_cc(s, source_id, all_lines, source_bytes, parser, builder, source=source)
        # The hook-context sidecar has its own line-count cursor, independent of the
        # session file's size watermark — so a grown sidecar imports even when the
        # main file is unchanged (and _import_cc returned early).
        if result.thread_id and sidecar_lines:
            n = import_sidecar_lines(s, result.thread_id, source_id, sidecar_lines)
            if n:
                result = replace(result, events_created=result.events_created + n)
        return result

    if session is not None:
        return _run(session)

    with get_session() as s:
        result = _run(s)
        s.commit()
        return result
