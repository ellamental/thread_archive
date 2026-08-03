"""Cursor (`state.vscdb`) DB scan + per-composer import + assembler.

A single live SQLite DB holds many conversations (composers + their bubbles). We
open it read-only, extract every composer, and run the per-composer importer
(incremental skip via the import_state cursor on ``lastUpdatedAt``). Pure
``_cursor_*`` / ``_build_cursor_messages`` helpers copied verbatim; orchestration
rewired onto our store + ``assemble_events``.

**The scan pays for what moved, not for what exists.** ``state.vscdb`` is the
editor's whole key-value store, not a conversation file: it is hundreds of
megabytes, the bubbles (message bodies) are the bulk of it, and Cursor rewrites
some key in it constantly — so the watcher's mtime fingerprint advances many times
an hour whether or not a conversation did. Two properties keep a no-op scan from
costing the size of the store:

* **Key ranges, never ``LIKE``.** ``key LIKE 'prefix%'`` is opaque to SQLite's
  planner and degrades to a full table scan; the half-open range
  :func:`_prefix_range` builds is an index seek on the key's unique index.
* **Bubbles are read per stale composer.** ``lastUpdatedAt`` lives in the composer
  blob, which is small, so the set of conversations that actually moved is known
  before a single message body is read — and on the common pass that set is empty.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.timestamps import parse_timestamp_iso

from .._store import ImportState, get_session
from . import _probe
from ._events import assemble_events
from ._result import DbScanResult, DbUnitImportResult
from ._state import (
    create_thread,
    get_import_state,
    get_import_states,
    get_thread_by_source,
    store_write_settled,
    upsert_import_state,
)

logger = logging.getLogger(__name__)


def _prefix_range(prefix: str) -> tuple[str, str]:
    """``(lo, hi)`` bounding every key starting with ``prefix``, half-open.

    ``hi`` increments the prefix's last character, which is the successor of every
    string the prefix can begin — so ``lo <= key < hi`` selects exactly the prefix's
    keys and SQLite serves it from the key index instead of scanning the table."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def import_cursor_db(db_path) -> DbScanResult:
    """Open a Cursor ``state.vscdb`` and import every composer whose conversation moved.

    Every composer counts as ``processed`` — the scan looked at all of them, and the
    watcher's "checked" figure means what it says. Only the ones past their watermark
    cost a bubble read."""
    import sqlite3

    db_path = Path(db_path)
    composers: dict[str, dict[str, Any]] = {}
    bubbles: dict[str, dict[str, Any]] = {}

    # Cursor's state.vscdb is live — open read-only (never lock a DB the editor
    # owns) with a busy timeout.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
        ).fetchone():
            return DbScanResult()

        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
            _prefix_range("composerData:"),
        ):
            composer_id = key.replace("composerData:", "")
            try:
                composers[composer_id] = json.loads(value)
            except (json.JSONDecodeError, TypeError) as e:
                # A corrupt composer blob must not vanish on a silent `continue`.
                # Log it and preserve a stub thread carrying the raw + error so a
                # failed conversation is visible in the archive, not silently lost.
                logger.warning(
                    "import_cursor_db: composer %s failed to parse; preserving stub: %s",
                    composer_id[:8], e,
                )
                try:
                    _import_cursor_error_stub(composer_id, value, {}, e)
                except Exception:
                    logger.exception(
                        "import_cursor_db: composer %s parse-stub failed", composer_id[:8]
                    )
                continue

        # Which conversations moved — decided off the composer blobs alone, before
        # any message body is read. A pass where nothing moved reads no bubbles.
        stale = _cursor_stale_composers(composers)
        for composer_id in stale:
            for key, value in conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
                _prefix_range(f"bubbleId:{composer_id}:"),
            ):
                parts = key.split(":")
                if len(parts) < 3:
                    logger.warning("import_cursor_db: malformed bubble key %r; skipping", key)
                    continue
                bubble_id = parts[2]
                try:
                    data = json.loads(value)
                    data["_composerId"] = composer_id
                except (json.JSONDecodeError, TypeError) as e:
                    # A corrupt bubble must not vanish on a silent `continue`. Keep a raw
                    # stub: if a header references it, the unknown-bubble path
                    # (_cursor_to_normalized) preserves it as a `message` event.
                    logger.warning(
                        "import_cursor_db: bubble %s failed to parse; keeping raw stub: %s", key, e
                    )
                    data = {
                        "_composerId": composer_id,
                        "_parse_error": str(e),
                        "_raw": value,
                        "type": None,
                    }
                bubbles[f"{composer_id}:{bubble_id}"] = data
    finally:
        conn.close()

    summary = DbScanResult()
    for composer_id, composer_data in composers.items():
        summary.processed += 1
        if composer_id not in stale:
            continue
        composer_bubbles = {
            k: v for k, v in bubbles.items() if k.startswith(f"{composer_id}:")
        }
        try:
            result = import_cursor_from_payload(
                composer_id=composer_id, composer_data=composer_data, bubbles=composer_bubbles
            )
            if result.events_created > 0:
                summary.imported += 1
                summary.events_created += result.events_created
        except Exception as e:
            # One composer blowing up must not skip it silently. Log the traceback,
            # count it out to the watcher's health, AND preserve a stub thread carrying
            # its id + raw + error so the failed conversation stays visible in the
            # archive and re-exportable.
            logger.exception(
                "import_cursor_db: composer %s failed; preserving stub", composer_id[:8]
            )
            summary.failed += 1
            summary.errors.append(f"composer {composer_id[:8]}: {e}")
            try:
                _import_cursor_error_stub(composer_id, composer_data, composer_bubbles, e)
            except Exception:
                logger.exception(
                    "import_cursor_db: composer %s stub also failed", composer_id[:8]
                )
    return summary


def _import_cursor_error_stub(
    composer_id: str, composer_data: Any, bubbles: dict[str, Any], error: Exception
) -> None:
    """Preserve a composer that failed to load/import as its own stub thread.

    A corrupt (unparseable JSON) or blow-up composer must not vanish behind a
    silent ``continue`` on the load or a logged-and-dropped ``skipping`` on the
    import. Keep a stub thread carrying the raw payload + error under a distinct
    ``:import-error`` source id, so the failure is visible in the archive and
    re-exportable while the real composer stays unimported and retryable on the
    next scan (its ``import_state`` watermark never advanced). Idempotent via the
    builder's dedup_key.
    """
    stub_source_id = f"{composer_id}:import-error"
    raw: dict[str, Any] = {
        "composer_id": composer_id,
        "error": str(error),
        "composer_data": composer_data,
        "bubble_keys": sorted(bubbles.keys()),
    }
    message = {
        "role": "cursor_import_error",
        "created_at": None,
        "content_text": f"[cursor import failed for {composer_id}: {error}]",
        "content_blocks": [{"type": "cursor_raw", "data": raw}],
        "provider_message_id": stub_source_id,
        "provider_data": {"provider": "cursor", "role": "unknown", "import_error": str(error)},
    }
    with get_session() as s:
        existing = get_thread_by_source(s, "cursor", stub_source_id)
        if existing and existing.id is not None:
            thread_id = existing.id
        else:
            name = composer_data.get("name") if isinstance(composer_data, dict) else None
            title = f"{name or 'Cursor Conversation'} (import error)"
            if len(title) > 100:
                title = title[:97] + "..."
            thread_id = create_thread(s, source="cursor", source_id=stub_source_id, title=title)
        assemble_events(s, thread_id, [message], DefaultEventBuilder())
        s.commit()


def import_cursor_from_payload(
    *, composer_id: str, composer_data: dict[str, Any], bubbles: dict[str, Any], session=None
) -> DbUnitImportResult:
    """Import one Cursor composer (atomic per-composer transaction when no session)."""
    _probe.count("items")
    if session is not None:
        return _run_cursor(session, composer_id, composer_data, bubbles)
    with get_session() as s:
        result = _run_cursor(s, composer_id, composer_data, bubbles)
        with _probe.timed("commit_ms"):
            s.commit()
        return result


def _run_cursor(session, composer_id, composer_data, bubbles) -> DbUnitImportResult:
    source_id = composer_id
    import_state = get_import_state(session, "cursor", source_id)

    if _cursor_composer_unchanged(import_state, composer_data):
        return DbUnitImportResult(0, (import_state.thread_id or "") if import_state else "", False)

    messages = _build_cursor_messages(composer_id, composer_data, bubbles)
    if not messages:
        return DbUnitImportResult(0, "", False)

    thread_id, is_new_thread = _cursor_resolve_thread(session, import_state, source_id, composer_data)

    start_index = import_state.last_line_count if import_state else 0
    new_messages = messages[start_index:]
    if not new_messages:
        return DbUnitImportResult(0, thread_id, is_new_thread)

    normalized = [_cursor_to_normalized(m) for m in new_messages]
    # cross_pass_dedup: a full re-scan of an already-imported composer (import_state
    # reset, or a manual re-run) must be a no-op even against rows the dedup_key
    # can't see — a bulk-seeded row can carry a NULL dedup_key, so keying only on it
    # would let a re-import stack duplicate events. The message-level (content +
    # timestamp) existence check skips any turn already present regardless of
    # dedup_key, mirroring the claude-code continuation guard.
    events_created, _ = assemble_events(
        session, thread_id, normalized, DefaultEventBuilder(), cross_pass_dedup=True
    )
    upsert_import_state(
        session,
        source="cursor",
        source_id=source_id,
        thread_id=thread_id,
        last_line_count=len(messages),
        last_file_size=0,
        last_message_uuid=messages[-1].get("id") if messages else None,
    )
    return DbUnitImportResult(events_created, thread_id, is_new_thread)


def _cursor_composer_unchanged(import_state: Optional[ImportState], composer_data: dict[str, Any]) -> bool:
    return store_write_settled(import_state, composer_data.get("lastUpdatedAt", 0))


def _cursor_stale_composers(composers: dict[str, dict[str, Any]]) -> set[str]:
    """Of ``composers``, the ids whose conversation is past its watermark.

    The same predicate :func:`_cursor_composer_unchanged` applies per composer inside
    the import, hoisted to the whole scan so it can gate the bubble read. Deciding it
    twice is deliberate: this set is an optimization the importer must not depend on,
    and the inner check stays the authority for a composer imported by any other path.

    A store that cannot be read yields every composer — the scan then behaves exactly
    as it would with no watermarks at all, which is slow but never silently skips a
    conversation."""
    try:
        with get_session() as session:
            states = get_import_states(session, "cursor")
    except Exception:  # noqa: BLE001 — an unreadable watermark must not skip an import
        logger.warning("import_cursor_db: could not read cursor watermarks", exc_info=True)
        return set(composers)
    return {
        composer_id
        for composer_id, composer_data in composers.items()
        if not _cursor_composer_unchanged(states.get(composer_id), composer_data)
    }


def _cursor_resolve_thread(session, import_state, source_id, composer_data) -> tuple[str, bool]:
    if import_state and import_state.thread_id:
        return import_state.thread_id, False
    existing = get_thread_by_source(session, "cursor", source_id)
    if existing and existing.id is not None:
        return existing.id, False
    title = composer_data.get("name") or "Cursor Conversation"
    if len(title) > 100:
        title = title[:97] + "..."
    thread_id = create_thread(session, source="cursor", source_id=source_id, title=title)
    return thread_id, True


def _build_cursor_messages(
    composer_id: str, composer_data: dict[str, Any], all_bubbles: dict[str, Any]
) -> list[dict[str, Any]]:
    """Assemble Cursor composer + bubbles into the normalized message shape."""
    messages: list[dict[str, Any]] = []
    headers = composer_data.get("fullConversationHeadersOnly", [])
    if not headers:
        return messages

    for idx, header in enumerate(headers):
        if not isinstance(header, dict):
            continue
        bubble_id = header.get("bubbleId")
        if not bubble_id:
            continue
        bubble = all_bubbles.get(f"{composer_id}:{bubble_id}", {})
        bubble_type = bubble.get("type") or header.get("type")
        if bubble_type == 1:
            role = "user"
        elif bubble_type == 2:
            role = "assistant"
        else:
            role = "unknown"

        message: dict[str, Any] = {
            "id": bubble_id,
            "role": role,
            "content": bubble.get("text", ""),
            "created_at": bubble.get("createdAt"),
            "index": idx,
        }

        # Per-bubble extras Cursor records alongside the text. Token counts feed
        # the api_request_completed usage fields; the model name replaces the
        # "cursor" placeholder; usageUuid joins to Cursor's usage table; attached
        # code chunks are the file excerpts shown to the model with the turn.
        token_count = bubble.get("tokenCount")
        if isinstance(token_count, dict):
            usage = {
                "input_tokens": token_count.get("inputTokens", 0),
                "output_tokens": token_count.get("outputTokens", 0),
            }
            if any(usage.values()):
                message["usage"] = usage
        model_info = bubble.get("modelInfo")
        if isinstance(model_info, dict) and model_info.get("modelName"):
            message["model"] = model_info["modelName"]
        if bubble.get("usageUuid"):
            message["usage_uuid"] = bubble["usageUuid"]
        attached = _cursor_attached_code(bubble)
        if attached:
            message["attached_code"] = attached
        # An unmodeled bubble type keeps its raw payload + resolved type so the
        # normalized mapping can preserve the whole turn (content/thinking/tool/raw)
        # rather than drop it — see _cursor_to_normalized's unknown branch.
        if role == "unknown":
            message["bubble_type"] = bubble_type
            if bubble:
                message["raw"] = bubble

        thinking = bubble.get("thinking")
        if thinking and isinstance(thinking, dict):
            thinking_text = thinking.get("text", "")
            if thinking_text:
                message["thinking"] = thinking_text

        tool_data = bubble.get("toolFormerData")
        if tool_data and isinstance(tool_data, dict):
            # ``params`` is Cursor's resolved arg dict; ``rawArgs`` the model's raw
            # JSON-string args. Prefer params, fall back to parsed rawArgs. ``result``
            # is the tool output. Capturing input+result lets us emit the tool_use +
            # tool_execution events the builder produces — dropping them would lose
            # every tool call on a re-parse.
            tool_input = tool_data.get("params")
            if tool_input is None:
                raw = tool_data.get("rawArgs")
                if isinstance(raw, str):
                    try:
                        tool_input = json.loads(raw)
                    except json.JSONDecodeError:
                        tool_input = raw
                else:
                    tool_input = raw
            message["tool_call"] = {
                "name": tool_data.get("name", "unknown"),
                "tool_id": tool_data.get("tool"),
                "call_id": tool_data.get("toolCallId"),
                "status": tool_data.get("status"),
                "input": tool_input,
                "result": tool_data.get("result"),
            }
        messages.append(message)

        # A compaction summary rides on the bubble whose turn triggered it. Emit
        # it as its own system message so the builder lands a context_summary
        # event, mirroring the other providers' compaction handling.
        summary_raw = bubble.get("cachedConversationSummary") or bubble.get("conversationSummary")
        summary_text = _cursor_summary_text(summary_raw)
        if summary_text:
            messages.append({
                "id": f"{bubble_id}:summary",
                "role": "system",
                "content": summary_text,
                "created_at": bubble.get("createdAt"),
                "index": idx,
                "summary_type": "cursor_compaction",
            })
    return messages


def _cursor_summary_text(raw: Any) -> str:
    """The compaction summary text out of Cursor's ``(cached)conversationSummary``.

    The field arrives either as a JSON-encoded string (``'{"summary": "..."}'``),
    a plain dict, or bare text; all reduce to the summary string."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw.strip()
        raw = parsed
    if isinstance(raw, dict):
        text = raw.get("summary")
        if isinstance(text, str):
            return text.strip()
        return json.dumps(raw, ensure_ascii=False)
    return str(raw).strip()


def _cursor_attached_code(bubble: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact the code chunks Cursor attached to a turn (path + line range +
    text when present). ``attachedFileCodeChunksMetadataOnly`` entries carry no
    text by design and are marked ``metadata_only``."""
    chunks: list[dict[str, Any]] = []
    for key in ("attachedCodeChunks", "attachedFileCodeChunksMetadataOnly"):
        for chunk in bubble.get(key) or []:
            if not isinstance(chunk, dict):
                continue
            entry: dict[str, Any] = {"path": chunk.get("relativeWorkspacePath")}
            if chunk.get("startLineNumber") is not None:
                entry["start_line"] = chunk["startLineNumber"]
            lines = chunk.get("lines")
            if lines:
                entry["text"] = "\n".join(lines) if isinstance(lines, list) else str(lines)
            if key == "attachedFileCodeChunksMetadataOnly":
                entry["metadata_only"] = True
            chunks.append(entry)
    return chunks


def _cursor_annotations(msg: dict[str, Any]) -> dict[str, Any]:
    """Message-level annotations (see the event builder's annotations convention):
    data about the turn that must never perturb its dedup identity."""
    annotations: dict[str, Any] = {}
    if msg.get("usage_uuid"):
        annotations["usage_uuid"] = msg["usage_uuid"]
    if msg.get("attached_code"):
        annotations["attached_code"] = msg["attached_code"]
    return annotations


def _cursor_iso(ts: Any) -> Optional[str]:
    # Cursor stores either epoch-ms numbers or ISO strings; both normalize to the
    # canonical aware-UTC ISO form (the ms/1e12 threshold and offset-less→UTC
    # coercion live in parse_timestamp).
    return parse_timestamp_iso(ts)


def _cursor_tool_blocks(tc: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_use (+ tool_result) blocks for a Cursor ``tool_call``. Shared by the
    assistant and unknown-bubble mappings so a tool call is preserved either way."""
    call_id = tc.get("call_id")
    name = tc.get("name", "unknown")
    blocks: list[dict[str, Any]] = [{
        "type": "tool_use",
        "tool_call_id": call_id,
        "name": name,
        "input": tc.get("input") or {},
    }]
    result = tc.get("result")
    if result is not None:
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        blocks.append({
            "type": "tool_result",
            "tool_use_id": call_id,
            "name": name,
            "content": content,
            "is_error": tc.get("status") == "error",
        })
    return blocks


def _cursor_to_normalized(msg: dict[str, Any]) -> dict[str, Any]:
    """Map one Cursor message into the canonical NormalizedMessage shape."""
    role = msg.get("role", "")
    created_at = _cursor_iso(msg.get("created_at"))
    pmid = msg.get("id", "")
    if role == "user":
        provider_data: dict[str, Any] = {"provider": "cursor", "role": "user"}
        annotations = _cursor_annotations(msg)
        # A user bubble's token count has no api_request_completed to land on;
        # keep it as an annotation rather than dropping it.
        if msg.get("usage"):
            annotations["usage"] = msg["usage"]
        if annotations:
            provider_data["annotations"] = annotations
        return {
            "role": "user",
            "created_at": created_at,
            "content_text": msg.get("content", ""),
            "content_blocks": [],
            "provider_message_id": pmid,
            "provider_data": provider_data,
        }
    if role == "assistant":
        blocks: list[dict[str, Any]] = []
        if msg.get("thinking"):
            blocks.append({"type": "thinking", "text": msg["thinking"]})
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        tc = msg.get("tool_call")
        if tc and isinstance(tc, dict):
            blocks.extend(_cursor_tool_blocks(tc))
        provider_data = {"provider": "cursor", "model": msg.get("model") or "cursor"}
        if msg.get("usage"):
            provider_data["usage"] = msg["usage"]
        annotations = _cursor_annotations(msg)
        if annotations:
            provider_data["annotations"] = annotations
        return {
            "role": "assistant",
            "created_at": created_at,
            "content_text": "",
            "content_blocks": blocks,
            "provider_message_id": pmid,
            "provider_data": provider_data,
        }
    if role == "system":
        # A compaction summary synthesized in _build_cursor_messages: a system
        # message with a context_summary block becomes a context_summary event.
        text = msg.get("content", "")
        return {
            "role": "system",
            "created_at": created_at,
            "content_text": text,
            "content_blocks": [{"type": "context_summary", "text": text}],
            "provider_message_id": pmid,
            "provider_data": {
                "provider": "cursor",
                "summary_type": msg.get("summary_type", "cursor_compaction"),
            },
        }
    # An unmodeled bubble type. The shared builder preserves any non-user/assistant/
    # system role as a `message` event (role + content + content_blocks), so carry the
    # bubble's content/thinking/tool_call + full raw here rather than dropping the turn.
    # A bare {"role": role} would discard every one.
    role = role or "unknown"
    unknown_blocks: list[dict[str, Any]] = []
    if msg.get("thinking"):
        unknown_blocks.append({"type": "thinking", "text": msg["thinking"]})
    tc = msg.get("tool_call")
    if tc and isinstance(tc, dict):
        unknown_blocks.extend(_cursor_tool_blocks(tc))
    provider_data = {"provider": "cursor", "role": role}
    annotations = _cursor_annotations(msg)
    if msg.get("usage"):
        annotations["usage"] = msg["usage"]
    if annotations:
        provider_data["annotations"] = annotations
    raw = msg.get("raw")
    if raw:
        provider_data["raw"] = raw
        unknown_blocks.append(
            {"type": "cursor_raw", "bubble_type": msg.get("bubble_type"), "data": raw}
        )
    return {
        "role": role,
        "created_at": created_at,
        "content_text": msg.get("content", ""),
        "content_blocks": unknown_blocks,
        "provider_message_id": pmid,
        "provider_data": provider_data,
    }
