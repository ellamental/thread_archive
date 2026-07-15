"""Bulk provider-export import: claude.ai + ChatGPT + xAI (Grok) account exports.

A manual, downloaded **account export** is a different shape from the live CLI
stores the watcher tails — a ZIP (or unzipped batch directory) of *all* a user's
conversations. This module loads such a bundle and imports each conversation as its
own thread, reusing the shared parse → :func:`assemble_events` write path.

- **claude.ai** (Settings → Account → Export Data): a ZIP with ``conversations.json``
  (+ ``memories``/``projects``/``users``) at the root, or the newer
  ``data-…-batch-NNNN/`` directory form. Threads land as ``source='claude'``.
- **ChatGPT** (Settings → Data Controls → Export Data): a ZIP with
  ``conversations.json`` (+ ``chat.html``/``user.json``) at the root. Threads land
  as ``source='chatgpt'``. Both providers name the file ``conversations.json``, so
  :func:`classify_export` tells them apart by sibling files, falling back to the
  conversation shape itself (claude.ai: ``chat_messages``; ChatGPT: ``mapping``).
- **xAI/Grok** (Settings → Data Controls → Download Your Data): a tree with
  ``…/export_data/<uuid>/prod-grok-backend.json``. Threads land as ``source='grok'``
  (the conversation UUIDs are disjoint from the Grok-CLI session UUIDs).

The loaders below are pure file-shape readers; the vendored ``ClaudeParser`` /
``ChatGPTParser`` do the per-provider normalization.
"""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.parsers.chatgpt import ChatGPTParser
from thread_archive._thread_import.parsers.claude import ClaudeParser
from thread_archive._thread_import.timestamps import parse_timestamp

from .._store import get_session
from ._events import assemble_events
from ._state import (
    create_thread,
    discard_new_thread,
    get_thread_by_source,
    set_thread_models_from_events,
)

logger = logging.getLogger(__name__)


@dataclass
class ExportImportResult:
    processed: int = 0
    imported: int = 0
    skipped: int = 0
    events_created: int = 0
    # Conversations that raised mid-import and were preserved as a stub thread
    # (raw + error) rather than silently skipped — see _import_conversation_stub.
    errored: int = 0


# ── classification ───────────────────────────────────────────────────────────

_XAI_MARKER = "prod-grok-backend.json"

# claude.ai and ChatGPT exports BOTH ship a root ``conversations.json``, so the
# filename alone cannot classify — a ChatGPT export read as claude.ai matches
# zero conversations and looks like a successful empty import. Sibling files
# disambiguate cheaply (exact basenames: ChatGPT ships ``user.json``, claude.ai
# ``users.json``); when no marker is present, the conversation shape itself is
# the tiebreak (claude.ai: ``chat_messages``; ChatGPT: ``mapping``).
_CHATGPT_SIBLINGS = {"chat.html", "user.json", "message_feedback.json"}
_CLAUDE_SIBLINGS = {"users.json", "projects.json", "memories.json"}


def _sniff_conversations_shape(conversations: object) -> Optional[str]:
    """``'claude'`` | ``'chatgpt'`` | None from a parsed ``conversations.json``."""
    if isinstance(conversations, list):
        for conv in conversations:
            if isinstance(conv, dict):
                if "chat_messages" in conv:
                    return "claude"
                if "mapping" in conv:
                    return "chatgpt"
    return None


def _classify_conversations_json(basenames: set[str], read_conversations) -> Optional[str]:
    """Shared claude-vs-ChatGPT call for a bundle with a root ``conversations.json``:
    sibling markers first (no parse), else parse and sniff the conversation shape.
    ``read_conversations`` is a thunk returning the parsed JSON (or raising)."""
    if basenames & _CHATGPT_SIBLINGS:
        return "chatgpt"
    if basenames & _CLAUDE_SIBLINGS:
        return "claude"
    try:
        return _sniff_conversations_shape(read_conversations())
    except (OSError, ValueError, KeyError):
        return None


def classify_export(path: Path) -> Optional[str]:
    """``'claude'`` | ``'chatgpt'`` | ``'xai'`` | None for an export ZIP or directory."""
    path = Path(path)
    if path.is_dir():
        if (path / "conversations.json").exists():
            siblings = {e.name for e in path.iterdir() if e.is_file()}
            return _classify_conversations_json(
                siblings,
                lambda: json.loads((path / "conversations.json").read_text(encoding="utf-8")),
            )
        if any(True for _ in path.rglob(_XAI_MARKER)):
            return "xai"
        return None
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            if any(n.rsplit("/", 1)[-1] == _XAI_MARKER for n in names):
                return "xai"
            if "conversations.json" in names:
                basenames = {n.rsplit("/", 1)[-1] for n in names}
                return _classify_conversations_json(
                    basenames, lambda: json.loads(zf.read("conversations.json"))
                )
    return None


# ── claude.ai bundle loaders ─────────────────────────────────────────────────


def _load_claude_export(path: Path) -> dict:
    if path.is_dir():
        return _load_claude_export_from_dir(path)
    with zipfile.ZipFile(path, "r") as zf:
        conversations = json.loads(zf.read("conversations.json"))
        try:
            memories_raw = json.loads(zf.read("memories.json"))
            memories = memories_raw[0] if isinstance(memories_raw, list) and memories_raw else {}
        except (KeyError, json.JSONDecodeError):
            memories = {}
        try:
            projects = json.loads(zf.read("projects.json"))
        except (KeyError, json.JSONDecodeError):
            projects = []
        try:
            users = json.loads(zf.read("users.json"))
        except (KeyError, json.JSONDecodeError):
            users = []
    return {"conversations": conversations, "memories": memories, "projects": projects, "users": users}


def _load_claude_export_from_dir(root: Path) -> dict:
    conversations_path = root / "conversations.json"
    if not conversations_path.exists():
        raise FileNotFoundError(f"Batch directory missing conversations.json: {root}")
    conversations = json.loads(conversations_path.read_text(encoding="utf-8"))
    users_path = root / "users.json"
    users = json.loads(users_path.read_text(encoding="utf-8")) if users_path.exists() else []

    memories: dict = {}
    memories_path = root / "memories.json"
    if memories_path.exists():
        raw = json.loads(memories_path.read_text(encoding="utf-8"))
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            memories = raw[0]
        elif isinstance(raw, dict):
            memories = raw

    projects: list = []
    projects_dir = root / "projects"
    projects_file = root / "projects.json"
    if projects_dir.is_dir():
        for project_file in sorted(projects_dir.glob("*.json")):
            try:
                projects.append(json.loads(project_file.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("skipping malformed project file %s: %s", project_file, e)
    elif projects_file.exists():
        projects = json.loads(projects_file.read_text(encoding="utf-8"))

    return {"conversations": conversations, "memories": memories, "projects": projects, "users": users}


def import_claude_ai_export(
    path, *, force: bool = False, limit: Optional[int] = None
) -> ExportImportResult:
    """Import a claude.ai account export (ZIP or batch dir). Threads → source='claude'."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Export not found: {path}")

    bundle = _load_claude_export(path)
    parser = ClaudeParser()
    builder = DefaultEventBuilder()
    result = ExportImportResult()

    non_empty = [c for c in bundle["conversations"] if c.get("chat_messages")]
    non_empty.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "", reverse=True)
    if limit:
        non_empty = non_empty[:limit]
    result.processed = len(non_empty)

    for conv in non_empty:
        source_id = conv.get("uuid", "")
        title = conv.get("name") or "Untitled"
        single = {
            "conversations": [conv],
            "memories": bundle["memories"],
            "projects": bundle["projects"],
            "users": bundle["users"],
        }
        result = _import_one(
            result, parser_messages=lambda s=single: parser.parse_export(s),
            source="claude", source_id=source_id, title=title,
            source_metadata={"provider": "claude", "surface": "web"},
            builder=builder, force=force, raw=conv,
        )
    return result


# ── ChatGPT export loaders ───────────────────────────────────────────────────


def _load_chatgpt_export(path: Path) -> list:
    """The conversation list from a ChatGPT export (ZIP or unzipped directory)."""
    if path.is_dir():
        conversations_path = path / "conversations.json"
        if not conversations_path.exists():
            raise FileNotFoundError(f"ChatGPT export missing conversations.json: {path}")
        data = json.loads(conversations_path.read_text(encoding="utf-8"))
    else:
        with zipfile.ZipFile(path, "r") as zf:
            data = json.loads(zf.read("conversations.json"))
    return data if isinstance(data, list) else []


def _chatgpt_sort_ts(conv: dict) -> float:
    """Newest-first sort key: ``update_time`` / ``create_time`` are epoch floats in
    ChatGPT exports; anything unparseable sorts oldest rather than raising."""
    for key in ("update_time", "create_time"):
        v = conv.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def import_chatgpt_export(
    path, *, force: bool = False, limit: Optional[int] = None
) -> ExportImportResult:
    """Import a ChatGPT account export (ZIP or dir). Threads → source='chatgpt'."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Export not found: {path}")

    conversations = _load_chatgpt_export(path)
    parser = ChatGPTParser()
    builder = DefaultEventBuilder()
    result = ExportImportResult()

    non_empty = [c for c in conversations if isinstance(c, dict) and c.get("mapping")]
    non_empty.sort(key=_chatgpt_sort_ts, reverse=True)
    if limit:
        non_empty = non_empty[:limit]
    result.processed = len(non_empty)

    for conv in non_empty:
        source_id = conv.get("id") or conv.get("conversation_id") or ""
        title = conv.get("title") or "Untitled"
        result = _import_one(
            result, parser_messages=lambda c=conv: parser.parse_export([c]),
            source="chatgpt", source_id=source_id, title=title,
            source_metadata={"provider": "chatgpt", "surface": "web"},
            builder=builder, force=force, raw=conv,
        )
    return result


# ── xAI (Grok) export loaders ────────────────────────────────────────────────


def _load_xai_export(path: Path) -> list:
    conversations: list = []
    if path.is_dir():
        payloads = sorted(p for p in path.rglob(_XAI_MARKER) if p.is_file())
        if not payloads:
            raise FileNotFoundError(f"xAI export missing {_XAI_MARKER}: {path}")
        for payload in payloads:
            data = json.loads(payload.read_text(encoding="utf-8"))
            conversations.extend(data.get("conversations") or [])
        return conversations
    with zipfile.ZipFile(path, "r") as zf:
        names = sorted(n for n in zf.namelist() if n.rsplit("/", 1)[-1] == _XAI_MARKER)
        if not names:
            raise FileNotFoundError(f"xAI export ZIP missing {_XAI_MARKER}: {path}")
        for name in names:
            data = json.loads(zf.read(name))
            conversations.extend(data.get("conversations") or [])
    return conversations


def _xai_parse_time(v) -> Optional[datetime]:
    # xai/grok export ``create_time``: Mongo extended-JSON ``{$date: …}``, an
    # epoch-ms number (xai's own rule: ``>1e11`` means milliseconds), or an ISO
    # string. Each is reduced to seconds-or-string and canonicalized to aware-UTC
    # by parse_timestamp; after the ms→s divide the value is sub-1e12, so the
    # core's own ms heuristic never re-triggers on it.
    if isinstance(v, dict):
        inner = v.get("$date")
        if isinstance(inner, dict):
            inner = inner.get("$numberLong")
        if inner is None:
            return None
        try:
            return parse_timestamp(int(inner) / 1000)
        except (TypeError, ValueError):
            return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return parse_timestamp(v / 1000 if v > 1e11 else v)
    if isinstance(v, str):
        return parse_timestamp(v)
    return None


def _xai_conversation_messages(bundle: dict) -> list[dict]:
    conv = bundle.get("conversation") or {}
    fallback_ts = _xai_parse_time(conv.get("create_time")) or datetime.now(timezone.utc)
    responses = [r.get("response") or {} for r in bundle.get("responses", [])]
    responses.sort(key=lambda r: _xai_parse_time(r.get("create_time")) or fallback_ts)
    messages: list[dict] = []
    for resp in responses:
        text_str = (resp.get("message") or "").strip()
        sender = (resp.get("sender") or "").lower()
        created = _xai_parse_time(resp.get("create_time")) or fallback_ts
        provider_data: dict = {"provider": "grok"}
        if sender != "human" and resp.get("model"):
            provider_data["model"] = resp["model"]

        if text_str:
            role = "user" if sender == "human" else "assistant"
            content_text = text_str
            content_blocks: list[dict] = [{"type": "text", "text": text_str}]
        else:
            # A response with no message text still carries a real turn — an
            # image/attachment/generated-media or tool-only turn. Preserve it rather
            # than dropping it (the old `continue` lost every one). Route it through a
            # generic role so the shared builder keeps it as a `message` event with
            # the full raw response — the user-role builder discards a text-less turn,
            # so plain "user"/"assistant" wouldn't reliably survive. The sender + raw
            # ride along in provider_data too.
            role = f"grok_{sender or 'unknown'}"
            content_text = ""
            content_blocks = [{"type": "grok_raw", "data": resp}]
            provider_data["sender"] = sender or None
            provider_data["raw"] = resp
        messages.append({
            "role": role,
            "content_text": content_text,
            "content_blocks": content_blocks,
            "created_at": created.isoformat(),
            "provider_message_id": resp.get("_id", ""),
            "provider_data": provider_data,
        })
    return messages


def import_xai_export(
    path, *, force: bool = False, limit: Optional[int] = None
) -> ExportImportResult:
    """Import an xAI/Grok account export (ZIP or dir). Threads → source='grok'."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Export not found: {path}")

    conversations = _load_xai_export(path)
    builder = DefaultEventBuilder()
    result = ExportImportResult()

    non_empty = [c for c in conversations if c.get("responses")]
    non_empty.sort(
        key=lambda c: (c.get("conversation") or {}).get("modify_time")
        or (c.get("conversation") or {}).get("create_time") or "",
        reverse=True,
    )
    if limit:
        non_empty = non_empty[:limit]
    result.processed = len(non_empty)

    for bundle in non_empty:
        conv = bundle.get("conversation") or {}
        source_id = conv.get("id", "")
        title = conv.get("title") or "Untitled"
        result = _import_one(
            result, parser_messages=lambda b=bundle: _xai_conversation_messages(b),
            source="grok", source_id=source_id, title=title,
            source_metadata={"provider": "grok", "surface": "web"},
            builder=builder, force=force, raw=bundle,
        )
    return result


# ── shared per-conversation write path ───────────────────────────────────────


def _import_conversation_stub(
    source: str, source_id: str, title: str, source_metadata: dict,
    builder: DefaultEventBuilder, raw, error: Exception,
) -> None:
    """Preserve a conversation that failed to import as its own stub thread.

    Carries the raw payload + error under a distinct ``:import-error`` source id, so
    the failure is visible in the archive and re-exportable, while the real
    ``source_id`` stays unimported and retryable (a plain re-run without ``force``
    won't mistake the stub for a successful import). Idempotent via the builder's
    dedup_key.
    """
    stub_source_id = f"{source_id}:import-error"
    message = {
        "role": f"{source}_import_error",
        "created_at": None,
        "content_text": f"[{source} import failed for {source_id}: {error}]",
        "content_blocks": [{"type": "import_error", "error": str(error), "data": raw}],
        "provider_message_id": stub_source_id,
        "provider_data": {"provider": source, "import_error": str(error)},
    }
    with get_session() as s:
        existing = get_thread_by_source(s, source, stub_source_id)
        if existing and existing.id is not None:
            thread_id = existing.id
        else:
            stub_title = f"{title or 'Untitled'} (import error)"
            thread_id = create_thread(
                s, source=source, source_id=stub_source_id,
                title=stub_title[:100], source_metadata={**source_metadata, "import_error": True},
            )
        assemble_events(s, thread_id, [message], builder)
        s.commit()


def _import_one(
    result: ExportImportResult, *, parser_messages, source: str, source_id: str,
    title: str, source_metadata: dict, builder: DefaultEventBuilder, force: bool,
    raw=None,
) -> ExportImportResult:
    """Import one export conversation into its own thread (atomic per conversation)."""
    if not source_id:
        result.skipped += 1
        return result
    try:
        with get_session() as s:
            existing = get_thread_by_source(s, source, source_id)
            if existing and not force:
                result.skipped += 1
                return result
            messages = parser_messages()
            if not messages:
                result.skipped += 1
                return result
            if existing and existing.id is not None:
                # force re-import: the thread name is unique, so reuse the existing
                # thread and let the dedup_key check collapse what it already holds —
                # only genuinely-new events land.
                is_new_thread = False
                thread_id = existing.id
            else:
                is_new_thread = True
                thread_id = create_thread(
                    s, source=source, source_id=source_id,
                    title=title[:100] if title else None, source_metadata=source_metadata,
                )
            n, _ = assemble_events(s, thread_id, messages, builder)
            if n == 0:
                if is_new_thread:
                    # Row AND staged truth record — no ghost threads/<id>.jsonl on commit.
                    discard_new_thread(s, thread_id)
                result.skipped += 1
            else:
                set_thread_models_from_events(s, thread_id)
                result.imported += 1
                result.events_created += n
            s.commit()
    except Exception as e:  # noqa: BLE001 — one bad conversation must not stop the export
        # Log the full traceback (a one-line warning hid the cause) and preserve a
        # stub thread carrying the raw conversation + error, so a bad conversation is
        # visible in the archive and re-exportable instead of silently skipped.
        logger.exception("export import error for %s:%s", source, source_id)
        try:
            _import_conversation_stub(source, source_id, title, source_metadata, builder, raw, e)
            result.errored += 1
        except Exception:
            logger.exception("export stub also failed for %s:%s", source, source_id)
            result.skipped += 1
    return result


def import_export(path, *, force: bool = False) -> ExportImportResult:
    """Auto-classify a claude.ai / ChatGPT / xAI export and import it. Raises on
    unknown shape."""
    kind = classify_export(Path(path))
    if kind == "claude":
        return import_claude_ai_export(path, force=force)
    if kind == "chatgpt":
        return import_chatgpt_export(path, force=force)
    if kind == "xai":
        return import_xai_export(path, force=force)
    raise ValueError(f"Unrecognized export (not claude.ai, ChatGPT, or xAI): {path}")
