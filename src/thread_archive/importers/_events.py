"""The shared parse/build → dedup → write loop.

Every provider's import is the same loop: walk NormalizedMessages, mint a stream id
per user turn (the following assistant turns reuse it), build events via
``DefaultEventBuilder`` with monotonic timestamp inheritance, and write. That loop
lives in one function, :func:`assemble_events`.

Idempotence is enforced on the ``dedup_key`` the builder computes (timestamp-free,
content-inclusive, scoped per thread). A re-import — even one that re-reads
already-imported lines after a watermark loss — adds nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from thread_import import DefaultEventBuilder

from ..store import Event
from ..truth import write_events

logger = logging.getLogger(__name__)


def _existing_dedup_keys(session: Session, thread_id: int) -> set[str]:
    rows = session.execute(
        select(Event.dedup_key).where(
            Event.thread_id == thread_id, Event.dedup_key.is_not(None)
        )
    ).scalars().all()
    return set(rows)


def _anchor_event(events: list, event_type: str):
    """The content-bearing anchor event of a built message (the user_message_sent
    of a user turn, the api_request_completed of an assistant turn), or None."""
    for ev in events:
        if ev.event_type == event_type:
            return ev
    return None


def _message_already_present(session: Session, thread_id: int, anchor) -> bool:
    """True when an event with the same (type, content, occurred_at) already exists
    in the thread. Used to suppress the replayed prefix when a CC continuation /
    fork-resume merges into an existing thread (its fresh UUIDs defeat the dedup_key
    check). The built ``anchor.occurred_at`` is the deterministic parse of the source
    line's timestamp, so passing it as a bind param matches the value the original
    import stored through the same column processor — robust across tz/format."""
    if anchor is None:
        return False
    content = anchor.payload.get("content")
    if content is None:
        return False
    return session.execute(
        select(Event.id)
        .where(
            Event.thread_id == thread_id,
            Event.event_type == anchor.event_type,
            func.json_extract(Event.payload, "$.content") == content,
            Event.occurred_at == anchor.occurred_at,
        )
        .limit(1)
    ).first() is not None


def _to_event(thread_id: int, te) -> Event:
    """Map a portable ThreadEvent onto an Event row (thread_id added at write time)."""
    return Event(
        thread_id=thread_id,
        stream_id=te.stream_id,
        api_call_id=te.api_call_id,
        event_type=te.event_type,
        payload=te.payload,
        occurred_at=te.occurred_at,
        correlation_id=te.correlation_id,
        dedup_key=te.dedup_key,
    )


def assemble_events(
    session: Session,
    thread_id: int,
    messages: list,
    builder: DefaultEventBuilder,
    *,
    base_prev_ts: Optional[datetime] = None,
    cross_pass_dedup: bool = False,
) -> tuple[int, Optional[str]]:
    """Build events from NormalizedMessages and write the not-yet-seen ones.

    ``base_prev_ts`` seeds the timestamp-inheritance anchor (grok/opencode seed it
    from the newest persisted event so a later incremental pass doesn't sort to the
    top of the thread). Returns ``(events_created, last_message_uuid)``.

    ``cross_pass_dedup`` adds a message-level (content + timestamp) existence check
    on top of the dedup_key membership check. It is enabled only when merging a CC
    continuation / fork-resume into an existing thread, where the replayed prefix
    carries fresh UUIDs (so its dedup_keys won't match) but identical content +
    timestamps. A matched user turn is skipped along with its following assistant
    turns (until the next genuinely-new user turn), mirroring canonical.
    """
    if not messages:
        return 0, None

    seen = _existing_dedup_keys(session, thread_id)
    batch: list[Event] = []
    last_uuid: Optional[str] = None
    current_stream_id: Optional[str] = None
    prev_occurred_at: Optional[datetime] = base_prev_ts
    skip_until_next_user = False

    for msg in messages:
        role = msg.get("role", "")
        # The parser emits the provider id as `provider_message_id`; older CC line
        # dicts use `uuid`. Track whichever is present.
        message_id = msg.get("uuid") or msg.get("provider_message_id")
        if message_id:
            last_uuid = message_id

        if role == "user":
            current_stream_id = str(uuid.uuid4())
            events = builder.build_events(msg, current_stream_id, prev_occurred_at=prev_occurred_at)
        elif role == "assistant":
            if not current_stream_id:
                current_stream_id = str(uuid.uuid4())
            events = builder.build_events(
                msg, current_stream_id, str(uuid.uuid4()), prev_occurred_at=prev_occurred_at
            )
        elif role == "system":
            events = builder.build_events(
                msg, current_stream_id or str(uuid.uuid4()), prev_occurred_at=prev_occurred_at
            )
        else:
            # A role the builder doesn't specifically model (tool/function/developer/
            # model/… from non-CC harnesses). Preserve the turn rather than drop it.
            events = builder.build_events(
                msg, current_stream_id or str(uuid.uuid4()), prev_occurred_at=prev_occurred_at
            )

        if events:
            # Advance the anchor even for deduped messages so a later new message
            # inherits a monotonic time (never a silent now()).
            prev_occurred_at = events[-1].occurred_at

        # Cross-pass message-level dedup (continuation/fork merge only): skip a turn
        # whose anchor (content + timestamp) is already in the thread.
        if cross_pass_dedup:
            if role == "user":
                skip_until_next_user = _message_already_present(
                    session, thread_id, _anchor_event(events, "user_message_sent")
                )
                if skip_until_next_user:
                    continue
            elif role == "assistant":
                if skip_until_next_user:
                    continue
                if _message_already_present(
                    session, thread_id, _anchor_event(events, "api_request_completed")
                ):
                    continue

        for te in events:
            if te.dedup_key and te.dedup_key in seen:
                continue
            if te.dedup_key:
                seen.add(te.dedup_key)
            batch.append(_to_event(thread_id, te))

    if batch:
        write_events(session, batch)
        # Index the new events into the FTS surface in the same transaction, so
        # search stays current without a full rebuild and FTS commits atomically
        # with the events + truth log.
        from ..retrieval.fts import index_events

        index_events(session, batch)
    return len(batch), last_uuid


def import_lines(
    session: Session,
    thread_id: int,
    lines: list[dict],
    parser,
    builder: DefaultEventBuilder,
    *,
    source: str = "claude-code",
    source_id: str = "",
    cross_pass_dedup: bool = False,
) -> tuple[int, Optional[str]]:
    """Claude Code: parse a line bundle via the thread_import parser, then assemble."""
    session_data = {
        "provider": "claude-code",
        "sessions": [{"session_id": "incremental", "project": "incremental", "lines": lines}],
    }
    messages = parser.parse_export(session_data)
    return assemble_events(session, thread_id, messages, builder, cross_pass_dedup=cross_pass_dedup)
