"""The materializer: fold topic-graph events into the knowledge projection.

A ``KgEvent`` is the unit of topic-graph truth; this module is the **fold** that turns
the append-only event log into the ``thread_links`` / ``topic_messages`` SQLite
tables the topic graph reads. It is the serverless analogue of a streaming
materializer — but in-process, folding to SQLite rather than to a graph server.

:func:`apply_event` dispatches one event to its projection mutation. It is called
in two places, both with the session it must mutate:

* **live write** — :mod:`thread_archive._knowledge.write` appends an event *and*
  applies it in the same transaction, so one call yields a durable log line plus an
  updated projection;
* **reindex replay** — ``truth.jsonl_log`` replays the whole log in ``id`` order to
  rebuild the projection from scratch (on top of any legacy snapshot seed).

Every mutation is **upsert + tombstone**, never blind insert: ``link.created`` on an
existing edge updates it, ``link.deleted`` removes it whether it came from the log or
a seed snapshot, ``evidence.archived`` stamps rather than deletes. That is what makes
the replay idempotent and order-stable — applying the same log twice, or folding a
delta onto a seed, lands on the same projection.

Timestamps come from the event (``occurred_at``), not the moment of replay, so a
rebuild is deterministic: a link's ``created_at`` is when it was *made*, every time.
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import delete, select, update

from .._store import Event, Thread, ThreadLink, TopicMessage

logger = logging.getLogger(__name__)


def apply_event(session, ev) -> None:
    """Fold one ``KgEvent`` into the projection on ``session`` (no commit).

    ``ev`` is any object exposing ``event_type`` / ``payload`` / ``actor`` /
    ``actor_thread_id`` / ``occurred_at`` (a live ``KgEvent`` or a transient one
    rebuilt from a log row). Unknown event types are logged and skipped — a forward-
    compatible reader never aborts a replay on an event it doesn't understand."""
    fn = _DISPATCH.get(ev.event_type)
    if fn is None:
        logger.warning("kg materialize: unknown event_type %r — skipped", ev.event_type)
        return
    fn(session, ev.payload or {}, ev)


# ── topics (topics are threads; the event log carries audit + lifecycle) ──────
def _topic_created(session, p: dict, ev) -> None:
    # The topic thread itself is created + recorded by the write path (it has its own
    # per-thread truth file); this event is the audit record. Nothing to project.
    return


def _topic_renamed(session, p: dict, ev) -> None:
    fields = {k: p[k] for k in ("title", "description") if k in p}
    if not fields:
        return
    _update_thread(session, ev.entity_id, fields)


def _topic_updated(session, p: dict, ev) -> None:
    fields = p.get("fields", p)
    allowed = {k: v for k, v in fields.items()
               if k in ("title", "description", "topic_kind", "epistemological_type", "search_description")}
    if allowed:
        _update_thread(session, ev.entity_id, allowed)


def _topic_archived(session, p: dict, ev) -> None:
    _update_thread(session, ev.entity_id, {"archived": True})


def _topic_merged(session, p: dict, ev) -> None:
    """Repoint every link + evidence from ``from_id`` onto ``into_id``, then archive
    ``from_id``. Repointing can collide with an existing edge/evidence (unique keys),
    so it is done row-by-row with a duplicate guard rather than a blind UPDATE."""
    from_id, into_id = p["from_id"], p["into_id"]
    if from_id == into_id:
        return
    _repoint_links(session, from_id, into_id)
    _repoint_evidence(session, from_id, into_id)
    _update_thread(session, from_id, {"archived": True})


# ── links (the topic-graph edge set) ──────────────────────────────────────────
def _link_created(session, p: dict, ev) -> None:
    src, tgt, lt = p["source_thread_id"], p["target_thread_id"], p.get("link_type", "related")
    row = _find_link(session, src, tgt, lt)
    if row is None:
        row = ThreadLink(source_thread_id=src, target_thread_id=tgt, link_type=lt, created_at=ev.occurred_at)
        session.add(row)
    row.strength = float(p.get("strength", 1.0))
    row.evidence = p.get("evidence")
    row.created_by = p.get("created_by") or getattr(ev, "actor", "librarian")
    row.created_by_thread_id = getattr(ev, "actor_thread_id", None)
    row.updated_at = ev.occurred_at


def _link_updated(session, p: dict, ev) -> None:
    row = _find_link(session, p["source_thread_id"], p["target_thread_id"],
                     p.get("link_type", "related"))
    if row is None:
        return
    for k, v in (p.get("fields") or {}).items():
        if k in ("strength", "evidence", "link_type"):
            setattr(row, k, v)
    row.updated_at = ev.occurred_at


def _link_deleted(session, p: dict, ev) -> None:
    session.execute(delete(ThreadLink).where(
        ThreadLink.source_thread_id == p["source_thread_id"],
        ThreadLink.target_thread_id == p["target_thread_id"],
        ThreadLink.link_type == p.get("link_type", "related"),
    ))


# ── evidence (message → topic citations) ──────────────────────────────────────
def _evidence_added(session, p: dict, ev) -> None:
    topic_id, event_id = p["topic_id"], int(p["event_id"])
    # The cited event's own row is authoritative for the thread; the payload
    # value is the fallback for a citation whose event the store doesn't hold
    # (a dangling reference replayed from an older, unvalidated write). Looked
    # up before the row is added — a query would autoflush the incomplete row.
    cited = session.get(Event, event_id)
    row = _find_evidence(session, topic_id, event_id)
    if row is None:
        row = TopicMessage(topic_id=topic_id, event_id=event_id, created_at=ev.occurred_at)
        session.add(row)
    row.thread_id = cited.thread_id if cited is not None else p["thread_id"]
    row.quote = p.get("quote", "")
    row.actor = p.get("actor") or getattr(ev, "actor", "librarian")
    row.created_by_thread_id = getattr(ev, "actor_thread_id", None)
    row.archived_at = None


def _evidence_archived(session, p: dict, ev) -> None:
    session.execute(update(TopicMessage).where(
        TopicMessage.topic_id == p["topic_id"],
        TopicMessage.event_id == int(p["event_id"]),
    ).values(archived_at=ev.occurred_at))


# ── helpers ───────────────────────────────────────────────────────────────────
def _update_thread(session, thread_id: str, fields: dict) -> None:
    session.execute(update(Thread).where(Thread.id == thread_id).values(**fields))


def _find_link(session, src: str, tgt: str, link_type: str):
    return session.execute(select(ThreadLink).where(
        ThreadLink.source_thread_id == src,
        ThreadLink.target_thread_id == tgt,
        ThreadLink.link_type == link_type,
    )).scalars().first()


def _find_evidence(session, topic_id: str, event_id: int):
    return session.execute(select(TopicMessage).where(
        TopicMessage.topic_id == topic_id,
        TopicMessage.event_id == event_id,
    )).scalars().first()


def _repoint_links(session, from_id: str, into_id: str) -> None:
    links = session.execute(select(ThreadLink).where(
        (ThreadLink.source_thread_id == from_id) | (ThreadLink.target_thread_id == from_id)
    )).scalars().all()
    for link in links:
        new_src = into_id if link.source_thread_id == from_id else link.source_thread_id
        new_tgt = into_id if link.target_thread_id == from_id else link.target_thread_id
        if new_src == new_tgt:  # a self-loop after merge — drop it
            session.delete(link)
            continue
        if _find_link(session, new_src, new_tgt, link.link_type) is not None:
            session.delete(link)  # the merged-into edge already exists — collapse
            continue
        link.source_thread_id, link.target_thread_id = new_src, new_tgt


def _repoint_evidence(session, from_id: str, into_id: str) -> None:
    rows = session.execute(
        select(TopicMessage).where(TopicMessage.topic_id == from_id)
    ).scalars().all()
    for row in rows:
        if _find_evidence(session, into_id, row.event_id) is not None:
            session.delete(row)  # citation already exists on the merged-into topic
            continue
        row.topic_id = into_id


_DISPATCH: dict[str, Callable] = {
    "topic.created": _topic_created,
    "topic.renamed": _topic_renamed,
    "topic.updated": _topic_updated,
    "topic.archived": _topic_archived,
    "topic.merged": _topic_merged,
    "link.created": _link_created,
    "link.updated": _link_updated,
    "link.deleted": _link_deleted,
    "evidence.added": _evidence_added,
    "evidence.archived": _evidence_archived,
}
