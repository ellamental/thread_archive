"""OpenCode (`opencode.db`) DB scan + per-session import + assembler.

One SQLite DB holds every session (session / message / part tables). We open it
read-only, read everything into memory, and dispatch each session to the
per-session importer (incremental skip via the import_state cursor on
``time_updated``; a streaming assistant message with no ``time.completed`` stops
the settled prefix so a half-finished turn isn't imported). Pure ``_opencode_*`` /
``_build_opencode_messages`` helpers copied verbatim; orchestration rewired.

Capture-everything invariant: no provider record is silently dropped on import.
Everything the DB holds reaches an event, leaning on the shared builder's two
verbatim-preservation paths — an unmodeled role becomes a ``message`` event
(``content_blocks`` kept), and an unmodeled assistant block becomes a
``content_block`` event (raw under ``data``). So non-user/assistant messages,
non-text user parts, unknown assistant part types, and even JSON that failed to
parse are all preserved rather than skipped.

Settled-prefix exception (the abandoned-turn hole): the ``time.completed`` gate
holds back a *live* streaming turn so its churning partial content isn't stacked
every poll (its dedup_key changes as it streams). But a turn that crashed and
will never get ``completed`` written would strand itself AND every message after
it forever. So the gate is staleness-aware: on a session untouched for longer
than ``_OPENCODE_ABANDONED_MS`` the turn is treated as abandoned — its
already-present content is imported once (tagged ``stop_reason="incomplete"``)
and the walk continues so the tail survives. A fresh session still breaks (no
churn); a stale session is re-imported at most once (the ``time_updated`` cursor
skips it thereafter), so the partial is captured exactly once.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder

from .._store import ImportState, get_session
from ._events import assemble_events
from ._state import create_thread, get_import_state, get_thread_by_source, upsert_import_state

logger = logging.getLogger(__name__)


@dataclass
class OpenCodeImportResult:
    events_created: int
    thread_id: int
    is_new_thread: bool


@dataclass
class OpenCodeDbScanResult:
    sessions_processed: int
    sessions_imported: int
    events_created: int
    sessions_failed: int = 0
    errors: list[str] = field(default_factory=list)


def import_opencode_db(db_path) -> OpenCodeDbScanResult:
    """Open an OpenCode ``opencode.db`` and import every session in it."""
    import sqlite3

    db_path = Path(db_path)
    sessions: dict[str, dict[str, Any]] = {}
    messages_by_session: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    parts_by_message: dict[str, list[dict[str, Any]]] = {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        have = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('session','message','part')"
            )
        }
        if not {"session", "message", "part"} <= have:
            return OpenCodeDbScanResult(0, 0, 0)

        for sid, project_id, parent_id, title, directory, t_created, t_updated, agent in conn.execute(
            "SELECT id, project_id, parent_id, title, directory, time_created, "
            "time_updated, agent FROM session"
        ):
            sessions[sid] = {
                "id": sid,
                "project_id": project_id,
                "parent_id": parent_id,
                "title": title,
                "directory": directory,
                "time_created": t_created,
                "time_updated": t_updated,
                "agent": agent,
            }

        for msg_id, session_id, data in conn.execute(
            "SELECT id, session_id, data FROM message ORDER BY time_created, id"
        ):
            try:
                parsed = json.loads(data)
            except (json.JSONDecodeError, TypeError) as e:
                # Corrupt message row: don't drop it silently. Carry a stub with the
                # raw text + error so `_build_opencode_messages` can preserve it as a
                # visible parse_error record (mirrors the parse_error convention).
                parsed = {"_opencode_parse_error": str(e), "_raw_data": _opencode_raw_text(data)}
            messages_by_session.setdefault(session_id, []).append((msg_id, parsed))

        for message_id, data in conn.execute(
            "SELECT message_id, data FROM part ORDER BY time_created, id"
        ):
            try:
                parsed = json.loads(data)
            except (json.JSONDecodeError, TypeError) as e:
                # Corrupt part row: keep it as a parse_error part. It rides the same
                # preservation paths as any other part (assistant → content_block,
                # user/other → a raw block) so the corruption stays visible.
                parsed = {
                    "type": "parse_error",
                    "raw_text": _opencode_raw_text(data),
                    "error": str(e),
                    "_opencode_parse_error": True,
                }
            parts_by_message.setdefault(message_id, []).append(parsed)
    finally:
        conn.close()

    summary = OpenCodeDbScanResult(0, 0, 0)
    for session_id, session_data in sessions.items():
        summary.sessions_processed += 1
        try:
            result = import_opencode_from_payload(
                session_id=session_id,
                session_data=session_data,
                messages=messages_by_session.get(session_id, []),
                parts_by_message=parts_by_message,
            )
            if result.events_created > 0:
                summary.sessions_imported += 1
                summary.events_created += result.events_created
        except Exception as e:  # noqa: BLE001 — one bad session must not stop the scan
            # Counted and carried out to the watcher, not just logged: a scan that
            # reports only "nothing new" makes a failed session indistinguishable from
            # an unchanged one, and `archive status` stays green while content is
            # missing. The session's watermark never advanced, so it retries.
            logger.exception("import_opencode_db: session %s failed; skipping", session_id[:12])
            summary.sessions_failed += 1
            summary.errors.append(f"session {session_id[:12]}: {e}")
    return summary


def import_opencode_from_payload(
    *,
    session_id: str,
    session_data: dict[str, Any],
    messages: list[tuple[str, dict[str, Any]]],
    parts_by_message: dict[str, list[dict[str, Any]]],
    session=None,
) -> OpenCodeImportResult:
    """Import one OpenCode session (atomic per-session transaction when no session)."""
    if session is not None:
        return _run_opencode(session, session_id, session_data, messages, parts_by_message)
    with get_session() as s:
        result = _run_opencode(s, session_id, session_data, messages, parts_by_message)
        s.commit()
        return result


def _run_opencode(session, session_id, session_data, messages, parts_by_message) -> OpenCodeImportResult:
    source_id = session_id
    import_state = get_import_state(session, "opencode", source_id)

    if _opencode_session_unchanged(import_state, session_data):
        return OpenCodeImportResult(0, (import_state.thread_id or 0) if import_state else 0, False)

    norm = _build_opencode_messages(messages, parts_by_message, session_data=session_data)
    if not norm:
        return OpenCodeImportResult(0, 0, False)

    thread_id, is_new_thread = _opencode_resolve_thread(session, import_state, session_id, source_id, session_data)

    start_index = import_state.last_line_count if import_state else 0
    new_messages = norm[start_index:]
    if not new_messages:
        return OpenCodeImportResult(0, thread_id, is_new_thread)

    base_ts = _parse_opencode_timestamp(session_data.get("time_created")) or datetime.now(timezone.utc)
    normalized = [_opencode_to_normalized(m) for m in new_messages]
    # cross_pass_dedup: see cursor.py — a full re-scan must not re-stack duplicate
    # events against NULL-dedup_key backfill rows (the Postgres-seeded March 2026
    # backfill). The message-level content+timestamp check skips already-present
    # turns regardless of dedup_key.
    events_created, _ = assemble_events(
        session, thread_id, normalized, DefaultEventBuilder(),
        base_prev_ts=base_ts, cross_pass_dedup=True,
    )
    upsert_import_state(
        session,
        source="opencode",
        source_id=source_id,
        thread_id=thread_id,
        last_line_count=len(norm),
        last_file_size=0,
        last_message_uuid=norm[-1].get("id") if norm else None,
    )
    return OpenCodeImportResult(events_created, thread_id, is_new_thread)


def _opencode_session_unchanged(import_state: Optional[ImportState], session_data: dict[str, Any]) -> bool:
    if not (import_state and import_state.last_import_at):
        return False
    time_updated_ms = session_data.get("time_updated") or 0
    last_import_ms = import_state.last_import_at.timestamp() * 1000
    return time_updated_ms <= last_import_ms


def _opencode_resolve_thread(session, import_state, session_id, source_id, session_data) -> tuple[int, bool]:
    if import_state and import_state.thread_id:
        return import_state.thread_id, False
    existing = get_thread_by_source(session, "opencode", source_id)
    if existing and existing.id is not None:
        return existing.id, False

    title = session_data.get("title") or "OpenCode Session"
    if len(title) > 100:
        title = title[:97] + "..."
    source_metadata = {
        k: v
        for k, v in {
            "provider": "opencode",
            "opencode_session_id": session_id,
            "directory": session_data.get("directory"),
            "agent": session_data.get("agent"),
            "parent_id": session_data.get("parent_id"),
        }.items()
        if v is not None
    }
    thread_id = create_thread(
        session,
        source="opencode",
        source_id=source_id,
        title=title,
        source_metadata=source_metadata,
    )
    return thread_id, True


# A session untouched for this long still carrying an unsettled assistant turn is
# treated as abandoned/crashed rather than live-streaming: its partial content is
# imported once (tagged incomplete) instead of stranding it + everything after it.
# Conservatively large so a genuinely live turn is never misread as abandoned.
_OPENCODE_ABANDONED_MS = 24 * 60 * 60 * 1000


def _build_opencode_messages(
    messages: list[tuple[str, dict[str, Any]]],
    parts_by_message: dict[str, list[dict[str, Any]]],
    session_data: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Assemble messages + parts into the settled-prefix normalized shape."""
    norm: list[dict[str, Any]] = []
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    session_updated_ms = (session_data or {}).get("time_updated")

    for msg_id, data in messages:
        if not isinstance(data, dict):
            continue

        # A message row whose JSON failed to parse was stubbed at read time —
        # preserve it as a visible parse_error record instead of dropping it.
        if data.get("_opencode_parse_error") is not None:
            norm.append({
                "id": msg_id,
                "role": "parse_error",
                "error": data.get("_opencode_parse_error"),
                "raw": data.get("_raw_data"),
            })
            continue

        role = data.get("role")
        mtime = data.get("time") or {}
        parts = parts_by_message.get(msg_id, [])

        if role == "user":
            text = "\n".join(
                p.get("text", "") for p in parts if p.get("type") == "text" and p.get("text")
            )
            norm.append({"id": msg_id, "role": "user", "created_at": mtime.get("created"), "content": text})
            # Non-text user parts (files/attachments/images) must not be discarded. Keep
            # them as a sibling turn under an unmodeled role so the builder's generic
            # `message` path preserves each raw part verbatim in content_blocks.
            other_parts = [p for p in parts if isinstance(p, dict) and p.get("type") != "text"]
            if other_parts:
                norm.append({
                    "id": f"{msg_id}#parts",
                    "role": "user_attachment",
                    "created_at": mtime.get("created"),
                    "parts": other_parts,
                })
            continue

        if role != "assistant":
            # Any other role (opencode is normally user/assistant only; this is the
            # forward-compat catch). Preserve it verbatim rather than dropping — text
            # as content, every part as a raw block.
            text = "\n".join(
                p.get("text", "") for p in parts if p.get("type") == "text" and p.get("text")
            )
            norm.append({
                "id": msg_id,
                "role": role or "unknown",
                "created_at": mtime.get("created"),
                "content": text,
                "parts": [p for p in parts if isinstance(p, dict)],
            })
            continue

        # assistant — hold back until the turn has settled (the settled prefix).
        if mtime.get("completed") is None:
            # A live in-progress turn settles on a later poll, so break and wait —
            # importing its churning partial now would stack a fresh event every poll
            # (dedup_key tracks content). But an abandoned/crashed turn never gets
            # `completed`; breaking there strands it + everything after it forever.
            # If the session has gone stale, treat the turn as abandoned: keep its
            # already-present content (tagged incomplete) and walk on. The stale
            # session is re-imported at most once (time_updated cursor), so this
            # partial lands exactly once — no churn.
            if _opencode_session_stale(session_updated_ms, now_ms):
                norm.append({
                    "id": msg_id,
                    "role": "assistant",
                    "started_at": mtime.get("created"),
                    "completed_at": None,
                    "model": _opencode_model(data),
                    "segments": _opencode_assistant_segments(parts),
                    "incomplete": True,
                })
                continue
            break

        norm.append({
            "id": msg_id,
            "role": "assistant",
            "started_at": mtime.get("created"),
            "completed_at": mtime.get("completed"),
            "model": _opencode_model(data),
            "segments": _opencode_assistant_segments(parts),
        })

    return norm


def _opencode_session_stale(session_updated_ms: Any, now_ms: float) -> bool:
    """True when a session hasn't been touched for ``_OPENCODE_ABANDONED_MS`` — a
    still-unsettled turn on such a session is abandoned, not live. A missing
    ``time_updated`` is treated as *not* stale (conservative: keep holding rather
    than risk churning a live turn we can't date)."""
    if not isinstance(session_updated_ms, (int, float)):
        return False
    return (now_ms - session_updated_ms) > _OPENCODE_ABANDONED_MS


def _opencode_raw_text(data: Any) -> str:
    """The raw stored blob for a corrupt row, as text and never truncated."""
    return data if isinstance(data, str) else repr(data)


def _opencode_text_segment(kind: str, p: dict[str, Any]) -> Optional[dict[str, Any]]:
    txt = p.get("text", "")
    if not txt:
        return None
    ptime = p.get("time") or {}
    return {"kind": kind, "text": txt, "ts": ptime.get("start")}


def _opencode_tool_segment(p: dict[str, Any]) -> dict[str, Any]:
    state = p.get("state") or {}
    stime = state.get("time") or {}
    return {
        "kind": "tool",
        "call_id": p.get("callID") or "",
        "name": p.get("tool") or "unknown",
        "input": state.get("input") or {},
        "output": _opencode_stringify(state.get("output")),
        "is_error": state.get("status") == "error",
        "ts": stime.get("start"),
        "end_ts": stime.get("end"),
    }


def _opencode_assistant_segments(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for p in parts:
        ptype = p.get("type")
        if ptype == "reasoning":
            seg = _opencode_text_segment("thinking", p)
        elif ptype == "text":
            seg = _opencode_text_segment("text", p)
        elif ptype == "tool":
            seg = _opencode_tool_segment(p)
        else:
            # An assistant part type we don't model (file, step-start/finish,
            # snapshot, a parse_error stub, …). Keep it as a raw segment carrying the
            # part + its type; `_opencode_to_normalized` turns it into an unmodeled
            # content block, which the builder preserves as a `content_block` event.
            ptime = p.get("time") or {}
            seg = {"kind": "raw", "part_type": ptype, "raw": p, "ts": ptime.get("start")}
        if seg is not None:
            segments.append(seg)
    return segments


def _opencode_model(data: dict[str, Any]) -> str:
    model_id = data.get("modelID")
    provider = data.get("providerID")
    if model_id and provider:
        return f"{provider}/{model_id}"
    return model_id or "opencode"


def _opencode_stringify(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    return json.dumps(output, default=str)


def _opencode_iso(ms: Any) -> Optional[str]:
    dt = _parse_opencode_timestamp(ms)
    return dt.isoformat() if dt else None


def _opencode_to_normalized(msg: dict[str, Any]) -> dict[str, Any]:
    """Map one settled OpenCode message into the canonical NormalizedMessage shape."""
    role = msg.get("role", "")
    if role == "user":
        return {
            "role": "user",
            "created_at": _opencode_iso(msg.get("created_at")),
            "content_text": msg.get("content", ""),
            "content_blocks": [],
            "provider_message_id": msg.get("id", ""),
            "provider_data": {"provider": "opencode", "role": "user"},
        }

    if role == "parse_error":
        # A corrupt message row — surface it as a system parse_error, mirroring the
        # shared parse_error convention. The builder's system path routes an
        # unrecognized first block to a context_summary event, so the raw text +
        # error survive (raw_text in provider_data, error in the content).
        raw = msg.get("raw")
        err = msg.get("error")
        return {
            "role": "system",
            "created_at": None,
            "content_text": f"[OpenCode message parse error]: {err}",
            "content_blocks": [{"type": "parse_error", "raw_text": raw, "error": err, "seq": 0}],
            "provider_message_id": msg.get("id", ""),
            "provider_data": {
                "provider": "opencode", "parse_error": True, "raw_text": raw, "error": err,
            },
        }

    if role != "assistant":
        # An unmodeled role (opencode's non-text user parts land here as
        # `user_attachment`; any other message role too). The builder's generic
        # `message` path keeps content_blocks verbatim; we also stash the raw parts in
        # provider_data so nothing is lost even on the lossier system path.
        blocks: list[dict[str, Any]] = [p for p in msg.get("parts", []) if isinstance(p, dict)]
        return {
            "role": role or "unknown",
            "created_at": _opencode_iso(msg.get("created_at")),
            "content_text": msg.get("content", ""),
            "content_blocks": blocks,
            "provider_message_id": msg.get("id", ""),
            "provider_data": {"provider": "opencode", "role": role, "opencode_parts": blocks},
        }

    blocks = []
    for seg in msg.get("segments", []):
        kind = seg.get("kind")
        ts = _opencode_iso(seg.get("ts"))
        if kind == "thinking":
            blocks.append({"type": "thinking", "text": seg.get("text", ""), "start_timestamp": ts})
        elif kind == "text":
            blocks.append({"type": "text", "text": seg.get("text", ""), "start_timestamp": ts})
        elif kind == "tool":
            call_id = seg.get("call_id") or ""
            name = seg.get("name") or "unknown"
            blocks.append({
                "type": "tool_use", "id": call_id, "name": name,
                "input": seg.get("input") or {}, "start_timestamp": ts,
            })
            blocks.append({
                "type": "tool_result", "tool_use_id": call_id, "name": name,
                "content": seg.get("output", ""), "is_error": bool(seg.get("is_error")),
                "start_timestamp": _opencode_iso(seg.get("end_ts")) or ts,
            })
        elif kind == "raw":
            # An unmodeled assistant part — emit an unmodeled content block so the
            # builder preserves it verbatim as a `content_block` event (raw under
            # `data`). The part type stays queryable as the block type.
            blocks.append({
                "type": seg.get("part_type") or "opencode_part",
                "opencode_part_type": seg.get("part_type"),
                "opencode_part": seg.get("raw"),
                "start_timestamp": ts,
            })
    provider_data = {"provider": "opencode", "model": msg.get("model") or "opencode"}
    if msg.get("incomplete"):
        # Abandoned/unsettled turn preserved via the staleness gate — tag it so it's
        # distinguishable from a normally-completed turn. stop_reason isn't part of
        # the dedup content, so a later settled version is still a distinct event.
        provider_data["stop_reason"] = "incomplete"
    return {
        "role": "assistant",
        "created_at": _opencode_iso(msg.get("started_at")),
        "content_text": "",
        "content_blocks": blocks,
        "provider_message_id": msg.get("id", ""),
        "provider_data": provider_data,
    }


def _parse_opencode_timestamp(ts: Any) -> Optional[datetime]:
    """OpenCode timestamps are integer milliseconds since epoch."""
    if not isinstance(ts, (int, float)):
        return None
    try:
        seconds = ts / 1000 if ts > 1e12 else ts
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (ValueError, OSError):
        return None
