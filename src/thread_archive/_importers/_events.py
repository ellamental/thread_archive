"""The shared parse/build → dedup → write loop.

Every provider's import is the same loop: walk NormalizedMessages, mint a stream id
per user turn (the following assistant turns reuse it), build events via
``DefaultEventBuilder`` with monotonic timestamp inheritance, and write. That loop
lives in one function, :func:`assemble_events`.

Idempotence is enforced on the ``dedup_key`` the builder computes (timestamp-free,
content-inclusive, scoped per thread). A re-import — even one that re-reads
already-imported lines after a watermark loss — adds nothing.

A turn straddles polls: the user line can land in one poll and its assistant reply in
the next. So both of the loop's carried anchors are seeded from what the thread
already holds — the stream id (:func:`_last_stream_id`) and, for the providers that
pass it, the timestamp (``base_prev_ts``). Without that seed the continuing assistant
turn would open its own stream and orphan itself from the user turn it answers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from thread_archive._thread_import import DefaultEventBuilder

from .._store import Event
from .._truth import write_events

logger = logging.getLogger(__name__)

# SQLite's default host-parameter ceiling is 999; stay well under it per IN (...).
_KEY_CHUNK = 500


def _existing_dedup_keys(
    session: Session, thread_id: int, keys: Optional[Iterable[str]] = None
) -> set[str]:
    """Which of ``keys`` the thread already holds — or *all* its keys when ``keys``
    is None.

    The import loop asks only about the keys it built this pass. Loading every key in
    the thread instead makes each poll of a long session pay for the whole session's
    history, which is the shape of quadratic work on a transcript that grows by a line
    at a time.
    """
    stmt = select(Event.dedup_key).where(
        Event.thread_id == thread_id, Event.dedup_key.is_not(None)
    )
    if keys is None:
        return set(session.execute(stmt).scalars().all())

    candidates = list(keys)
    found: set[str] = set()
    for i in range(0, len(candidates), _KEY_CHUNK):
        chunk = candidates[i : i + _KEY_CHUNK]
        found.update(session.execute(stmt.where(Event.dedup_key.in_(chunk))).scalars().all())
    return found


def _last_stream_id(session: Session, thread_id: int) -> Optional[str]:
    """The stream id of the thread's newest event — the open turn a continuing
    assistant message belongs to. None on a thread with no events yet."""
    return session.execute(
        select(Event.stream_id)
        .where(Event.thread_id == thread_id)
        .order_by(Event.id.desc())
        .limit(1)
    ).scalars().first()


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

    last_uuid: Optional[str] = None
    prev_occurred_at: Optional[datetime] = base_prev_ts
    # Seed from the thread's open turn so an assistant reply that arrives in a later
    # poll than its user line joins that turn instead of stranding itself in a new one.
    current_stream_id: Optional[str] = _last_stream_id(session, thread_id)

    built: list[tuple[str, list]] = []
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
        else:
            # 'system', or a role the builder doesn't specifically model (tool/function/
            # developer/model/… from non-CC harnesses). Preserve the turn, don't drop it.
            events = builder.build_events(
                msg, current_stream_id or str(uuid.uuid4()), prev_occurred_at=prev_occurred_at
            )

        if events:
            # Advance the anchor even for deduped messages so a later new message
            # inherits a monotonic time (never a silent now()).
            prev_occurred_at = events[-1].occurred_at
        built.append((role, events))

    seen = _existing_dedup_keys(
        session, thread_id, {te.dedup_key for _, evs in built for te in evs if te.dedup_key}
    )
    batch: list[Event] = []
    skip_until_next_user = False

    for role, events in built:
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
        from .._retrieval.fts import index_events

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
