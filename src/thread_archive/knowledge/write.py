"""The curatorial write API — the librarian's hands on the topic graph.

Thin, typed functions over the event-sourced knowledge layer. Each *mutating* call
does the same three things atomically: append a ``KgEvent`` to the truth log, fold it
into the projection (:func:`thread_archive.knowledge.materialize.apply_event`), and —
when it owns the session — commit and drop the graph cache. One call = a durable log
line + an updated projection. The conversation archive stays untouched; this only
writes the knowledge overlay (topics, links, citations) that sits beside it.

Topics are threads (``thread_type='topic'``), so :func:`create_topic` writes a real
per-thread truth file and the ``topic.created`` event is the audit record; links and
evidence have no owning conversation file, so the ``kg_events.jsonl`` log *is* their
truth. Read helpers (:func:`review_queue`, :func:`topic_search`,
:func:`thread_user_messages`) are the curation-read surface the librarian needs to
decide *what* to write — kept here so the MCP and the skill share one library.

All writes are idempotent at the projection layer (upsert + tombstone), so re-running
the librarian never double-links or double-cites — a partial pass is simply redone.
"""

from __future__ import annotations

import functools
import json
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..store import Event, KgEvent, Thread, ThreadLink, TopicMessage, use_session
from ..truth.jsonl_log import append_kg_event, record_thread, shared_ingest_lock
from .graph import reset_cache
from .materialize import apply_event


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _locked_write(fn):
    """Run an own-session mutation under the shared reindex lock.

    Every mutating function here appends truth (a ``KgEvent``, sometimes a thread
    record) and commits; racing a reindex unlocked, that write can land after the
    rebuild's read point and commit into the database inode the swap replaces.
    When the caller passes its own ``session`` it owns the transaction boundary —
    and must hold the lock around it itself."""

    @functools.wraps(fn)
    def wrapper(*args, session: Optional[Session] = None, **kwargs):
        if session is not None:
            return fn(*args, session=session, **kwargs)
        with shared_ingest_lock():
            return fn(*args, session=None, **kwargs)

    return wrapper


def _topic_name(title: str) -> str:
    """The unique ``threads.name`` for a topic. Topics share the threads table with
    conversations, so they need a namespaced unique name distinct from a conversation's
    ``"{source}:{source_id}"``."""
    return f"topic:{title}"


def _emit(
    session: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id,
    payload: dict,
    actor: str,
    actor_thread_id: Optional[int],
) -> KgEvent:
    """Append a ``KgEvent`` and fold it into the projection, in the caller's session.

    Flush first so ``id`` / ``recorded_at`` are populated before the row is staged to
    truth and applied; the caller owns the commit (the before-commit drain writes the
    log line). The single seam every mutating function below goes through."""
    ev = KgEvent(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        payload=payload,
        actor=actor,
        actor_thread_id=actor_thread_id,
        occurred_at=_now(),
    )
    session.add(ev)
    session.flush()
    append_kg_event(session, ev)
    apply_event(session, ev)
    return ev


# ── topics ─────────────────────────────────────────────────────────────────────
@_locked_write
def create_topic(
    title: str,
    description: Optional[str] = None,
    *,
    topic_kind: Optional[str] = None,
    actor: str = "librarian",
    actor_thread_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> dict:
    """Create a topic (a ``thread_type='topic'`` thread) and return ``{topic_id, ...}``.

    Raises ``ValueError`` if a topic with that title already exists — search first
    (:func:`topic_search`) and link to the existing one rather than making a duplicate.
    """
    own = session is None
    with use_session(session) as s:
        existing = s.execute(select(Thread).where(Thread.name == _topic_name(title))).scalars().first()
        if existing is not None:
            raise ValueError(f"a topic named {title!r} already exists (id={existing.id}); link to it instead")
        topic = Thread(
            name=_topic_name(title), title=title, description=description,
            thread_type="topic", topic_kind=topic_kind,
        )
        s.add(topic)
        s.flush()
        record_thread(s, topic)  # the topic's own per-thread truth file opens with its record
        ev = _emit(
            s, event_type="topic.created", entity_type="topic", entity_id=topic.id,
            payload={"title": title, "description": description, "thread_id": topic.id},
            actor=actor, actor_thread_id=actor_thread_id,
        )
        result = {"topic_id": topic.id, "title": title, "event_id": ev.id}
        if own:
            s.commit()
            reset_cache()
    return result


@_locked_write
def rename_topic(
    topic_id: int, title: str, *, description: Optional[str] = None,
    actor: str = "librarian", actor_thread_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> dict:
    """Rename (and optionally re-describe) a topic."""
    own = session is None
    with use_session(session) as s:
        topic = _require_topic(s, topic_id)
        topic.title = title
        if description is not None:
            topic.description = description
        topic.updated_at = _now()
        record_thread(s, topic)  # latest-wins metadata in the topic's per-thread file
        payload: dict = {"title": title}
        if description is not None:
            payload["description"] = description
        ev = _emit(s, event_type="topic.renamed", entity_type="topic", entity_id=topic_id,
                   payload=payload, actor=actor, actor_thread_id=actor_thread_id)
        result = {"topic_id": int(topic_id), "title": title, "event_id": ev.id}
        if own:
            s.commit()
            reset_cache()
    return result


@_locked_write
def archive_topic(
    topic_id: int, *, actor: str = "librarian", actor_thread_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> dict:
    """Archive a topic (drops it from the live graph; the thread stays reachable)."""
    own = session is None
    with use_session(session) as s:
        topic = _require_topic(s, topic_id)
        topic.archived = True
        topic.updated_at = _now()
        record_thread(s, topic)
        ev = _emit(s, event_type="topic.archived", entity_type="topic", entity_id=topic_id,
                   payload={}, actor=actor, actor_thread_id=actor_thread_id)
        result = {"topic_id": int(topic_id), "archived": True, "event_id": ev.id}
        if own:
            s.commit()
            reset_cache()
    return result


@_locked_write
def merge_topics(
    from_id: int, into_id: int, *, actor: str = "librarian",
    actor_thread_id: Optional[int] = None, session: Optional[Session] = None,
) -> dict:
    """Merge ``from_id`` into ``into_id``: repoint its links + citations, archive it."""
    own = session is None
    with use_session(session) as s:
        from_topic = _require_topic(s, from_id)
        _require_topic(s, into_id)
        ev = _emit(s, event_type="topic.merged", entity_type="topic", entity_id=into_id,
                   payload={"from_id": int(from_id), "into_id": int(into_id)},
                   actor=actor, actor_thread_id=actor_thread_id)
        # Persist the merged-away topic's archival to its per-thread file too (the fold
        # archived it in the table; this records it in the truth the file carries).
        from_topic.archived = True
        from_topic.updated_at = ev.occurred_at
        record_thread(s, from_topic)
        result = {"from_id": int(from_id), "into_id": int(into_id), "event_id": ev.id}
        if own:
            s.commit()
            reset_cache()
    return result


# ── links ──────────────────────────────────────────────────────────────────────
@_locked_write
def link_threads(
    source_id: int, target_id: int, link_type: str = "related", *,
    strength: float = 1.0, evidence: Optional[str] = None, actor: str = "librarian",
    actor_thread_id: Optional[int] = None, session: Optional[Session] = None,
) -> dict:
    """Create (or update) a directed link between two threads/topics. Idempotent on
    ``(source, target, link_type)``."""
    own = session is None
    with use_session(session) as s:
        ev = _emit(
            s, event_type="link.created", entity_type="link",
            entity_id=f"{int(source_id)}:{int(target_id)}:{link_type}",
            payload={
                "source_thread_id": int(source_id), "target_thread_id": int(target_id),
                "link_type": link_type, "strength": float(strength),
                "evidence": evidence, "created_by": actor,
            },
            actor=actor, actor_thread_id=actor_thread_id,
        )
        result = {
            "source_thread_id": int(source_id), "target_thread_id": int(target_id),
            "link_type": link_type, "event_id": ev.id,
        }
        if own:
            s.commit()
            reset_cache()
    return result


@_locked_write
def unlink_threads(
    source_id: int, target_id: int, link_type: str = "related", *,
    actor: str = "librarian", actor_thread_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> dict:
    """Remove a link (a tombstone event — the operation is recorded, not erased)."""
    own = session is None
    with use_session(session) as s:
        ev = _emit(
            s, event_type="link.deleted", entity_type="link",
            entity_id=f"{int(source_id)}:{int(target_id)}:{link_type}",
            payload={"source_thread_id": int(source_id), "target_thread_id": int(target_id),
                     "link_type": link_type},
            actor=actor, actor_thread_id=actor_thread_id,
        )
        result = {"source_thread_id": int(source_id), "target_thread_id": int(target_id),
                  "link_type": link_type, "event_id": ev.id}
        if own:
            s.commit()
            reset_cache()
    return result


# ── evidence (message → topic citations) ─────────────────────────────────────────
@_locked_write
def add_topic_evidence(
    topic_id: int, event_id: int, thread_id: int, quote: str, *,
    actor: str = "librarian", actor_thread_id: Optional[int] = None,
    session: Optional[Session] = None,
) -> dict:
    """Cite a conversation message as evidence for a topic. Idempotent on
    ``(topic_id, event_id)``."""
    own = session is None
    with use_session(session) as s:
        ev = _emit(
            s, event_type="evidence.added", entity_type="topic_message",
            entity_id=f"{int(topic_id)}:{int(event_id)}",
            payload={"topic_id": int(topic_id), "event_id": int(event_id),
                     "thread_id": int(thread_id), "quote": quote, "actor": actor},
            actor=actor, actor_thread_id=actor_thread_id,
        )
        result = {"topic_id": int(topic_id), "event_id": int(event_id), "kg_event_id": ev.id}
        if own:
            s.commit()
    return result


@_locked_write
def archive_topic_evidence(
    topic_id: int, event_id: int, *, actor: str = "librarian",
    actor_thread_id: Optional[int] = None, session: Optional[Session] = None,
) -> dict:
    """Archive a citation (tombstone — sets ``archived_at``, keeps the row + history)."""
    own = session is None
    with use_session(session) as s:
        ev = _emit(
            s, event_type="evidence.archived", entity_type="topic_message",
            entity_id=f"{int(topic_id)}:{int(event_id)}",
            payload={"topic_id": int(topic_id), "event_id": int(event_id)},
            actor=actor, actor_thread_id=actor_thread_id,
        )
        result = {"topic_id": int(topic_id), "event_id": int(event_id), "kg_event_id": ev.id}
        if own:
            s.commit()
    return result


# ── curation-read surface ────────────────────────────────────────────────────────
def review_queue(
    limit: int = 20, *, exclude_source_id: Optional[str] = None,
    exclude_ids: Optional[list[int]] = None, session: Optional[Session] = None,
) -> list[dict]:
    """Unreviewed conversation threads — the librarian's backlog.

    Event-bearing conversation threads the librarian hasn't curated yet, newest first.
    Pass ``exclude_source_id`` to drop the caller's own live session (its still-growing
    transcript sits at the top of its own queue otherwise).

    **State is the data, not a ledger.** 'Reviewed' is *derived from the curation a thread
    produced* — a thread leaves the queue the instant it gains its first live topic
    citation (or a link touching it); there is no summary column or processed-list to keep
    in sync. That makes the queue idempotent (a half-done thread simply reappears) and safe
    to drain even if two workers briefly overlap. Corollary: a thread genuinely read but
    yielding nothing worth citing stays in the queue — acceptable, and in practice the
    librarian-gate forces ≥1 citation per processed thread, so reviewed ⇒ cited.

    **Parallel backfill via lease-claims.** When ``$THREAD_ARCHIVE_LIBRARIAN_WORKER`` is
    set (the backfill driver sets a distinct id per instance), this call *claims* the batch
    it returns — recording a timestamped lease in ``<home>/.librarian-claims.json`` so other
    workers skip it, and reclaiming any lease older than an hour (a dead worker's). Launch N
    instances with distinct worker ids and they self-balance with no central coordinator.
    Interactive use (no worker id) is a plain read. ``exclude_ids`` filters explicitly and
    bypasses the claim path (it's how the claim layer asks for the un-held remainder)."""
    worker = os.environ.get("THREAD_ARCHIVE_LIBRARIAN_WORKER")
    if worker and session is None and exclude_ids is None:
        from ._claims import claim_review_batch

        return claim_review_batch(worker, batch=limit, exclude_source_id=exclude_source_id)

    has_events = select(Event.id).where(Event.thread_id == Thread.id).exists()
    # 'Curated' = the librarian drew something from this thread: a live (non-archived)
    # topic citation sourced from it, or a link touching it. Review state is
    # derived from the curation the thread produced, never from a summary
    # column (there is none).
    is_curated = or_(
        select(TopicMessage.id)
        .where(TopicMessage.thread_id == Thread.id, TopicMessage.archived_at.is_(None))
        .exists(),
        select(ThreadLink.id)
        .where(
            or_(
                ThreadLink.source_thread_id == Thread.id,
                ThreadLink.target_thread_id == Thread.id,
            )
        )
        .exists(),
    )
    conds = [
        Thread.thread_type == "conversation",
        ~is_curated,
        or_(Thread.archived.is_(False), Thread.archived.is_(None)),
        has_events,
    ]
    if exclude_source_id:
        conds.append(or_(Thread.source_id.is_(None), Thread.source_id != exclude_source_id))
    if exclude_ids:
        conds.append(Thread.id.notin_(exclude_ids))
    with use_session(session) as s:
        rows = s.execute(
            select(Thread.id, Thread.title, Thread.source_id)
            .where(*conds).order_by(Thread.id.desc()).limit(limit)
        ).all()
    return [{"id": r.id, "title": r.title, "source_id": r.source_id} for r in rows]


def topic_search(query: str, limit: int = 10, *, session: Optional[Session] = None) -> list[dict]:
    """Find existing live topics by title substring — search before creating duplicates."""
    like = f"%{query}%"
    with use_session(session) as s:
        rows = s.execute(
            select(Thread.id, Thread.title).where(
                Thread.thread_type == "topic",
                or_(Thread.archived.is_(False), Thread.archived.is_(None)),
                Thread.title.ilike(like),
            ).order_by(Thread.id).limit(limit)
        ).all()
    return [{"topic_id": r.id, "title": r.title} for r in rows]


def thread_user_messages(
    thread_id: int, *, limit: Optional[int] = None, session: Optional[Session] = None,
) -> list[dict]:
    """A thread's user messages as ``[{event_id, text}]`` — the cheap, high-signal read
    the librarian cites from (citations anchor on these ``event_id`` values)."""
    q = select(Event.id, Event.payload).where(
        Event.thread_id == int(thread_id),
        Event.event_type.in_(("user_message_sent", "thread_message_sent")),
    ).order_by(Event.id)
    if limit:
        q = q.limit(limit)
    out: list[dict] = []
    with use_session(session) as s:
        for eid, payload in s.execute(q):
            p = payload if isinstance(payload, dict) else json.loads(payload)
            content = (p.get("content") or "").strip()
            if content:
                out.append({"event_id": eid, "text": content})
    return out


def _require_topic(session: Session, topic_id: int) -> Thread:
    topic = session.get(Thread, int(topic_id))
    if topic is None or topic.thread_type != "topic":
        raise ValueError(f"no topic with id {topic_id}")
    return topic
