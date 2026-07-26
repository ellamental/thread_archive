"""Backfill importer-dropped non-content fields onto stored events, all sources.

The importers preserve, per source, fields the archive's stored events may lack:
structured usage (``input_tokens`` / ``output_tokens`` / ``thinking_tokens`` /
``cache_read_tokens`` / ``cache_write_tokens`` / ``uncached_tokens``), ``cost``,
``stop_reason``, the ``annotations`` dict on anchor payloads
(``api_request_completed`` / ``user_message_sent``) and on block-level payloads
(``thinking_complete`` / ``text_complete`` / ``tool_use_complete`` /
``tool_execution_completed`` / ``tool_execution_error``), and codex user images
(``images`` + ``content_type``). This script re-parses every incremental source
through its own importer's message builders, matches each fresh event to its
stored row, and merges the *missing* fields on via the amendment mechanism
(:mod:`.._ops.amend`) — generalizing :mod:`.backfill_usage_cost` to every event
type and every incremental source.

Matching per thread: ``dedup_key`` first (timestamp-free, content-inclusive),
then exact content anchor (type, timestamp, block, content_hash) with
provider_message_id agreement — :mod:`.backfill_reconcile`'s helpers. A fresh
event that matches neither falls to the structural-anchor salvage / insert
phases below.

Why each write is amendment-legal (content identity never touched):

* **Missing-key merge** adds only keys outside ``_DEDUP_CONTENT_KEYS`` (amend's
  ``check_patch`` refuses anything else); the merged payload re-hashes to the
  same ``dedup_key``, so re-import idempotency and the verify gates stay green.
  ``timestamp_inferred`` / ``timestamp_source`` are never patched (a fresh
  re-parse infers timestamps differently than the original chunked import), and
  ``model`` is content-identity and is never patched.
* **Zero-token overwrite**: usage keys stored as ``0`` are the retired
  importers' placeholder for "not recorded"; a fresh nonzero count replaces the
  placeholder. Usage keys are outside the content hash, and
  ``amend_event_payloads`` permits value changes (it drops only keys whose
  stored value already equals the patch value).
* **Annotations deep-merge** is add-only: missing subkeys from the fresh
  ``annotations`` dict are added; an existing subkey is never overwritten.
  ``annotations`` is deliberately outside the dedup content keys.
* **Structural-anchor salvage** (a fresh/stored pair whose *content* drifted —
  key and content anchor both miss but ``(type, timestamp, block)`` matches a
  unique stored row): the drifted content field itself stays untouched; only an
  annotation is added — ``annotations.error_detail`` for a stored
  ``tool_execution_error`` with an empty ``error`` (the hollow errors the
  antigravity/opencode importers used to write), ``annotations.model_name`` for
  a stored api_request event whose ``model`` is the ``"cursor"`` placeholder.
  Any other structural match is counted as ``content_drift_skipped``, never
  merged.
* **Insert-dropped** writes new rows only for an allowlist of event types the
  retired importers never wrote at all — antigravity ``thinking_complete``,
  cursor ``context_summary``, codex image-only ``user_message_sent`` — so a
  fresh key can only collide with a genuine duplicate (the same safety argument
  as :mod:`.recover_dropped_events`). An insert borrows its turn's ``stream_id``
  (and ``api_call_id`` for ``thinking_complete``) from adjacent fresh events of
  the same turn that DID match stored rows; a turn with no matched neighbor is
  skipped (``insert_unanchored_skipped``), never guessed. Inserts go through
  ``write_events`` — the ordinary truth+index seam — so the truth line lands
  with the same commit as the row.
* **Thread metadata merge** adds only keys absent from
  ``Thread.source_metadata`` (the thread-level fields the importers now derive:
  codex git/cli_version, grok session fields, opencode project fields,
  claude-science frame stats, cowork ``session_stats``), re-staged to truth via
  the importers' ``_restage_thread`` seam so a reindex keeps the merge.

Missing-only semantics throughout make re-runs no-ops. Any ambiguity — two
content-anchor candidates, two structural candidates — is counted and skipped,
never guessed at. ``--limit`` caps threads
examined *per source* (so a capped sweep still samples every source). Dry-run
by default; ``--apply`` writes, with inserted event ids appended to the
``--backup`` JSONL (amended ids + before-values already live in
``truth/amendments.jsonl``). A source whose store/files are unavailable is
skipped with a count.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import uuid as _uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, cast

from sqlalchemy import select

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.event_builder import ThreadEvent
from thread_archive._thread_import.parsers.base import NormalizedMessage
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser

from .._importers._events import _to_event
from .._importers._read import read_session_lines
from .._importers._state import _restage_thread
from .._importers.antigravity import _build_antigravity_messages
from .._importers.claude_science import (
    _FRAME_COLUMNS,
    _FRAME_STAT_COLUMNS,
    _NON_CONVERSATION_TYPES,
    _SEEDED_PROJECT_IDS,
    _frame_metadata,
    _message_annotations,
    _synthesize_line,
)
from .._importers.codex import (
    _build_codex_messages,
    _codex_call_maps,
    _codex_session_meta,
    _codex_source_metadata,
)
from .._importers.cowork import _normalize_cowork_line, _session_stats
from .._importers.cursor import _build_cursor_messages, _cursor_to_normalized
from .._importers.grok import (
    _build_grok_messages,
    _grok_prompt_ts_map,
    _grok_session_meta,
    _grok_source_metadata,
    _grok_tool_timestamps,
    _parse_grok_timestamp,
)
from .._importers.opencode import (
    _build_opencode_messages,
    _opencode_to_normalized,
    _parse_opencode_timestamp,
)
from .._ops.amend import _PROTECTED_KEYS, amend_event_payloads
from .._retrieval.fts import index_events
from .._store import Event, ImportState, Thread, get_session
from .._truth import write_events
from .._watcher.sources import (
    CoworkWatcher,
    _cursor_default_db,
    _opencode_default_db,
    antigravity_watcher,
    codex_watcher,
    discover_claude_science_dbs,
    grok_watcher,
)
from .backfill_reconcile import (
    _content_anchor,
    _norm_key,
    _pmid,
    _struct_anchor,
)
from .backfill_reconcile import (
    _fresh_events as _cc_fresh_events,
)
from .backfill_reconcile import (
    _iter_pairs as _cc_iter_pairs,
)

logger = logging.getLogger(__name__)

# Payload keys never merged: content identity (enforced by check_patch anyway)
# plus the timestamp-provenance flags, which a full re-parse derives differently
# than the original chunked import did.
_NEVER_PATCH = frozenset(_PROTECTED_KEYS) | {
    "timestamp_inferred",
    "timestamp_source",
    # A legacy Codex row's inclusive input value must remain paired with an
    # absent marker so the stats compatibility fold can recognize it. Adding
    # the fixed importer's ``False`` marker without overwriting that nonzero
    # input value would mislabel and re-inflate the historical row.
    "input_tokens_includes_cache",
}

# Usage keys where a stored 0 is the retired importers' "not recorded"
# placeholder and a fresh nonzero count may replace it.
_TOKEN_KEYS = frozenset({
    "input_tokens", "output_tokens", "thinking_tokens",
    "cache_read_tokens", "cache_read_input_tokens", "cache_write_tokens",
    "uncached_tokens",
})


@dataclass(frozen=True)
class WorkItem:
    """One re-parseable source conversation: its identity plus lazy builders."""
    source: str
    source_id: str
    fresh: Callable[[], list[ThreadEvent]]
    meta: Optional[Callable[[], Optional[dict]]]   # () -> thread source_metadata


# ── fresh event assembly (mirrors assemble_events' stream/timestamp logic) ───

def _events_from_messages(
    messages: Iterable[NormalizedMessage | dict[str, Any]],
    base_prev_ts: Optional[datetime] = None,
) -> list[ThreadEvent]:
    """Build ThreadEvents from NormalizedMessages with the same role→stream and
    monotonic-timestamp rules as ``assemble_events`` (stream ids are fresh UUIDs —
    matching never reads them; inserts borrow stored ids instead)."""
    builder = DefaultEventBuilder()
    out: list[ThreadEvent] = []
    prev = base_prev_ts
    current_stream: Optional[str] = None
    for raw_message in messages:
        msg = cast(NormalizedMessage, raw_message)
        role = msg.get("role", "")
        if role == "user":
            current_stream = str(_uuid.uuid4())
            evs = builder.build_events(msg, current_stream, prev_occurred_at=prev)
        elif role == "assistant":
            if not current_stream:
                current_stream = str(_uuid.uuid4())
            evs = builder.build_events(
                msg, current_stream, str(_uuid.uuid4()), prev_occurred_at=prev
            )
        else:
            evs = builder.build_events(
                msg, current_stream or str(_uuid.uuid4()), prev_occurred_at=prev
            )
        if evs:
            prev = evs[-1].occurred_at
        out.extend(evs)
    return out


# ── per-source adapters (discovery + fresh build + thread metadata) ──────────

def _cc_item_fresh(path: Path, source_name: str) -> list[ThreadEvent]:
    return _cc_fresh_events(source_name, read_session_lines(path))


def _codex_item_meta(path: Path) -> Optional[dict]:
    return _codex_source_metadata(_codex_session_meta(read_session_lines(path)))


def _grok_item_meta(path: Path, source_id: str) -> Optional[dict]:
    return _grok_source_metadata(_grok_session_meta(path.parent), source_id)


def _antigravity_item_fresh(path: Path, source_id: str) -> list[ThreadEvent]:
    lines = read_session_lines(path)
    return _events_from_messages(_build_antigravity_messages(lines, lines, source_id))


def iter_cc_like_items(source_name: str) -> Iterator[WorkItem]:
    """One Claude-Code-shaped source's transcripts, via backfill_reconcile's discovery."""
    for name, path, source_id in _cc_iter_pairs():
        if name != source_name:
            continue
        yield WorkItem(
            name, source_id,
            fresh=partial(_cc_item_fresh, path, name),
            meta=None,
        )


def iter_codex_items(sessions_dir: Optional[Path] = None) -> Iterator[WorkItem]:
    w = codex_watcher(sessions_dir)
    if not w.is_available():
        return
    for path, source_id in w.iter_files():
        def fresh(p=path):
            lines = read_session_lines(p)
            names, inputs = _codex_call_maps(lines)
            # Full-file re-parse: the model/ambient context enters at the
            # defaults and the lines themselves re-declare it per turn.
            return _events_from_messages(
                _build_codex_messages(lines, "codex", names, inputs, ambient=None)
            )
        yield WorkItem(
            "codex", source_id, fresh=fresh,
            meta=partial(_codex_item_meta, path),
        )


def iter_grok_items(sessions_dir: Optional[Path] = None) -> Iterator[WorkItem]:
    w = grok_watcher(sessions_dir)
    if not w.is_available():
        return
    for path, source_id in w.iter_files():
        def fresh(p=path, sid=source_id):
            lines = read_session_lines(p)
            meta = _grok_session_meta(p.parent)
            messages = _build_grok_messages(
                lines, meta, _grok_tool_timestamps(p.parent),
                _grok_prompt_ts_map(p.parent, sid), sid, prefix_lines=[],
            )
            base = _parse_grok_timestamp(meta.get("created_at"))
            return _events_from_messages(messages, base_prev_ts=base)
        yield WorkItem(
            "grok", source_id, fresh=fresh,
            meta=partial(_grok_item_meta, path, source_id),
        )


def iter_antigravity_items(brain_dir: Optional[Path] = None) -> Iterator[WorkItem]:
    w = antigravity_watcher(brain_dir)
    if not w.is_available():
        return
    for path, source_id in w.iter_files():
        yield WorkItem(
            "antigravity", source_id,
            fresh=partial(_antigravity_item_fresh, path, source_id),
            meta=None,
        )


def iter_cowork_items() -> Iterator[WorkItem]:
    w = CoworkWatcher()
    if not w.is_available():
        return
    for audit_path, source_id, _meta_path in w._iter_sessions():
        def _lines(p=audit_path):
            return [_normalize_cowork_line(ln) for ln in read_session_lines(p)
                    if isinstance(ln, dict)]
        def fresh(p=audit_path):
            return _cc_fresh_events("claude-code", _lines(p))
        def meta(p=audit_path):
            stats = _session_stats(_lines(p))
            return {"session_stats": stats} if stats else None
        yield WorkItem("cowork", source_id, fresh=fresh, meta=meta)


def iter_opencode_items(db_path: Optional[Path] = None) -> Iterator[WorkItem]:
    db_path = db_path or _opencode_default_db()
    if db_path is None or not Path(db_path).exists():
        return
    sessions: dict[str, dict] = {}
    messages_by_session: dict[str, list[tuple[str, dict]]] = {}
    parts_by_message: dict[str, list[dict]] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('session','message','part')"
        )}
        if not {"session", "message", "part"} <= have:
            return
        for sid, project_id, parent_id, title, directory, t_c, t_u, agent in conn.execute(
            "SELECT id, project_id, parent_id, title, directory, time_created, "
            "time_updated, agent FROM session"
        ):
            sessions[sid] = {
                "id": sid, "project_id": project_id, "parent_id": parent_id,
                "title": title, "directory": directory,
                "time_created": t_c, "time_updated": t_u, "agent": agent,
            }
        for msg_id, session_id, data in conn.execute(
            "SELECT id, session_id, data FROM message ORDER BY time_created, id"
        ):
            try:
                parsed = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue  # corrupt rows are the importer's problem, not a field backfill's
            messages_by_session.setdefault(session_id, []).append((msg_id, parsed))
        for message_id, data in conn.execute(
            "SELECT message_id, data FROM part ORDER BY time_created, id"
        ):
            try:
                parsed = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            parts_by_message.setdefault(message_id, []).append(parsed)
    finally:
        conn.close()

    for session_id, session_data in sessions.items():
        def fresh(sid=session_id, sd=session_data):
            norm = _build_opencode_messages(
                messages_by_session.get(sid, []), parts_by_message, session_data=sd
            )
            base = _parse_opencode_timestamp(sd.get("time_created"))
            return _events_from_messages(
                [_opencode_to_normalized(m) for m in norm], base_prev_ts=base
            )
        def meta(sd=session_data):
            out = {k: v for k, v in {
                "project_id": sd.get("project_id"),
                "directory": sd.get("directory"),
                "agent": sd.get("agent"),
                "parent_id": sd.get("parent_id"),
            }.items() if v is not None}
            return out or None
        yield WorkItem("opencode", session_id, fresh=fresh, meta=meta)


def iter_cursor_items(db_path: Optional[Path] = None) -> Iterator[WorkItem]:
    db_path = db_path or _cursor_default_db()
    if db_path is None or not Path(db_path).exists():
        return
    composers: dict[str, dict] = {}
    bubbles: dict[str, dict] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
        ).fetchone():
            return
        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            try:
                composers[key.replace("composerData:", "")] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ):
            parts = key.split(":")
            if len(parts) < 3:
                continue
            try:
                data = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            data["_composerId"] = parts[1]
            bubbles[f"{parts[1]}:{parts[2]}"] = data
    finally:
        conn.close()

    for composer_id, composer_data in composers.items():
        composer_bubbles = {
            k: v for k, v in bubbles.items() if k.startswith(f"{composer_id}:")
        }
        def fresh(cid=composer_id, cd=composer_data, cb=composer_bubbles):
            messages = _build_cursor_messages(cid, cd, cb)
            return _events_from_messages([_cursor_to_normalized(m) for m in messages])
        yield WorkItem("cursor", composer_id, fresh=fresh, meta=None)


def iter_claude_science_items(base: Optional[Path] = None) -> Iterator[WorkItem]:
    for db_path, org_uuid in discover_claude_science_dbs(base):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if not conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='frames'"
            ).fetchone():
                continue
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
                message_rows = conn.execute(
                    "SELECT idx, msg_json FROM frame_messages WHERE frame_id = ? ORDER BY idx",
                    (frame["id"],),
                ).fetchall()
                if not message_rows:
                    continue
                yield WorkItem(
                    "claude-science", f"{org_uuid}:{frame['id']}",
                    fresh=partial(_science_fresh, frame, message_rows),
                    meta=partial(_frame_metadata, org_uuid, frame),
                )
        finally:
            conn.close()


def _science_fresh(frame: dict, message_rows: list) -> list:
    """Synthesize CC-shaped lines from a frame (as the importer does), parse them,
    merge the science-specific message annotations, and build events."""
    base_ms = int(frame.get("created_at") or 0)
    model = frame.get("model")
    lines: list[dict] = []
    annotations_by_uuid: dict[str, dict] = {}
    for row in message_rows:
        idx, raw = row[0], row[1]
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict):
            continue
        line = _synthesize_line(frame["id"], idx, msg, model, base_ms)
        if line is None:
            continue
        lines.append(line)
        ann = _message_annotations(msg)
        if ann:
            annotations_by_uuid[line["uuid"]] = ann
    messages = ClaudeCodeParser().parse_export({
        "provider": "claude-code",
        "sessions": [{"session_id": "backfill", "project": "backfill", "lines": lines}],
    })
    for message in messages:
        provider_message_id = message.get("provider_message_id")
        ann = (
            annotations_by_uuid.get(provider_message_id)
            if isinstance(provider_message_id, str)
            else None
        )
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
    return _events_from_messages(messages)


# name → zero-arg iterator over WorkItems; empty iteration ⇒ store unavailable.
# The per-provider entries below are the ones needing bespoke re-parse knowledge;
# every Claude-Code-shaped source shares one adapter and is discovered from the
# registry, so a harness declaring that parser is audited without an entry here.
_BESPOKE_ADAPTERS: dict[str, Callable[[], Iterator[WorkItem]]] = {
    "codex": iter_codex_items,
    "grok": iter_grok_items,
    "antigravity": iter_antigravity_items,
    "opencode": iter_opencode_items,
    "cursor": iter_cursor_items,
    "claude-science": iter_claude_science_items,
    "cowork": iter_cowork_items,
}


def adapters() -> dict[str, Callable[[], Iterator[WorkItem]]]:
    """Every source this audit can re-derive, by name.

    A source with no adapter has no dropped-field audit at all, and silently:
    nothing reports that its fields were never checked. Deriving the
    Claude-Code-shaped entries from the registry is what keeps that from
    happening to a harness that shares the format.
    """
    from .._providers import sources_using_parser

    found: dict[str, Callable[[], Iterator[WorkItem]]] = {}
    for provider in sources_using_parser("claude-code"):
        found[provider.name] = partial(iter_cc_like_items, provider.name)
    found.update(_BESPOKE_ADAPTERS)
    return found


# ── patch construction ───────────────────────────────────────────────────────

def _merge_annotations(stored_ann: Any, fresh_ann: Any) -> Optional[dict]:
    """The annotations dict to patch — stored subkeys always win, only missing
    subkeys are added. None when nothing new (or the shapes don't allow a merge)."""
    if not isinstance(fresh_ann, dict) or not fresh_ann:
        return None
    if stored_ann is None:
        return dict(fresh_ann)
    if not isinstance(stored_ann, dict):
        return None  # unexpected stored shape — leave it alone
    merged = {**fresh_ann, **stored_ann}
    return merged if set(merged) - set(stored_ann) else None


def _build_patch(stored: dict, fresh: dict) -> dict:
    """Missing-only patch of ``fresh`` onto ``stored`` — plus the zero-token
    placeholder overwrite and the annotations deep-merge. Content-identity keys
    and timestamp provenance are never included."""
    patch: dict = {}
    for k, v in fresh.items():
        if k in _NEVER_PATCH or v is None:
            continue
        if k == "annotations":
            merged = _merge_annotations(stored.get("annotations"), v)
            if merged is not None:
                patch["annotations"] = merged
            continue
        if k not in stored:
            patch[k] = v
        elif (
            k in _TOKEN_KEYS and stored.get(k) == 0
            and isinstance(v, int) and not isinstance(v, bool) and v != 0
        ):
            patch[k] = v
    return patch


def _salvage_patch(stored_ev, fresh_ev) -> tuple[Optional[dict], str]:
    """The annotation-only patch for a content-drifted structural match.

    Returns ``(patch, reason)``: a hollow stored ``tool_execution_error`` gains
    ``annotations.error_detail`` from the fresh error text; a stored api_request
    event with the ``"cursor"`` model placeholder gains ``annotations.model_name``
    from the fresh model. Reasons: ``error_detail`` / ``model_name`` when patched,
    ``already`` when the annotation exists, ``drift`` for any other drift."""
    stored_p = stored_ev.payload if isinstance(stored_ev.payload, dict) else {}
    fresh_p = fresh_ev.payload or {}
    ann = stored_p.get("annotations")
    ann = dict(ann) if isinstance(ann, dict) else {}

    if stored_ev.event_type == "tool_execution_error":
        fresh_err = fresh_p.get("error")
        if not str(stored_p.get("error") or "").strip() and str(fresh_err or "").strip():
            if "error_detail" in ann:
                return None, "already"
            ann["error_detail"] = fresh_err
            return {"annotations": ann}, "error_detail"

    if stored_ev.event_type in ("api_request_started", "api_request_completed"):
        fresh_model = fresh_p.get("model")
        if stored_p.get("model") == "cursor" and fresh_model not in (None, "", "cursor"):
            if "model_name" in ann:
                return None, "already"
            ann["model_name"] = fresh_model
            return {"annotations": ann}, "model_name"

    return None, "drift"


# ── insert allowlist ─────────────────────────────────────────────────────────

def _insertable(source: str, ev) -> bool:
    """Event types the retired importers never wrote — the only rows this script
    may insert (a fresh key can then only collide with a genuine duplicate)."""
    if source == "antigravity" and ev.event_type == "thinking_complete":
        return True
    if source == "cursor" and ev.event_type == "context_summary":
        return True
    if source == "codex" and ev.event_type == "user_message_sent":
        payload = ev.payload or {}
        return bool(payload.get("images")) and not str(payload.get("content") or "").strip()
    return False


# ── planning ─────────────────────────────────────────────────────────────────

@dataclass
class ThreadPlan:
    thread_id: str
    patches: list          # [(event_id, patch)]
    inserts: list          # [ThreadEvent] with borrowed stream/api ids set
    meta_missing: dict     # thread source_metadata keys to add
    stats: dict


def plan_thread(session, thread_id: str, source: str, fresh: list) -> ThreadPlan:
    """Match fresh events to stored rows and plan patches/inserts. Pure planning."""
    stats: dict[str, int] = defaultdict(int)
    stored = list(
        session.execute(
            select(Event).where(Event.thread_id == thread_id).order_by(Event.id)
        ).scalars().all()
    )
    by_key = {_norm_key(e.dedup_key, thread_id): e for e in stored if e.dedup_key}
    content_idx: dict[tuple, list] = defaultdict(list)
    struct_idx: dict[tuple, list] = defaultdict(list)
    for e in stored:
        p = e.payload if isinstance(e.payload, dict) else {}
        content_idx[_content_anchor(e.event_type, p, e.occurred_at)].append(e)
        struct_idx[_struct_anchor(e.event_type, p, e.occurred_at)].append(e)

    used: set[int] = set()
    patches: list[tuple[int, dict]] = []
    unmatched: list = []
    stream_map: dict[str, str] = {}      # fresh stream_id → stored stream_id
    api_map: dict[str, str] = {}         # fresh api_call_id → stored api_call_id

    def record_linkage(f, m) -> None:
        stream_map.setdefault(f.stream_id, m.stream_id)
        if f.api_call_id and m.api_call_id:
            api_map.setdefault(f.api_call_id, m.api_call_id)

    for f in fresh:
        match = by_key.get(f.dedup_key)
        if match is None:
            candidates = []
            for e in content_idx.get(
                _content_anchor(f.event_type, f.payload, f.occurred_at), []
            ):
                if e.id in used:
                    continue
                fp, ep = _pmid(f.payload or {}), _pmid(e.payload or {})
                if fp is not None and ep is not None and fp != ep:
                    continue  # same content, different turn — not this event
                candidates.append(e)
            if len(candidates) > 1:
                stats["ambiguous"] += 1
                continue  # never guess; not eligible for salvage/insert either
            match = candidates[0] if candidates else None
        if match is None:
            unmatched.append(f)
            continue
        if match.id in used:
            stats["dup_fresh"] += 1
            continue
        used.add(match.id)
        record_linkage(f, match)
        old = match.payload if isinstance(match.payload, dict) else {}
        patch = _build_patch(old, f.payload or {})
        if not patch:
            stats["already_complete"] += 1
            continue
        for k in patch:
            stats[f"field:{k}"] += 1
        patches.append((match.id, patch))

    # Salvage / insert phase for fresh events nothing matched.
    inserts: list = []
    planned_insert_keys: set[str] = set()
    for f in unmatched:
        candidates = [
            e for e in struct_idx.get(
                _struct_anchor(f.event_type, f.payload or {}, f.occurred_at), []
            )
            if e.id not in used
        ]
        if len(candidates) == 1:
            e = candidates[0]
            salvage_patch, reason = _salvage_patch(e, f)
            if salvage_patch is not None:
                used.add(e.id)
                record_linkage(f, e)
                stats[f"salvage:{reason}"] += 1
                patches.append((e.id, salvage_patch))
            elif reason == "already":
                used.add(e.id)
                record_linkage(f, e)
                stats["salvage_already"] += 1
            else:
                stats["content_drift_skipped"] += 1
            continue
        if len(candidates) > 1:
            stats["struct_ambiguous"] += 1
            continue
        if not _insertable(source, f):
            stats["unmatched_fresh"] += 1
            continue
        if f.dedup_key and (f.dedup_key in by_key or f.dedup_key in planned_insert_keys):
            stats["already_complete"] += 1
            continue
        borrowed_stream = stream_map.get(f.stream_id)
        borrowed_api = api_map.get(f.api_call_id) if f.api_call_id else None
        if borrowed_stream is None or (
            f.event_type == "thinking_complete" and borrowed_api is None
        ):
            stats["insert_unanchored_skipped"] += 1
            continue
        f.stream_id = borrowed_stream
        f.api_call_id = borrowed_api
        if f.dedup_key:
            planned_insert_keys.add(f.dedup_key)
        stats[f"insert:{f.event_type}"] += 1
        inserts.append(f)

    stats["patches"] = len(patches)
    stats["inserts"] = len(inserts)
    return ThreadPlan(thread_id, patches, inserts, {}, dict(stats))


def _plan_meta(session, thread_id: str, fresh_meta: Optional[dict]) -> dict:
    """Thread source_metadata keys the fixed importer derives that the stored
    thread lacks. Missing-only — an existing key is never touched."""
    if not fresh_meta:
        return {}
    thread = session.get(Thread, thread_id)
    if thread is None:
        return {}
    stored = thread.source_metadata or {}
    return {k: v for k, v in fresh_meta.items() if v is not None and k not in stored}


# ── run ──────────────────────────────────────────────────────────────────────

def _iter_default_items(sources: Optional[set[str]], totals) -> Iterator[WorkItem]:
    for name, factory in adapters().items():
        if sources and name not in sources:
            continue
        yielded = False
        for item in factory():
            yielded = True
            yield item
        if not yielded:
            totals[name]["source_unavailable_or_empty"] += 1


def _apply_thread(thread_id: str, source: str, source_id: str, plan: ThreadPlan, backup) -> dict:
    """Write one thread's planned changes: amendments (their own locked seam),
    then inserts through write_events (truth + index atomic per commit), then the
    metadata merge. Returns applied counters."""
    out: dict[str, int] = defaultdict(int)
    if plan.patches:
        res = amend_event_payloads(
            ((thread_id, eid, patch) for eid, patch in plan.patches),
            reason=f"backfill_dropped_fields: {source}/{source_id}",
        )
        out["events_amended"] += res["events_amended"]
    if plan.inserts:
        with get_session() as s:
            rows = [_to_event(thread_id, te) for te in plan.inserts]
            write_events(s, rows)
            index_events(s, rows)
            s.commit()
            inserted_ids = [r.id for r in rows]
        out["events_inserted"] += len(inserted_ids)
        if backup is not None:
            backup.write(json.dumps({
                "type": "insert", "source": source, "source_id": source_id,
                "thread_id": thread_id, "event_ids": inserted_ids,
                "at": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            backup.flush()
    if plan.meta_missing:
        with get_session() as s:
            thread = s.get(Thread, thread_id)
            if thread is not None:
                meta = dict(thread.source_metadata or {})
                added = {k: v for k, v in plan.meta_missing.items() if k not in meta}
                if added:
                    meta.update(added)
                    thread.source_metadata = meta
                    _restage_thread(s, thread)
                    s.commit()
                    out["meta_keys_added"] += len(added)
    return dict(out)


def run(
    *,
    apply: bool = False,
    limit: Optional[int] = None,
    sources: Optional[set[str]] = None,
    backup_path: Optional[Path] = None,
    items: Optional[Iterable[WorkItem]] = None,
) -> dict:
    """Sweep every source, plan per thread, write on ``apply``.

    ``items`` overrides adapter discovery (tests feed scripted stores);
    ``limit`` caps threads examined per source. Returns
    ``{"sources": {name: stats}, "totals": stats}``.
    """
    per_source: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    examined: dict[str, int] = defaultdict(int)
    backup = open(backup_path, "a", encoding="utf-8") if (apply and backup_path) else None
    try:
        stream = items if items is not None else _iter_default_items(sources, per_source)
        for item in stream:
            if sources and item.source not in sources:
                continue
            if limit is not None and examined[item.source] >= limit:
                continue
            st = per_source[item.source]
            with get_session() as s:
                state = s.execute(
                    select(ImportState).where(
                        ImportState.source == item.source,
                        ImportState.source_id == item.source_id,
                    )
                ).scalar_one_or_none()
                if state is None or not state.thread_id:
                    st["no_thread"] += 1
                    continue
                examined[item.source] += 1
                st["threads"] += 1
                thread_id = state.thread_id
                try:
                    plan = plan_thread(s, thread_id, item.source, item.fresh())
                    plan.meta_missing = _plan_meta(
                        s, thread_id, item.meta() if item.meta else None
                    )
                except Exception as e:  # noqa: BLE001 — one bad thread must not stop the sweep
                    logger.warning("plan failed for %s/%s: %s", item.source, item.source_id, e)
                    st["plan_errors"] += 1
                    continue
            for k, v in plan.stats.items():
                st[k] += v
            st["meta_keys_missing"] += len(plan.meta_missing)
            if plan.patches or plan.inserts or plan.meta_missing:
                st["threads_changed"] += 1
                if apply:
                    try:
                        applied = _apply_thread(
                            thread_id, item.source, item.source_id, plan, backup
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "apply failed for %s/%s: %s", item.source, item.source_id, e
                        )
                        st["apply_errors"] += 1
                        continue
                    for k, v in applied.items():
                        st[k] += v
    finally:
        if backup:
            backup.close()

    totals: dict[str, int] = defaultdict(int)
    for st in per_source.values():
        for k, v in st.items():
            totals[k] += v
    return {"sources": {k: dict(v) for k, v in per_source.items()}, "totals": dict(totals)}


def main(
    argv: Optional[list[str]] = None,
    *,
    items: Optional[Iterable[WorkItem]] = None,
) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap threads examined per source")
    ap.add_argument(
        "--source", action="append", default=None,
        help="restrict to a source (repeatable; default: all adapters)",
    )
    ap.add_argument("--backup", type=Path, default=None,
                    help="JSONL of inserted event ids (apply; amendments audit themselves)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    res = run(
        apply=args.apply, limit=args.limit,
        sources=set(args.source) if args.source else None,
        backup_path=args.backup, items=items,
    )
    totals = res["totals"]
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] backfill-dropped-fields")
    print(f"  threads examined:    {totals.get('threads', 0):,}")
    print(f"  threads changed:     {totals.get('threads_changed', 0):,}")
    print(f"  patches planned:     {totals.get('patches', 0):,}")
    if args.apply:
        print(f"  events amended:      {totals.get('events_amended', 0):,}")
        print(f"  events inserted:     {totals.get('events_inserted', 0):,}")
        print(f"  meta keys added:     {totals.get('meta_keys_added', 0):,}")
    else:
        print(f"  inserts planned:     {totals.get('inserts', 0):,}")
        print(f"  meta keys missing:   {totals.get('meta_keys_missing', 0):,}")
    for k in sorted(totals):
        if k.startswith(("field:", "salvage:", "insert:")):
            print(f"    {k:<32}{totals[k]:,}")
    for k in (
        "already_complete", "unmatched_fresh", "ambiguous", "struct_ambiguous",
        "content_drift_skipped", "insert_unanchored_skipped", "salvage_already",
        "dup_fresh", "no_thread", "plan_errors", "apply_errors",
        "source_unavailable_or_empty",
    ):
        if totals.get(k):
            print(f"  {k:<21}{totals[k]:,}")
    print("  per source:")
    for name in sorted(res["sources"]):
        st = res["sources"][name]
        print(
            f"    {name:<16} threads={st.get('threads', 0):,} "
            f"patches={st.get('patches', 0):,} inserts={st.get('inserts', 0):,} "
            f"meta={st.get('meta_keys_missing', st.get('meta_keys_added', 0)):,} "
            f"unmatched={st.get('unmatched_fresh', 0):,} "
            f"errors={st.get('plan_errors', 0) + st.get('apply_errors', 0):,}"
        )


if __name__ == "__main__":
    main()
