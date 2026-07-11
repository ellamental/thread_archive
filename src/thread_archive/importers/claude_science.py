"""Claude Science (the Anthropic *AI Workbench* desktop app) session import.

Claude Science keeps each org's work in a single live SQLite DB at
``~/.claude-science/orgs/<org>/operon-cli.db``. A conversation is a **frame** (the
``frames`` table): a root frame (``parent_frame_id IS NULL``) is a top-level chat,
and a child frame is a subagent run (e.g. a ``REVIEWER`` spawned by the main
``OPERON`` agent). A frame's transcript is its ``frame_messages`` rows — one
Anthropic-shaped message per ``(frame_id, idx)`` in ``msg_json``
(``{"role", "content": [blocks]}`` where a block is text / thinking / tool_use /
tool_result / image).

We scan the DB the way the Cursor / OpenCode importers do (open read-only, walk the
conversations, incremental-skip via the import_state cursor), but a frame's messages
are *already* Claude-shaped — so rather than re-deriving the NormalizedMessage blocks
by hand, we synthesize Claude-Code-style JSONL lines from them and run them through
the standard CC import path under ``source="claude-science"`` (exactly as cowork does
for its ``audit.jsonl``). Continuation detection is CC-only, so it's skipped.

Two store quirks shape the mapping:

- **No per-message timestamp.** ``frame_messages`` carries none — only the frame has
  ``created_at`` / ``completed_at``. So we stamp each message a deterministic
  ``created_at + idx·STEP`` (see :func:`_synthesize_line`): monotonic by ``idx`` and
  stable across incremental passes (``idx`` and ``created_at`` don't move), which is
  what the event assembler needs. dedup is timestamp-free (content-keyed), so this
  affects only ordering, never idempotence.
- **Subagent frames.** A child frame (it has a ``parent_frame_id``) imports as a
  hidden ``thread_type="system"`` thread with a 🤖-prefixed title, mirroring how CC
  Task-tool subagents are filed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from thread_import import DefaultEventBuilder
from thread_import.parsers.claude_code import ClaudeCodeParser

from ..store import get_session
from ._events import import_lines
from ._state import (
    adopt_if_unwatermarked,
    create_thread,
    discard_new_thread,
    get_import_state,
    get_thread_by_source,
    set_thread_models_from_events,
    upsert_import_state,
)
from ._titles import extract_title

logger = logging.getLogger(__name__)

SOURCE = "claude-science"

# Synthetic per-message spacing (ms). The store records no per-message time, so we
# space messages a second apart from the frame's start — monotonic and stable per
# ``idx``, which is all the assembler's ordering needs.
_STEP_MS = 1000

# Frames that aren't conversations: the per-project "User Uploads" container.
_NON_CONVERSATION_TYPES = ("uploads",)

# Anthropic ships a demo project ("Example project", id ``proj_example``) of pre-built
# analyses — scRNA-seq, CRISPR, phylogenetics — seeded into every install. Those are
# *Anthropic's* canned transcripts, not the user's own work, so they're kept out of
# the archive (importing them would inject fabricated "science you did" threads). The
# user's real frames live under their own ``proj_<hash>`` ids and import normally.
_SEEDED_PROJECT_IDS = ("proj_example",)


@dataclass
class ClaudeScienceImportResult:
    events_created: int
    thread_id: int
    is_new_thread: bool


@dataclass
class ClaudeScienceDbScanResult:
    frames_processed: int
    frames_imported: int
    events_created: int
    frames_failed: int = 0
    errors: list[str] = field(default_factory=list)


def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _synthesize_line(frame_id: str, idx: int, msg: dict, model: Optional[str], base_ms: int) -> Optional[dict]:
    """Turn one ``frame_messages`` row into a Claude-Code-shaped JSONL line.

    The message body (``role`` + ``content`` blocks) is already Anthropic-shaped, so
    it drops straight into the CC line's ``message`` field; we only add the envelope
    (``type`` / ``uuid`` / synthetic ``timestamp`` / ``sessionId``) the parser keys
    off. A real ``_uuid`` is used when present so re-imports dedup on a stable id;
    otherwise ``{frame_id}:{idx}`` is a stable synthetic stand-in."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content")
    if content is None:
        return None
    message: dict[str, Any] = {"role": role, "content": content}
    if role == "assistant" and model:
        message["model"] = model
    return {
        "type": role,
        "uuid": msg.get("_uuid") or f"{frame_id}:{idx}",
        "timestamp": _iso_from_ms(base_ms + idx * _STEP_MS),
        "sessionId": frame_id,
        "message": message,
    }


def _frame_title(frame: dict, lines: list[dict]) -> str:
    """A thread title for a frame: its ``name`` (what the app shows), else its
    ``task_summary``, else the first-user-message fallback. A child (subagent) frame
    gets a 🤖 prefix. Onboarding/system-prefixed first lines fall back to an
    agent-named label rather than leaking the injected ``[System]`` block."""
    title = (frame.get("name") or "").strip() or (frame.get("task_summary") or "").strip()
    if not title:
        derived = extract_title(lines)
        if derived and derived != "Claude Code Session" and not derived.startswith("[System]"):
            title = derived
        else:
            agent = (frame.get("agent_name") or "").strip().replace("_", " ").title()
            title = f"{agent} Session" if agent else "Claude Science Session"
    if frame.get("parent_frame_id"):
        title = f"🤖 {title}"
    return title[:100]


def _frame_metadata(org_uuid: str, frame: dict) -> dict:
    """Origin metadata for a Claude Science thread — the org, the frame's identity in
    the app, and (for a subagent) the parent/root frame it was spawned under."""
    meta: dict[str, Any] = {
        "app": "claude-science",
        "org_uuid": org_uuid,
        "frame_id": frame.get("id"),
        "agent_name": frame.get("agent_name"),
        "conversation_type": frame.get("conversation_type"),
        "project_id": frame.get("project_id"),
        "model": frame.get("model"),
    }
    if frame.get("parent_frame_id"):
        meta["is_subagent"] = True
        meta["parent_frame_id"] = frame.get("parent_frame_id")
        meta["root_frame_id"] = frame.get("root_frame_id")
    return {k: v for k, v in meta.items() if v is not None}


def _run_frame(
    session,
    org_uuid: str,
    frame: dict,
    message_rows: list,
    parser: ClaudeCodeParser,
    builder: DefaultEventBuilder,
) -> ClaudeScienceImportResult:
    frame_id = frame["id"]
    source_id = f"{org_uuid}:{frame_id}"
    import_state = get_import_state(session, SOURCE, source_id)

    total = len(message_rows)
    start = import_state.last_line_count if import_state else 0
    if import_state and start >= total:
        return ClaudeScienceImportResult(0, import_state.thread_id or 0, False)

    base_ms = int(frame.get("created_at") or 0)
    model = frame.get("model")
    lines: list[dict] = []
    for row in message_rows:
        idx, raw = row[0], row[1]
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict):
            continue
        line = _synthesize_line(frame_id, idx, msg, model, base_ms)
        if line is not None:
            lines.append(line)

    # Resolve the thread (watermark wins, else lookup by source) before any create.
    thread_id: Optional[int] = (
        import_state.thread_id if (import_state and import_state.thread_id) else None
    )
    if thread_id is None:
        existing = get_thread_by_source(session, SOURCE, source_id)
        thread_id = existing.id if existing else None

    # Reindex safety: a thread with events but no watermark (cursor lost to a
    # `rm index.db && reindex`) is adopted at EOF rather than re-imported from 0.
    if import_state is None and adopt_if_unwatermarked(
        session, source=SOURCE, source_id=source_id,
        thread_id=thread_id, total_lines=total, file_size=0,
    ):
        return ClaudeScienceImportResult(0, thread_id or 0, False)

    is_new_thread = False
    if thread_id is None:
        thread_id = create_thread(
            session,
            source=SOURCE,
            source_id=source_id,
            title=_frame_title(frame, lines),
            thread_type="system" if frame.get("parent_frame_id") else "conversation",
            source_metadata=_frame_metadata(org_uuid, frame),
        )
        is_new_thread = True

    new_lines = lines[start:]
    events_created, last_uuid = import_lines(
        session, thread_id, new_lines, parser, builder, source=SOURCE, source_id=source_id,
    )

    # A freshly-created thread that imported nothing (no importable content) is
    # cleaned up so we don't leave an empty thread behind — row AND staged truth
    # record, so no ghost threads/<id>.jsonl survives the commit.
    if is_new_thread and events_created == 0:
        discard_new_thread(session, thread_id)
        thread_id = 0
        is_new_thread = False
    elif thread_id and events_created:
        set_thread_models_from_events(session, thread_id)

    upsert_import_state(
        session,
        source=SOURCE,
        source_id=source_id,
        thread_id=thread_id or None,
        last_line_count=total,
        last_file_size=0,
        last_message_uuid=last_uuid,
    )
    return ClaudeScienceImportResult(events_created, thread_id or 0, is_new_thread)


def import_claude_science_frame(
    org_uuid: str,
    frame: dict,
    message_rows: list,
    parser: Optional[ClaudeCodeParser] = None,
    builder: Optional[DefaultEventBuilder] = None,
    *,
    session=None,
) -> ClaudeScienceImportResult:
    """Import one frame's transcript (atomic per-frame transaction when no session)."""
    parser = parser or ClaudeCodeParser()
    builder = builder or DefaultEventBuilder()
    if session is not None:
        return _run_frame(session, org_uuid, frame, message_rows, parser, builder)
    with get_session() as s:
        result = _run_frame(s, org_uuid, frame, message_rows, parser, builder)
        s.commit()
        return result


# The frame columns the importer reads (title/metadata/timestamps + identity).
_FRAME_COLUMNS = (
    "id", "parent_frame_id", "root_frame_id", "agent_name", "conversation_type",
    "name", "task_summary", "model", "project_id", "created_at",
)


def import_claude_science_db(db_path, org_uuid: str) -> ClaudeScienceDbScanResult:
    """Open a Claude Science ``operon-cli.db`` (read-only) and import every frame in
    it under ``org_uuid``. The DB is live, so we open it ``mode=ro`` with a busy
    timeout and never write to it."""
    db_path = Path(db_path)
    summary = ClaudeScienceDbScanResult(0, 0, 0)
    parser = ClaudeCodeParser()
    builder = DefaultEventBuilder()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='frames'"
        ).fetchone():
            return summary

        type_ph = ", ".join("?" for _ in _NON_CONVERSATION_TYPES)
        proj_ph = ", ".join("?" for _ in _SEEDED_PROJECT_IDS)
        frames = conn.execute(
            f"SELECT {', '.join(_FRAME_COLUMNS)} FROM frames "
            f"WHERE conversation_type NOT IN ({type_ph}) "
            f"  AND (project_id IS NULL OR project_id NOT IN ({proj_ph})) "
            f"ORDER BY created_at",
            (*_NON_CONVERSATION_TYPES, *_SEEDED_PROJECT_IDS),
        ).fetchall()

        for frame_row in frames:
            frame = dict(zip(_FRAME_COLUMNS, frame_row))
            frame_id = frame["id"]
            message_rows = conn.execute(
                "SELECT idx, msg_json FROM frame_messages WHERE frame_id = ? ORDER BY idx",
                (frame_id,),
            ).fetchall()
            if not message_rows:
                continue
            summary.frames_processed += 1
            try:
                result = import_claude_science_frame(
                    org_uuid, frame, message_rows, parser, builder
                )
                if result.events_created > 0:
                    summary.frames_imported += 1
                    summary.events_created += result.events_created
            except Exception as e:  # noqa: BLE001 — one bad frame must not stop the scan
                # Counted out to the watcher, not just logged — see the same guard in
                # opencode: a silently-skipped frame reads as "nothing new" and leaves
                # `archive status` green while a conversation is missing.
                logger.exception(
                    "import_claude_science_db: frame %s failed; skipping", frame_id[:8]
                )
                summary.frames_failed += 1
                summary.errors.append(f"frame {frame_id[:8]}: {e}")
    finally:
        conn.close()
    return summary
