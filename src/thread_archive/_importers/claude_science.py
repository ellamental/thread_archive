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

Three store quirks shape the mapping:

- **Per-message timestamps are optional.** A message carries ``_ts`` (epoch ms,
  monotonic by ``idx``) only if the app wrote it; a frame from a version that
  didn't has none, and only the frame's own ``created_at`` / ``completed_at``
  place it. So :func:`_synthesize_line` takes ``_ts`` when it is there and
  otherwise stamps a deterministic ``created_at + idx·STEP`` — monotonic by
  ``idx`` and stable across incremental passes (``idx`` and ``created_at`` don't
  move), which is what the event assembler needs. The ladder is a placement of
  last resort: it compresses an hour of work into one second per message, so a
  real ``_ts`` always wins. dedup is timestamp-free (content-keyed), so which
  one a message gets affects only ordering, never idempotence.
- **The message record grows keys.** ``msg_json`` is not a source *line*, so the
  parser's field-level drift ledger cannot see it: a key the app adds would be
  dropped silently, which is how ``_ts`` went a month unread. Every key the
  importer accounts for is named in :data:`_KNOWN_STORE_KEYS`; anything else is
  preserved verbatim under the message's ``annotations["unmodeled"]`` and
  recorded to the drift ledger, the same bargain the line-based sources get.
- **Subagent frames.** A child frame (it has a ``parent_frame_id``) imports as a
  hidden ``thread_type="system"`` thread with a 🤖-prefixed title, mirroring how CC
  Task-tool subagents are filed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser
from thread_archive._thread_import.timestamps import parse_timestamp_iso

from .._store import get_session
from . import _probe
from ._events import assemble_events, log_parse_validation, preserve_unmodeled_fields
from ._result import DbScanResult, DbUnitImportResult
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
from ._validation_ledger import record_drift

logger = logging.getLogger(__name__)

SOURCE = "claude-science"

# Synthetic per-message spacing (ms), for a message the store gave no ``_ts``:
# a second apart from the frame's start — monotonic and stable per ``idx``, which
# is all the assembler's ordering needs.
_STEP_MS = 1000

# Store `_tokens` keys → Anthropic usage-field names. Mapped into the synthesized
# line's `message.usage` so the CC parser lifts them into provider_data["usage"] and
# the builder emits them as structured token fields on api_request_completed.
_TOKEN_KEY_MAP = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_tokens"),
    ("cache_write", "cache_write_tokens"),
    ("uncached", "uncached_tokens"),
)

# Message-level store extras → annotation keys. These are science-specific (the CC
# parser doesn't know them), so they ride outside the synthesized line and are merged
# into provider_data["annotations"] on the parsed NormalizedMessage — the sanctioned
# extras channel (see the annotations convention in event_builder), which never
# perturbs dedup identity.
_ANNOTATION_KEY_MAP = (
    ("_artifact_refs", "artifact_refs"),
    ("_async_exec", "async_exec"),  # per-tool_use background run: exec id + interrupted
    ("_cell_images", "cell_images"),
    ("_intent_id", "intent_id"),
    ("_refusal", "refusal"),  # the model declined this turn
    ("_rolling_summary", "rolling_summary"),
    ("_harness_notice", "harness_notice"),
    ("_harness_prompt", "harness_prompt"),
)

# Store keys read for the synthesized line itself rather than carried as extras:
# the message body, its id, its timestamp, and the assistant fields the CC parser
# lifts out of ``message`` (see :func:`_synthesize_line`).
_MAPPED_STORE_KEYS = ("role", "content", "_uuid", "_ts", "_tokens", "_response_id")

# Conscious drops: a key whose content the archive already records another way,
# so carrying it would give the same fact a second place to drift.
_DROPPED_STORE_KEYS = (
    # True exactly when the turn used an Anthropic server-side tool — which the
    # turn's own server_tool_use / web_search_tool_result blocks already say.
    "_has_server_tools",
)

# Every ``msg_json`` key the importer accounts for. A key outside this set is the
# drift signal for this store (see the module docstring): preserved under
# ``annotations["unmodeled"]``, and reported.
_KNOWN_STORE_KEYS = frozenset(
    _MAPPED_STORE_KEYS
    + _DROPPED_STORE_KEYS
    + tuple(src for src, _ in _ANNOTATION_KEY_MAP)
)

# Frames that aren't conversations: the per-project "User Uploads" container.
_NON_CONVERSATION_TYPES = ("uploads",)

# Anthropic ships a demo project ("Example project", id ``proj_example``) of pre-built
# analyses — scRNA-seq, CRISPR, phylogenetics — seeded into every install. Those are
# *Anthropic's* canned transcripts, not the user's own work, so they're kept out of
# the archive (importing them would inject fabricated "science you did" threads). The
# user's real frames live under their own ``proj_<hash>`` ids and import normally.
_SEEDED_PROJECT_IDS = ("proj_example",)


def _synthesize_line(frame_id: str, idx: int, msg: dict, model: Optional[str], base_ms: int) -> Optional[dict]:
    """Turn one ``frame_messages`` row into a Claude-Code-shaped JSONL line.

    The message body (``role`` + ``content`` blocks) is already Anthropic-shaped, so
    it drops straight into the CC line's ``message`` field; we only add the envelope
    (``type`` / ``uuid`` / ``timestamp`` / ``sessionId``) the parser keys off. A real
    ``_uuid`` is used when present so re-imports dedup on a stable id; otherwise
    ``{frame_id}:{idx}`` is a stable synthetic stand-in. The timestamp is the
    message's own ``_ts`` where the store has one, and the synthetic
    ``base_ms + idx·STEP`` ladder where it doesn't.

    Store extras ride along in the shape the CC parser already reads: per-message
    ``_tokens`` become ``message.usage`` (Anthropic field names) and ``_response_id``
    becomes ``message.id``, so they surface as structured tokens / ``message_id`` on
    the assistant turn's api_request_completed."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content")
    if content is None:
        return None
    message: dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        if model:
            message["model"] = model
        tokens = msg.get("_tokens")
        if isinstance(tokens, dict):
            usage = {
                new: tokens[old]
                for old, new in _TOKEN_KEY_MAP
                if tokens.get(old) is not None
            }
            if usage:
                message["usage"] = usage
        if msg.get("_response_id"):
            message["id"] = msg["_response_id"]
    ts = msg.get("_ts")
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        ts = base_ms + idx * _STEP_MS
    return {
        "type": role,
        "uuid": msg.get("_uuid") or f"{frame_id}:{idx}",
        # epoch-ms; parse_timestamp treats sub-1e12 floats as seconds.
        "timestamp": parse_timestamp_iso(ts / 1000),
        "sessionId": frame_id,
        "message": message,
    }


def _unmodeled_store_keys(msg: dict) -> set[str]:
    """The message's keys the importer doesn't account for — this store's
    field-level drift signal."""
    return {k for k in msg if k not in _KNOWN_STORE_KEYS}


def _message_annotations(msg: dict) -> Optional[dict]:
    """Science-specific extras of one store message, as an annotations dict
    (present-only), plus any unmodeled key's value under ``unmodeled``. None when
    the message carries none."""
    ann: dict[str, Any] = {
        new: msg[old] for old, new in _ANNOTATION_KEY_MAP if msg.get(old) is not None
    }
    unmodeled = {k: msg[k] for k in _unmodeled_store_keys(msg)}
    if unmodeled:
        ann["unmodeled"] = unmodeled
    return ann or None


def _import_science_lines(
    session,
    thread_id: str,
    lines: list[dict],
    annotations_by_uuid: dict[str, dict],
    parser: ClaudeCodeParser,
    builder: DefaultEventBuilder,
    *,
    source_id: str,
    unmodeled_keys: Optional[set[str]] = None,
) -> tuple[int, Optional[str]]:
    """Parse synthesized CC lines and assemble, merging science-specific message
    extras into ``provider_data["annotations"]`` on the parsed NormalizedMessages
    (matched by provider_message_id) before the builder runs — the CC parser can't
    carry keys it doesn't know, so the merge happens on its output.

    The envelope's ``provider`` is the *parse* identity (it selects how the CC
    parser reads the bundle) and stays ``"claude-code"``; validation runs under
    this importer's own ``SOURCE``, so a block type or field the app grows lands
    in Claude Science's ledger against its own ``ProviderConfig`` instead of
    being blamed on Claude Code.

    ``unmodeled_keys`` are the store keys this slice carried that the importer
    doesn't account for. The validators never see them — they live on the store
    record, not on the synthesized line — so they are reported here, to the same
    ledger, beside the findings that do come from a parse."""
    session_data = {
        "provider": "claude-code",
        "sessions": [{"session_id": "incremental", "project": "incremental", "lines": lines}],
    }
    messages = parser.parse_export(session_data)
    preserve_unmodeled_fields(messages, provider=SOURCE)
    log_parse_validation(
        messages,
        provider=SOURCE,
        conversation_id=source_id or "incremental",
        batch_safe=True,
    )
    if unmodeled_keys:
        record_drift(
            SOURCE,
            source_id or "incremental",
            findings=[
                f"Unmodeled store field 'msg_json.{key}' - a new key on the app's "
                f"message record; its value is preserved under the anchor event's "
                f"annotations['unmodeled'] until modeled or ledgered "
                f"(field-level format drift)"
                for key in sorted(unmodeled_keys)
            ],
            batch_safe=True,
            additive=True,  # preserved, not lost — the same bargain a new line field gets
        )
    if annotations_by_uuid:
        for message in messages:
            pmid = message.get("provider_message_id")
            ann = annotations_by_uuid.get(pmid) if pmid else None
            if not ann:
                continue
            provider_data = message.get("provider_data")
            if not isinstance(provider_data, dict):
                provider_data = {}
                message["provider_data"] = provider_data
            existing = provider_data.get("annotations")
            provider_data["annotations"] = (
                {**existing, **ann} if isinstance(existing, dict) else dict(ann)
            )
    return assemble_events(session, thread_id, messages, builder)


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
    the app, its aggregate cost/token stats, and (for a subagent) the parent/root
    frame it was spawned under. Stat columns are present-only: nulls and empties
    (blank ``effort``, an empty ``mentioned_artifact_ids`` list) are skipped."""
    meta: dict[str, Any] = {
        "app": "claude-science",
        "org_uuid": org_uuid,
        "frame_id": frame.get("id"),
        "agent_name": frame.get("agent_name"),
        "conversation_type": frame.get("conversation_type"),
        "project_id": frame.get("project_id"),
        "model": frame.get("model"),
    }
    for col in _FRAME_STAT_COLUMNS:
        value = frame.get(col)
        if value is None:
            continue
        if col == "mentioned_artifact_ids":
            # Stored as JSON text; keep the decoded list, skip empty/undecodable.
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not value:
                continue
        elif isinstance(value, str) and not value.strip():
            continue
        meta[col] = value
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
) -> DbUnitImportResult:
    frame_id = frame["id"]
    source_id = f"{org_uuid}:{frame_id}"
    import_state = get_import_state(session, SOURCE, source_id)

    total = len(message_rows)
    start = import_state.last_line_count if import_state else 0
    if import_state and start >= total:
        return DbUnitImportResult(0, import_state.thread_id or "", False)

    base_ms = int(frame.get("created_at") or 0)
    model = frame.get("model")
    lines: list[dict] = []
    annotations_by_uuid: dict[str, dict] = {}
    unmodeled_keys: set[str] = set()
    # The watermark counts *raw* store rows, so the slice must too: filtering
    # first and slicing the filtered list with a raw offset would skip one
    # importable message for every filtered row behind it — permanently, since
    # the watermark still advances to the raw total.
    for row in message_rows[start:]:
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
            unmodeled_keys |= _unmodeled_store_keys(msg)
            ann = _message_annotations(msg)
            if ann:
                annotations_by_uuid[line["uuid"]] = ann

    # Resolve the thread (watermark wins, else lookup by source) before any create.
    thread_id: Optional[str] = (
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
        return DbUnitImportResult(0, thread_id or "", False)

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

    events_created, last_uuid = _import_science_lines(
        session, thread_id, lines, annotations_by_uuid, parser, builder,
        source_id=source_id, unmodeled_keys=unmodeled_keys,
    )

    # A freshly-created thread that imported nothing (no importable content) is
    # cleaned up so we don't leave an empty thread behind — row AND staged truth
    # record, so no ghost threads/<id>.jsonl survives the commit.
    if is_new_thread and events_created == 0:
        discard_new_thread(session, thread_id)
        thread_id = ""
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
    return DbUnitImportResult(events_created, thread_id or "", is_new_thread)


def import_claude_science_frame(
    org_uuid: str,
    frame: dict,
    message_rows: list,
    parser: Optional[ClaudeCodeParser] = None,
    builder: Optional[DefaultEventBuilder] = None,
    *,
    session=None,
) -> DbUnitImportResult:
    """Import one frame's transcript (atomic per-frame transaction when no session)."""
    parser = parser or ClaudeCodeParser()
    builder = builder or DefaultEventBuilder()
    _probe.count("items")
    if session is not None:
        return _run_frame(session, org_uuid, frame, message_rows, parser, builder)
    with get_session() as s:
        result = _run_frame(s, org_uuid, frame, message_rows, parser, builder)
        with _probe.timed("commit_ms"):
            s.commit()
        return result


# The frame columns the importer requires (title/metadata/timestamps + identity).
_FRAME_COLUMNS = (
    "id", "parent_frame_id", "root_frame_id", "agent_name", "conversation_type",
    "name", "task_summary", "model", "project_id", "created_at",
)

# Aggregate cost/token stat columns, surfaced into thread source_metadata. Selected
# only when the DB carries them (the app's schema grows over time), so an older
# store still imports.
_FRAME_STAT_COLUMNS = (
    "total_cost", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens",
    "aux_cost", "aux_input_tokens", "aux_output_tokens",
    "aux_cache_read_tokens", "aux_cache_write_tokens",
    "effort", "mentioned_artifact_ids",
)


def import_claude_science_db(db_path, org_uuid: str) -> DbScanResult:
    """Open a Claude Science ``operon-cli.db`` (read-only) and import every frame in
    it under ``org_uuid``. The DB is live, so we open it ``mode=ro`` with a busy
    timeout and never write to it."""
    db_path = Path(db_path)
    summary = DbScanResult()
    parser = ClaudeCodeParser()
    builder = DefaultEventBuilder()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='frames'"
        ).fetchone():
            return summary

        have = {row[1] for row in conn.execute("PRAGMA table_info(frames)")}
        columns = _FRAME_COLUMNS + tuple(c for c in _FRAME_STAT_COLUMNS if c in have)

        type_ph = ", ".join("?" for _ in _NON_CONVERSATION_TYPES)
        proj_ph = ", ".join("?" for _ in _SEEDED_PROJECT_IDS)
        frames = conn.execute(
            f"SELECT {', '.join(columns)} FROM frames "
            f"WHERE conversation_type NOT IN ({type_ph}) "
            f"  AND (project_id IS NULL OR project_id NOT IN ({proj_ph})) "
            f"ORDER BY created_at",
            (*_NON_CONVERSATION_TYPES, *_SEEDED_PROJECT_IDS),
        ).fetchall()

        for frame_row in frames:
            frame = dict(zip(columns, frame_row))
            frame_id = frame["id"]
            message_rows = conn.execute(
                "SELECT idx, msg_json FROM frame_messages WHERE frame_id = ? ORDER BY idx",
                (frame_id,),
            ).fetchall()
            if not message_rows:
                continue
            summary.processed += 1
            try:
                result = import_claude_science_frame(
                    org_uuid, frame, message_rows, parser, builder
                )
                if result.events_created > 0:
                    summary.imported += 1
                    summary.events_created += result.events_created
            except Exception as e:  # noqa: BLE001 — one bad frame must not stop the scan
                # Counted out to the watcher, not just logged — see the same guard in
                # opencode: a silently-skipped frame reads as "nothing new" and leaves
                # `thread-archive status` green while a conversation is missing.
                logger.exception(
                    "import_claude_science_db: frame %s failed; skipping", frame_id[:8]
                )
                summary.failed += 1
                summary.errors.append(f"frame {frame_id[:8]}: {e}")
    finally:
        conn.close()
    return summary
