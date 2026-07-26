"""Test-only writers for the topic-graph data plane.

The archive's knowledge layer is read-only in production — an external write layer
writes topics, links, citations, and stored summaries through the truth log.
These helpers give the suite that writer: each one appends the same truth
(``KgEvent`` rows via ``append_kg_event``, thread records via ``record_thread``)
and folds it into the projection (``apply_event``), so seeded data exercises
the real data plane — reindex replays it, verify parity-checks it — exactly
like production topic-graph writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from thread_archive._knowledge.materialize import apply_event
from thread_archive._store import Event, KgEvent, Thread, use_session
from thread_archive._truth.jsonl_log import append_kg_event, record_thread, shared_ingest_lock

# Caps mirrored by any real summary writer; asserted by the summary tests.
SUMMARY_MAX_CHARS = 4_000
INDEXED_SUMMARY_MAX_CHARS = 24_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _emit(
    session: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id,
    payload: dict,
    actor: str = "test",
) -> KgEvent:
    ev = KgEvent(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        payload=payload,
        actor=actor,
        actor_thread_id=None,
        occurred_at=_now(),
    )
    session.add(ev)
    session.flush()
    append_kg_event(session, ev)
    apply_event(session, ev)
    return ev


def create_topic(
    title: str,
    description: Optional[str] = None,
    *,
    topic_kind: Optional[str] = None,
    session: Optional[Session] = None,
) -> dict:
    """Seed a topic (a ``thread_type='topic'`` thread); ``{topic_id, title, event_id}``."""
    own = session is None
    with shared_ingest_lock(), use_session(session) as s:
        name = f"topic:{title}"
        if s.execute(select(Thread).where(Thread.name == name)).scalars().first() is not None:
            raise ValueError(f"a topic named {title!r} already exists")
        topic = Thread(name=name, title=title, description=description,
                       thread_type="topic", topic_kind=topic_kind)
        s.add(topic)
        s.flush()
        record_thread(s, topic)
        ev = _emit(
            s, event_type="topic.created", entity_type="topic", entity_id=topic.id,
            payload={"title": title, "description": description, "thread_id": topic.id},
        )
        result = {"topic_id": topic.id, "title": title, "event_id": ev.id}
        if own:
            s.commit()
    return result


def link_threads(
    source_id: str, target_id: str, link_type: str = "related", *,
    strength: float = 1.0, evidence: Optional[str] = None,
    session: Optional[Session] = None,
) -> dict:
    """Seed a directed link between two existing threads/topics."""
    own = session is None
    with shared_ingest_lock(), use_session(session) as s:
        for tid in (source_id, target_id):
            if s.get(Thread, tid) is None:
                raise ValueError(f"no thread with id {tid} — link endpoints must exist")
        ev = _emit(
            s, event_type="link.created", entity_type="link",
            entity_id=f"{source_id}:{target_id}:{link_type}",
            payload={"source_thread_id": source_id, "target_thread_id": target_id,
                     "link_type": link_type, "strength": float(strength),
                     "evidence": evidence, "created_by": "test"},
        )
        result = {"source_thread_id": source_id, "target_thread_id": target_id,
                  "link_type": link_type, "event_id": ev.id}
        if own:
            s.commit()
    return result


def add_topic_evidence(
    topic_id: str, event_id: int, thread_id: str, quote: str, *,
    session: Optional[Session] = None,
) -> dict:
    """Seed a citation of a real archived message as evidence for a topic."""
    own = session is None
    with shared_ingest_lock(), use_session(session) as s:
        topic = s.get(Thread, topic_id)
        if topic is None or topic.thread_type != "topic":
            raise ValueError(f"no topic with id {topic_id}")
        cited = s.get(Event, int(event_id))
        if cited is None:
            raise ValueError(f"no event with id {event_id}")
        if cited.thread_id != thread_id:
            raise ValueError(f"event {event_id} belongs to thread {cited.thread_id}, not {thread_id}")
        ev = _emit(
            s, event_type="evidence.added", entity_type="topic_message",
            entity_id=f"{topic_id}:{int(event_id)}",
            payload={"topic_id": topic_id, "event_id": int(event_id),
                     "thread_id": thread_id, "quote": quote, "actor": "test"},
        )
        result = {"topic_id": topic_id, "event_id": int(event_id), "kg_event_id": ev.id}
        if own:
            s.commit()
    return result


def set_thread_summary(
    thread_id: str,
    summary: Optional[str] = None,
    *,
    indexed_summary: Optional[str] = None,
    session: Optional[Session] = None,
) -> dict:
    """Seed a stored summary — thread metadata (not a ``KgEvent``): re-staged in
    the thread's truth record and synced into the thread-meta search docs, the
    same shape a production summary write leaves behind."""
    if not summary and not indexed_summary:
        raise ValueError("nothing to set — pass summary and/or indexed_summary")
    own = session is None
    with shared_ingest_lock(), use_session(session) as s:
        t = s.get(Thread, thread_id)
        if t is None:
            raise ValueError(f"no thread with id {thread_id}")
        if t.thread_type == "topic":
            raise ValueError(f"thread {thread_id} is a topic — topics carry a description")
        fields = []
        if summary:
            t.summary = summary.strip()
            fields.append("summary")
        if indexed_summary:
            t.indexed_summary = indexed_summary.strip()
            fields.append("indexed_summary")
        t.updated_at = _now()
        record_thread(s, t)
        s.flush()
        from thread_archive._retrieval.fts import index_thread_meta

        index_thread_meta(s, [thread_id])
        result = {"thread_id": thread_id, "fields": fields}
        if own:
            s.commit()
    return result
