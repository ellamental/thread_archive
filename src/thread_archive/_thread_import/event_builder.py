"""Event builder for converting NormalizedMessage to Thread events.

This module provides the single source of truth for transforming
NormalizedMessage objects (from thread_archive._thread_import) into ThreadEvent objects
that can be written to the Thread event log.

The EventBuilder protocol decouples event creation from database writes,
making the logic testable and reusable across different import paths.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, cast

from thread_archive._thread_import.timestamps import parse_timestamp

from .parsers.base import NormalizedMessage

# Payload keys that carry an event's *semantic content*. The dedup key hashes
# only these — never the timestamp — so an exact re-import collapses to one row,
# while an edited turn (changed content, same provider_message_id) produces a
# DIFFERENT key and is kept as a new event. Import must never be the thing that
# discards either version of a turn; the log is append-only.
_DEDUP_CONTENT_KEYS = (
    "content", "text", "thinking", "output", "error", "content_blocks",
    "input", "data", "files", "summarizes", "model",
)


def compute_content_hash(payload: dict) -> str:
    """Timestamp-free hash of an event's semantic content (see _DEDUP_CONTENT_KEYS)."""
    material = {k: payload[k] for k in _DEDUP_CONTENT_KEYS if k in payload}
    blob = json.dumps(material, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def compute_dedup_key(provider_message_id: str, event_type: str, payload: dict) -> str:
    """Deterministic natural identity for one event — stable across re-imports
    and across producers.

    Form: ``{provider_message_id|content_anchor}:{event_type}:{block}:{content_hash}``.
    The key is NOT prefixed with the thread id: dedup is thread-scoped by the
    ``WHERE thread_id = ...`` clause in the importer, so the prefix would be
    redundant — and every stored key is bare (``denamespace_dedup_keys``
    normalizes any prefixed stragglers); keep this bare.
    Same id + same content → same key (idempotent). Same id + edited content →
    different key (both versions kept). Events with no provider id fall back to a
    content anchor so they still dedup on content+type+position.
    """
    content_hash = compute_content_hash(payload)
    if payload.get("tool_call_id"):
        block = f"tool={payload['tool_call_id']}"
    elif payload.get("block_index") is not None:
        block = f"blk={payload['block_index']}"
    else:
        block = ""
    anchor = provider_message_id or f"c={content_hash}"
    return f"{anchor}:{event_type}:{block}:{content_hash}"


@dataclass
class ThreadEvent:
    """Portable event representation - not tied to DB schema.

    This mirrors backend/event_log.py's Event but is independent
    of the database layer, allowing event creation logic to be
    tested without database access.
    """
    event_type: str
    payload: dict
    stream_id: str
    api_call_id: Optional[str] = None
    # tz-aware UTC: a naive local-time default sorted wrong against the aware
    # timestamps every builder path stores (SQLite compares them as text).
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Deterministic natural-key identity (timestamp-free, content-inclusive),
    # set by build_events. Bare (not thread-id-prefixed) — dedup is thread-scoped
    # by the importer's WHERE clause. Observe-only until the dedup constraint
    # lands; see compute_dedup_key.
    dedup_key: Optional[str] = None

    # Metadata for tracking
    caused_by_event_id: Optional[str] = None
    correlation_id: Optional[str] = None


class EventBuilder(Protocol):
    """Protocol for transforming NormalizedMessage into Thread events.

    Implementations convert a single NormalizedMessage into the sequence
    of ThreadEvent objects needed to represent it in the event log.

    For user messages: returns [USER_MESSAGE_SENT]
    For assistant messages: returns [API_REQUEST_STARTED,
        THINKING_COMPLETE*, TEXT_COMPLETE*, TOOL_USE_COMPLETE*,
        API_REQUEST_COMPLETED, STREAM_COMPLETED]
    """

    def build_events(
        self,
        message: NormalizedMessage,
        stream_id: str,
        api_call_id: Optional[str] = None,
    ) -> list[ThreadEvent]:
        """Convert a normalized message to Thread events.

        Args:
            message: The normalized message from archive
            stream_id: The stream ID for this message
            api_call_id: Optional API call ID (generated if not provided)

        Returns:
            List of ThreadEvent objects to be written to the event log
        """
        ...


class DefaultEventBuilder:
    """Standard event builder for all providers.

    The single source of truth for how NormalizedMessage becomes Thread events.
    """

    def build_events(
        self,
        message: NormalizedMessage,
        stream_id: str,
        api_call_id: Optional[str] = None,
        prev_occurred_at: Optional[datetime] = None,
    ) -> list[ThreadEvent]:
        """Convert a normalized message to Thread events.

        ``prev_occurred_at`` is the previous turn's timestamp; when this
        message carries no source timestamp it is inherited (monotonic, not
        fabricated) rather than stamped with ``now()``.
        """
        role = message.get("role", "")
        occurred_at, ts_provenance = self._resolve_occurred_at(message, prev_occurred_at)

        if role == "user":
            events = self._build_user_events(message, stream_id, occurred_at)
        elif role == "assistant":
            events = self._build_assistant_events(
                message, stream_id, api_call_id, occurred_at
            )
        elif role == "system":
            events = self._build_system_events(message, stream_id, occurred_at)
        else:
            events = self._build_generic_message_events(message, stream_id, occurred_at)

        provider_message_id = message.get("provider_message_id", "") or ""
        for event in events:
            # Flag any event whose time wasn't a real source timestamp, so a
            # gap stays visible (never a silent now()).
            if ts_provenance is not None:
                event.payload.setdefault("timestamp_inferred", True)
                event.payload.setdefault("timestamp_source", ts_provenance)
            event.dedup_key = compute_dedup_key(
                provider_message_id, event.event_type, event.payload
            )
        return events

    def _resolve_occurred_at(
        self,
        message: NormalizedMessage,
        prev_occurred_at: Optional[datetime],
    ) -> tuple[datetime, Optional[str]]:
        """Resolve a message's timestamp without ever silently fabricating one.

        Returns ``(occurred_at, provenance)`` — provenance is ``None`` for a
        real source timestamp, else a marker string. A hard requirement:
        message timestamps must never be silently invented; a gap must stay
        visible so it can be re-exported.
        """
        ts = self._parse_timestamp(message.get("created_at"))
        if ts is not None:
            return ts, None
        if prev_occurred_at is not None:
            # Inherit the prior turn's time — monotonic, not invented.
            return prev_occurred_at, "inferred_prev_turn"
        # No timestamp anywhere. Flag it loudly so it's queryable; the planned
        # escalation is an importer-level hard-fail/notify (Decision 2).
        return datetime.now(timezone.utc), "fabricated_no_source"

    # Text-only content from tool loading confirmations (not real user messages)
    _TOOL_LOAD_CONFIRMATIONS = frozenset({
        "Tool loaded.",
    })

    # User content-block types already represented by a dedicated event (or folded
    # into the user_message_sent payload, for images). Any *other* block type on a
    # user turn is preserved as its own content_block event so nothing is dropped.
    _USER_BLOCKS_ALREADY_EMITTED = frozenset({
        "text", "tool_result", "ide_context", "model_change", "image",
    })

    # Prefix → injection source. Order matters only to mirror the original
    # if/elif fall-through (no prefix here is a prefix of another).
    _INJECTED_PREFIXES: tuple[tuple[str, str], ...] = (
        ("This session is being continued", "context_continuation"),
        ("Caveat:", "caveat"),
        ("[Request interrupted", "request_interrupted"),
        ("[Image:", "image_placeholder"),
        ("Base directory for this skill", "skill_doc"),
        ("# Update Config Skill", "skill_doc"),
        ("# Keybindings Skill", "skill_doc"),
        ("<steward_checkin", "steward_checkin"),
    )

    # Substring (anywhere) → injection source. session_briefing only counts
    # within the first 200 chars (handled in the loop).
    _INJECTED_SUBSTRINGS: tuple[tuple[str, str], ...] = (
        ("<session_briefing", "session_briefing"),
        ("<task-notification>", "task_notification"),
        ("<bash-notification>", "bash_notification"),
    )

    @classmethod
    def _detect_injected(cls, content: str) -> tuple[bool, str | None]:
        """Detect system-injected content masquerading as user messages.

        Returns (is_injected, source) where source describes the injection type.
        """
        if not content:
            return False, None

        # Warmup is special-cased: prefix AND a short-length guard.
        if content.startswith("Warmup") and len(content) < 20:
            return True, "warmup"

        for prefix, source in cls._INJECTED_PREFIXES:
            if content.startswith(prefix):
                return True, source

        # session_briefing is limited to the head; the rest match anywhere.
        head = content[:200]
        for needle, source in cls._INJECTED_SUBSTRINGS:
            haystack = head if source == "session_briefing" else content
            if needle in haystack:
                return True, source

        return False, None

    def _build_user_events(
        self,
        message: NormalizedMessage,
        stream_id: str,
        occurred_at: datetime,
    ) -> list[ThreadEvent]:
        """Create events for a user message. Returns [USER_MESSAGE_SENT]."""
        content_text = message.get("content_text", "")
        content_blocks = message.get("content_blocks", [])
        provider_message_id = message.get("provider_message_id", "")

        # Check for tool_result blocks (CC JSONL: tool results are user messages
        # with tool_result content blocks but no user text)
        tool_result_blocks = [
            b for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        # IDE context blocks (opened files, selections) the parser lifted out of the
        # raw turn — "what Ella was looking at when she sent this". Preserved as their
        # own events below rather than stripped away with the tags.
        ide_context_blocks = [
            b for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "ide_context"
        ]
        # A manual `/model` switch the parser preserved (its command text stripped to
        # empty) — recorded as its own event so the archive can mark the switch.
        model_change_blocks = [
            b for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "model_change"
        ]

        # Keep any turn that carries *any* content — text, or any content block
        # (tool_result / ide_context / model_change / image / an unmodeled block).
        # Only a genuinely empty turn (no text and no blocks at all) is skipped;
        # everything else is preserved below, unknown block types included.
        if not content_text.strip() and not content_blocks:
            return []

        # Detect tool-loading confirmation turns: content blocks are mostly
        # tool_result/tool_reference with only a trivial text confirmation.
        # These are system artifacts, not real user messages.
        has_tool_results = any(
            isinstance(b, dict) and b.get("type") in ("tool_result", "tool_reference")
            for b in content_blocks
        )
        if has_tool_results and content_text.strip() in self._TOOL_LOAD_CONFIRMATIONS:
            return [
                ThreadEvent(
                    event_type="tool_loaded",
                    payload={
                        "content": content_text,
                        "provider_data": {"provider_message_id": provider_message_id},
                    },
                    stream_id=stream_id,
                    occurred_at=occurred_at,
                )
            ]

        # Check for multimodal content (images)
        has_images = any(
            isinstance(b, dict) and b.get("type") == "image"
            for b in content_blocks
        )

        if has_images:
            # Extract images in the format the live path uses (UserMessagePayload.images)
            payload = {
                "content": content_text,
                "content_type": "multimodal",
                "images": self._extract_user_images(content_blocks),
                "provider_data": {"provider_message_id": provider_message_id},
            }
        else:
            payload = {
                "content": content_text,
                "provider_data": {"provider_message_id": provider_message_id},
            }

        events = []

        # Create user_message_sent when there's real user text OR images — an
        # image-only turn (a screenshot paste with no caption) must not be
        # dropped; its images ride on the multimodal payload built above.
        if content_text.strip() or has_images:
            if content_text.strip():
                # Tag injected/system content so we can distinguish real user input
                injected, source = self._detect_injected(content_text)
                if injected:
                    payload["injected"] = True
                    payload["source"] = source

                # Tag steering messages (typed mid-turn, queued, consumed as an
                # attachment injection). The read path uses this to slot a
                # late-backfilled steering event into its chronological position.
                if (message.get("provider_data") or {}).get("queued_command"):
                    payload["queued"] = True

            events.append(ThreadEvent(
                event_type="user_message_sent",
                payload=payload,
                stream_id=stream_id,
                occurred_at=occurred_at,
            ))

        # Extract tool results embedded in user messages (CC JSONL format:
        # tool results come as content blocks on user-role messages)
        for block in tool_result_blocks:
            events.append(self._tool_result_event(block, stream_id, None, occurred_at))

        # Preserve IDE context (opened files, selections) as its own events, so a
        # turn's editor context survives import — and a context-only turn isn't lost.
        for block in ide_context_blocks:
            events.append(self._ide_context_event(block, stream_id, occurred_at))

        # A manual model switch (`/model X`) becomes its own event so the reader can
        # render a "switched to X" marker between turns.
        for block in model_change_blocks:
            events.append(ThreadEvent(
                event_type="model_change",
                payload={
                    "to": block.get("to_model"),
                    "trigger": block.get("trigger", "user"),
                },
                stream_id=stream_id,
                occurred_at=occurred_at,
            ))

        # Preserve any content block the cases above didn't already emit — an
        # unmodeled user block (document / mcp_tool_use / a future kind, or the
        # parser's "raw" catch-all) becomes its own content_block event rather
        # than silently vanishing. Mirrors the assistant path (_process_content_block).
        for idx, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type in self._USER_BLOCKS_ALREADY_EMITTED:
                continue
            events.append(ThreadEvent(
                event_type="content_block",
                payload={
                    "block_type": block_type,
                    "data": block,
                    "block_index": block.get("seq", idx),
                },
                stream_id=stream_id,
                occurred_at=occurred_at,
            ))

        return events

    @staticmethod
    def _extract_user_images(content_blocks: list) -> list:
        """Extract base64 image blocks into the live UserMessagePayload.images shape."""
        images = []
        for b in content_blocks:
            if isinstance(b, dict) and b.get("type") == "image":
                source = b.get("source", {})
                if source.get("type") == "base64" and source.get("data"):
                    images.append({
                        "media_type": source.get("media_type", "image/png"),
                        "data": source["data"],
                    })
        return images

    @staticmethod
    def _tool_result_event(
        block: Mapping[str, Any],
        stream_id: str,
        api_call_id: Optional[str],
        occurred_at: datetime,
    ) -> "ThreadEvent":
        """Build the event for a tool_result block.

        An error result becomes ``tool_execution_error`` (with the failure text
        under ``error``), NOT ``tool_execution_completed`` — because the display
        reader only recognizes a tool failure via the distinct
        ``tool_execution_error`` event type; an is_error flag on
        ``tool_execution_completed`` is silently rendered as success
        (message_view._build_tool_result_lookup). This is the canonical form
        every producer must converge on.
        """
        tool_call_id = block.get("tool_use_id")
        name = block.get("name", "unknown")
        content = block.get("content", "")
        if block.get("is_error"):
            event_type = "tool_execution_error"
            payload: dict = {"tool_call_id": tool_call_id, "tool_name": name, "error": content}
        else:
            event_type = "tool_execution_completed"
            payload = {
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "output": content,
                "is_error": False,
            }
        # A missing id can never pair to its call. Flag it rather than minting a
        # uuid that's guaranteed to dangle.
        if not tool_call_id:
            payload["unpaired"] = True
        return ThreadEvent(
            event_type=event_type,
            payload=payload,
            stream_id=stream_id,
            api_call_id=api_call_id,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _ide_context_event(
        block: Mapping[str, Any],
        stream_id: str,
        occurred_at: datetime,
    ) -> "ThreadEvent":
        """Build the event for an ``ide_context`` block — an opened file or a code
        selection the parser lifted out of the raw user turn. Kept as a first-class
        event so "what was open / selected when this turn was sent" survives import
        rather than being stripped away with the tags. ``block_index`` (the block's
        seq) namespaces the dedup key so several context blocks on one turn don't
        collapse into one."""
        payload: dict = {
            "context_type": block.get("context_type"),
            "content": block.get("raw_content", ""),
            "block_index": block.get("seq"),
        }
        file_path = block.get("file_path")
        if file_path:
            payload["file_path"] = file_path
        return ThreadEvent(
            event_type="ide_context",
            payload=payload,
            stream_id=stream_id,
            occurred_at=occurred_at,
        )

    def _build_generic_message_events(
        self,
        message: NormalizedMessage,
        stream_id: str,
        occurred_at: datetime,
    ) -> list[ThreadEvent]:
        """Preserve a message whose role isn't one of user/assistant/system.

        Other harnesses emit roles the builder doesn't model — ``tool``,
        ``function``, ``developer``, ``model``, or anything ``normalize_role`` passes
        through unchanged. Rather than drop the turn, keep it verbatim as a single
        ``message`` event carrying the role, its text, and its raw content blocks, so
        nothing is lost on import and it stays searchable + re-exportable."""
        role = message.get("role", "") or "unknown"
        content_text = message.get("content_text", "")
        content_blocks = message.get("content_blocks", [])
        if not content_text.strip() and not content_blocks:
            return []
        payload: dict = {"role": role, "content": content_text}
        if content_blocks:
            payload["content_blocks"] = content_blocks
        return [ThreadEvent(
            event_type="message",
            payload=payload,
            stream_id=stream_id,
            occurred_at=occurred_at,
        )]

    def _build_system_events(
        self,
        message: NormalizedMessage,
        stream_id: str,
        occurred_at: datetime,
    ) -> list[ThreadEvent]:
        """Create events for system/metadata messages.

        Maps content block types to event types:
        - context_summary → CONTEXT_SUMMARY
        - file_snapshot → FILE_SNAPSHOT
        - Other system messages → stored with their block type
        """
        content_blocks = message.get("content_blocks", [])
        content_text = message.get("content_text", "")
        provider_data = message.get("provider_data", {})

        if not content_blocks:
            return []

        # Determine event type from the first content block
        first_block = content_blocks[0] if content_blocks else {}
        block_type = first_block.get("type", "") if isinstance(first_block, dict) else ""

        if block_type == "context_summary":
            return [ThreadEvent(
                event_type="context_summary",
                payload={
                    "content": first_block.get("text", content_text),
                    "summary_type": provider_data.get("summary_type", "context_continuation"),
                    "summarizes": first_block.get("summarizes"),
                },
                stream_id=stream_id,
                occurred_at=occurred_at,
            )]
        elif block_type == "file_snapshot":
            return [ThreadEvent(
                event_type="file_snapshot",
                payload={
                    "files": first_block.get("files", []),
                    "message_id": message.get("provider_message_id", ""),
                    "snapshot_timestamp": first_block.get("timestamp"),
                },
                stream_id=stream_id,
                occurred_at=occurred_at,
            )]
        elif block_type == "queue_operation":
            return [ThreadEvent(
                event_type="queue_operation",
                payload=first_block.get("data", {}),
                stream_id=stream_id,
                occurred_at=occurred_at,
            )]
        elif block_type == "progress":
            return [ThreadEvent(
                event_type="progress",
                payload={
                    "data": first_block.get("data", {}),
                    "tool_use_id": first_block.get("tool_use_id"),
                },
                stream_id=stream_id,
                occurred_at=occurred_at,
            )]
        else:
            # Generic system event — preserve whatever data is there
            return [ThreadEvent(
                event_type="context_summary",
                payload={
                    "content": content_text,
                    "system_type": block_type,
                    "provider_data": provider_data,
                },
                stream_id=stream_id,
                occurred_at=occurred_at,
            )]

    def _build_assistant_events(
        self,
        message: NormalizedMessage,
        stream_id: str,
        api_call_id: Optional[str],
        occurred_at: datetime,
    ) -> list[ThreadEvent]:
        """Create events for an assistant message.

        Returns sequence: API_REQUEST_STARTED, [content events],
        API_REQUEST_COMPLETED, STREAM_COMPLETED
        """
        events = []
        api_call_id = api_call_id or str(uuid.uuid4())

        content_text = message.get("content_text", "")
        content_blocks = message.get("content_blocks", [])
        provider_data = message.get("provider_data", {})
        provider_message_id = message.get("provider_message_id", "")
        model = provider_data.get("model", "unknown")

        # API_REQUEST_STARTED
        events.append(ThreadEvent(
            event_type="api_request_started",
            payload={"model": model, "provider_data": {"provider_message_id": provider_message_id}},
            stream_id=stream_id,
            api_call_id=api_call_id,
            occurred_at=occurred_at,
        ))

        # Process content blocks. A block's own start_timestamp may refine the
        # time forward (never backward — blocks are emitted in content order and
        # `id` is the true ordinal), so the running timestamp stays monotonic
        # non-decreasing across the turn.
        api_blocks = []
        running_ts = occurred_at
        for idx, block in enumerate(content_blocks):
            if isinstance(block, dict) and block.get("start_timestamp"):
                block_ts = self._parse_timestamp(cast(Mapping[str, Any], block)["start_timestamp"])
                if block_ts is not None and block_ts >= running_ts:
                    running_ts = block_ts
            block_events, block_api = self._process_content_block(
                block, idx, stream_id, api_call_id, running_ts
            )
            events.extend(block_events)
            if block_api:
                api_blocks.append(block_api)

        # Fallback: if no blocks but has content_text
        if not content_blocks and content_text:
            api_blocks.append({"type": "text", "text": content_text})
            events.append(ThreadEvent(
                event_type="text_complete",
                payload={"text": content_text, "block_index": 0},
                stream_id=stream_id,
                api_call_id=api_call_id,
                occurred_at=occurred_at,
            ))

        # API_REQUEST_COMPLETED — completes after every block, so it carries the
        # running (max) timestamp, keeping the whole turn monotonic.
        usage = provider_data.get("usage", {})
        events.append(ThreadEvent(
            event_type="api_request_completed",
            payload={
                "stop_reason": provider_data.get("stop_reason", "end_turn"),
                "content_blocks": api_blocks,
                "model": model,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "thinking_tokens": usage.get("thinking_tokens", 0),
            },
            stream_id=stream_id,
            api_call_id=api_call_id,
            occurred_at=running_ts,
        ))

        # STREAM_COMPLETED
        events.append(ThreadEvent(
            event_type="stream_completed",
            payload={},
            stream_id=stream_id,
            api_call_id=api_call_id,
            occurred_at=running_ts,
        ))

        return events

    def _process_content_block(
        self,
        block: Any,
        idx: int,
        stream_id: str,
        api_call_id: str,
        occurred_at: datetime,
    ) -> tuple[list[ThreadEvent], Optional[dict]]:
        """Process a single content block.

        Returns:
            Tuple of (events to emit, API block representation)
        """
        events = []
        api_block = None

        # occurred_at is resolved by the caller (monotonic non-decreasing across
        # the turn); this method maps a block to its events at that timestamp.

        # Handle raw text string
        if isinstance(block, str):
            api_block = {"type": "text", "text": block}
            events.append(ThreadEvent(
                event_type="text_complete",
                payload={"text": block, "block_index": idx},
                stream_id=stream_id,
                api_call_id=api_call_id,
                occurred_at=occurred_at,
            ))
            return events, api_block

        block_type = block.get("type", "")

        if block_type == "thinking":
            text = block.get("text", "")
            api_block = {"type": "thinking", "thinking": text}
            events.append(ThreadEvent(
                event_type="thinking_complete",
                payload={"text": text, "block_index": idx},
                stream_id=stream_id,
                api_call_id=api_call_id,
                occurred_at=occurred_at,
            ))

        elif block_type == "text":
            text = block.get("text", "")
            api_block = {"type": "text", "text": text}
            events.append(ThreadEvent(
                event_type="text_complete",
                payload={"text": text, "block_index": idx},
                stream_id=stream_id,
                api_call_id=api_call_id,
                occurred_at=occurred_at,
            ))

        elif block_type == "tool_use":
            tool_call_id = block.get("tool_call_id") or block.get("id")
            name = block.get("name", "unknown")
            input_data = block.get("input", {})
            api_block = {
                "type": "tool_use",
                "id": tool_call_id or "",
                "name": name,
                "input": input_data,
            }
            payload = {
                "tool_call_id": tool_call_id,
                "tool_name": name,
                "input": input_data,
                "block_index": idx,
            }
            # A tool_use with no id can't be paired to its result. Flag it
            # rather than minting a uuid that's guaranteed to dangle.
            if not tool_call_id:
                payload["unpaired"] = True
            if block.get("provider_tool_name"):
                payload["provider_data"] = {"provider_tool_name": block["provider_tool_name"]}
            events.append(ThreadEvent(
                event_type="tool_use_complete",
                payload=payload,
                stream_id=stream_id,
                api_call_id=api_call_id,
                occurred_at=occurred_at,
            ))

        elif block_type == "tool_result":
            events.append(self._tool_result_event(block, stream_id, api_call_id, occurred_at))
            # No api_block for tool_result - it's handled separately

        else:
            # A block type the builder doesn't specifically model. Other harnesses
            # emit server_tool_use, web_search_tool_result, redacted_thinking, image,
            # document, mcp_tool_use, … — preserve them verbatim rather than dropping:
            # as their own content_block event, and unchanged in the api summary.
            api_block = block
            events.append(ThreadEvent(
                event_type="content_block",
                payload={"block_type": block_type, "data": block, "block_index": idx},
                stream_id=stream_id,
                api_call_id=api_call_id,
                occurred_at=occurred_at,
            ))

        return events, api_block

    def _to_api_blocks(
        self,
        content_blocks: list[dict],
        for_user: bool = False
    ) -> list[dict]:
        """Convert content blocks to Anthropic API format."""
        api_blocks = []
        for block in content_blocks:
            if isinstance(block, str):
                api_blocks.append({"type": "text", "text": block})
                continue

            block_type = block.get("type")
            if block_type == "text":
                api_blocks.append({"type": "text", "text": block.get("text", "")})
            elif block_type == "image" and for_user:
                source = block.get("source", {})
                if source:
                    api_blocks.append({
                        "type": "image",
                        "source": source,
                    })
        return api_blocks

    def _parse_timestamp(
        self, ts: Optional[str], default: Optional[datetime] = None
    ) -> Optional[datetime]:
        """Parse an ISO/epoch timestamp; return ``default`` (None) on a
        missing/unparseable value. Never fabricates ``datetime.now()`` —
        callers own the fallback so a missing timestamp stays visible
        (never silently invented)."""
        return parse_timestamp(ts, default=default)
