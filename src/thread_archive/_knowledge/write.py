"""The curatorial write API — the librarian's hands on the archive.

Thin, typed functions over the event-sourced knowledge layer. Each *graph* mutation
does the same three things atomically: append a ``KgEvent`` to the truth log, fold it
into the projection (:func:`thread_archive._knowledge.materialize.apply_event`), and —
when it owns the session — commit and drop the graph cache. One call = a durable log
line + an updated projection. The conversation events stay untouched; this writes the
knowledge overlay (topics, links, citations) that sits beside them, plus one piece of
thread *metadata*: the stored summaries (:func:`set_thread_summary`) the librarian
writes alongside its citations.

Topics are threads (``thread_type='topic'``), so :func:`create_topic` writes a real
per-thread truth file and the ``topic.created`` event is the audit record; links and
evidence have no owning conversation file, so the ``kg_events.jsonl`` log *is* their
truth. Summaries are deliberately NOT kg events: they are thread metadata like the
title — durable via the thread's own latest-wins truth record — and keeping the
summary text out of ``kg_events.jsonl`` means redaction's thread-meta scrub
(``_ops.redact``) covers every copy without a second leak surface to chase.

Read helpers (:func:`review_queue`, :func:`topic_search`,
:func:`thread_user_messages`) are the curation-read surface the librarian needs to
decide *what* to write — kept here so the MCP and the skill share one library.

All writes are idempotent, so re-running never double-links, double-cites, or stacks
summaries (a re-summarize simply overwrites) — a partial pass is simply redone.
"""

from __future__ import annotations

import functools
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .._store import Event, KgEvent, Thread, ThreadLink, TopicMessage, use_session
from .._truth.jsonl_log import append_kg_event, record_thread, shared_ingest_lock
from .graph import reset_cache
from .materialize import apply_event


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _locked_write(fn):
    """Run a mutation under the shared reindex lock — unconditionally.

    Every mutating function here appends truth (a ``KgEvent``, sometimes a thread
    record) and commits; racing a reindex unlocked, that write can land after the
    rebuild's read point and commit into the database inode the swap replaces.
    A caller-supplied ``session`` gets the lock too — nested shared holds coexist
    (flock SH + SH), so there is no unlocked path to misuse. (A session *created*
    before the lock may still ride a pre-swap connection; open sessions inside
    the locked call, which every in-tree caller does.)"""

    @functools.wraps(fn)
    def wrapper(*args, session: Optional[Session] = None, **kwargs):
        with shared_ingest_lock():
            return fn(*args, session=session, **kwargs)

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
    ``(source, target, link_type)``. Both endpoints must exist — the kg log is
    truth, and a link to a nonexistent thread would dangle on every replay."""
    own = session is None
    with use_session(session) as s:
        for tid in (source_id, target_id):
            if s.get(Thread, int(tid)) is None:
                raise ValueError(f"no thread with id {tid} — link endpoints must exist")
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
    ``(topic_id, event_id)``.

    Validated before anything is emitted: the topic must exist (and be a topic),
    the event must exist, and ``thread_id`` must be the cited event's actual
    thread — a citation is a claim about a real message, and the kg log is truth,
    so a bad reference must be rejected here rather than replayed forever."""
    own = session is None
    with use_session(session) as s:
        _require_topic(s, topic_id)
        cited = s.get(Event, int(event_id))
        if cited is None:
            raise ValueError(
                f"no event with id {event_id} — citations must reference a real archived message"
            )
        if int(cited.thread_id) != int(thread_id):
            raise ValueError(
                f"event {event_id} belongs to thread {cited.thread_id}, not {thread_id} — "
                "check which message you meant to cite"
            )
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


# ── stored summaries (thread metadata, not kg events) ─────────────────────────────
# Caps, not targets: the short summary is a search doc (a few dense sentences), the
# indexed summary a structured map with event anchors. A runaway write would bloat
# the FTS/embedding surface it exists to serve.
SUMMARY_MAX_CHARS = 4_000
INDEXED_SUMMARY_MAX_CHARS = 24_000


@_locked_write
def set_thread_summary(
    thread_id: int,
    summary: Optional[str] = None,
    *,
    indexed_summary: Optional[str] = None,
    session: Optional[Session] = None,
) -> dict:
    """Store a thread's summary — the short searchable ``summary`` (a few dense
    sentences) and/or the structured ``indexed_summary`` (markdown with
    ``(event NNN)`` anchors, served by ``thread_read summary='indexed'``).

    Sets only the field(s) passed; a provided field overwrites (re-summarizing is an
    update, not an append). Not a ``KgEvent``: summaries are thread metadata like the
    title — the thread's re-staged truth record (latest-wins on reindex) is their
    durability, and redaction's thread-meta scrub already covers them. The short
    summary becomes a thread-meta search doc immediately (``index_thread_meta``),
    where the embed cohost also picks it up for the semantic arm.

    Topics are refused — a topic's ``description`` (via ``rename_topic``) is its
    summary surface.
    """
    summary = summary.strip() if isinstance(summary, str) else summary
    indexed_summary = indexed_summary.strip() if isinstance(indexed_summary, str) else indexed_summary
    if not summary and not indexed_summary:
        raise ValueError("nothing to set — pass summary and/or indexed_summary (non-empty)")
    if summary and len(summary) > SUMMARY_MAX_CHARS:
        raise ValueError(
            f"summary is {len(summary)} chars (max {SUMMARY_MAX_CHARS}) — it should be "
            "a few dense sentences; put per-section detail in indexed_summary"
        )
    if indexed_summary and len(indexed_summary) > INDEXED_SUMMARY_MAX_CHARS:
        raise ValueError(
            f"indexed_summary is {len(indexed_summary)} chars (max {INDEXED_SUMMARY_MAX_CHARS})"
        )
    own = session is None
    with use_session(session) as s:
        t = s.get(Thread, int(thread_id))
        if t is None:
            raise ValueError(f"no thread with id {thread_id}")
        if t.thread_type == "topic":
            raise ValueError(
                f"thread {thread_id} is a topic — topics carry a description "
                "(topic_rename), not a stored summary"
            )
        fields = []
        if summary:
            t.summary = summary
            fields.append("summary")
        if indexed_summary:
            t.indexed_summary = indexed_summary
            fields.append("indexed_summary")
        t.updated_at = _now()
        record_thread(s, t)  # latest-wins metadata in the thread's truth file
        s.flush()
        # Sync the thread-meta search docs now (diff-based; also drops the stale
        # vector so the embed cohost re-embeds). Imported here like _ops.redact
        # does — the retrieval package is heavier than this module needs at import.
        from .._retrieval.fts import index_thread_meta

        index_thread_meta(s, [int(thread_id)])
        result = {"thread_id": int(thread_id), "fields": fields}
        if own:
            s.commit()
    return result


# ── curation-read surface ────────────────────────────────────────────────────────
def review_queue(
    limit: int = 20, *, exclude_source_id: Optional[str] = None,
    quiet_minutes: int = 60, session: Optional[Session] = None,
) -> list[dict]:
    """Conversation threads with librarian work left — the backlog.

    Event-bearing, non-archived conversation threads still missing either half of the
    librarian's per-thread output — a live topic citation/link, or a stored short
    summary — newest first. Pass ``exclude_source_id`` to drop the caller's own live
    session (its still-growing transcript sits at the top of its own queue otherwise).

    **State is the data, not a ledger.** 'Done' is *derived from the curation a thread
    carries* — a thread leaves the queue the instant it has BOTH its first live topic
    citation (or a link touching it) AND a non-empty stored summary; there is no
    processed-list to keep in sync. That makes the queue idempotent (a half-done
    thread simply reappears) and safe to drain even if two librarian runs briefly
    overlap. The librarian-gate forces both halves per processed thread, so done ⇒
    cited + summarized.

    ``quiet_minutes`` holds back still-ingesting threads: one whose newest event was
    *ingested* inside the window (``recorded_at``, uniform naive-UTC) is likely a live
    session — its citations would be premature and its summary stale on arrival."""
    has_events = select(Event.id).where(Event.thread_id == Thread.id).exists()
    # 'Curated' = the librarian drew something from this thread: a live (non-archived)
    # topic citation sourced from it, or a link touching it.
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
    is_summarized = Thread.summary.isnot(None) & (func.trim(Thread.summary) != "")
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=quiet_minutes)
    recently_ingested = (
        select(Event.id)
        .where(Event.thread_id == Thread.id, Event.recorded_at >= cutoff)
        .exists()
    )
    conds = [
        Thread.thread_type == "conversation",
        or_(~is_curated, ~is_summarized),
        or_(Thread.archived.is_(False), Thread.archived.is_(None)),
        has_events,
        ~recently_ingested,
    ]
    if exclude_source_id:
        conds.append(or_(Thread.source_id.is_(None), Thread.source_id != exclude_source_id))
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
