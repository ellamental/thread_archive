"""The knowledge-graph read API — getting curated topics *back out*.

The write layer (:mod:`.write`) accumulates topics, links, and citations; this module
is the library surface that reads them back: one topic with everything attached
(:func:`topic_get`), its citations with quotes (:func:`topic_members`), and the set of
conversation threads a topic covers (:func:`topic_thread_ids`) — the resolver behind
search's ``topic_id`` scope. Shared by the librarian MCP tools and the retrieval
layer so every surface renders the same graph.

Archived citations and links are tombstones (``archived_at`` set) and are excluded
everywhere here; an archived *topic* still reads (a merged-away topic stays referenced
from kg history) — it just carries no live graph metadata.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .._store import Thread, ThreadLink, TopicMessage, use_session


def _require_topic(session: Session, topic_id: int) -> Thread:
    topic = session.get(Thread, int(topic_id))
    if topic is None or topic.thread_type != "topic":
        raise ValueError(f"no topic with id {topic_id}")
    return topic


def topic_get(topic_id: int, *, session: Optional[Session] = None) -> dict:
    """One topic with everything attached: metadata, links (both directions),
    citation count, member threads, graph metadata, and community peers.

    Raises ``ValueError`` when the id isn't a topic (conversations have
    ``thread_read``)."""
    from .graph import get_community_peers, get_topic_graph_meta

    with use_session(session) as s:
        t = _require_topic(s, topic_id)
        links = []
        other = Thread.__table__.alias("other")
        for direction, own_col, other_col in (
            ("out", ThreadLink.source_thread_id, ThreadLink.target_thread_id),
            ("in", ThreadLink.target_thread_id, ThreadLink.source_thread_id),
        ):
            rows = s.execute(
                select(ThreadLink.link_type, ThreadLink.strength, ThreadLink.evidence,
                       other.c.id, other.c.title, other.c.name, other.c.thread_type)
                .join(other, other.c.id == other_col)
                .where(own_col == int(topic_id))
                .order_by(ThreadLink.strength.desc(), other.c.id)
            ).all()
            links.extend(
                {
                    "direction": direction,
                    "other_id": r.id,
                    "other_title": r.title or r.name,
                    "other_type": r.thread_type,
                    "link_type": r.link_type,
                    "strength": r.strength,
                    "evidence": r.evidence,
                }
                for r in rows
            )
        citation_count = s.execute(
            select(func.count()).select_from(TopicMessage)
            .where(TopicMessage.topic_id == int(topic_id),
                   TopicMessage.archived_at.is_(None))
        ).scalar_one()
        member = Thread.__table__.alias("member")
        member_threads = [
            {"thread_id": r.thread_id, "title": r.title or r.name, "citations": r.n}
            for r in s.execute(
                select(TopicMessage.thread_id, member.c.title, member.c.name,
                       func.count().label("n"))
                .join(member, member.c.id == TopicMessage.thread_id)
                .where(TopicMessage.topic_id == int(topic_id),
                       TopicMessage.archived_at.is_(None))
                .group_by(TopicMessage.thread_id, member.c.title, member.c.name)
                .order_by(func.count().desc(), TopicMessage.thread_id)
            ).all()
        ]
        detail = {
            "id": t.id,
            "title": t.title or t.name,
            "topic_kind": t.topic_kind,
            "description": t.description,
            "archived": bool(t.archived),
            "citation_count": citation_count,
            "member_threads": member_threads,
        }
    detail["links"] = links
    detail["graph"] = get_topic_graph_meta(int(topic_id))
    detail["peers"] = get_community_peers(int(topic_id), limit=8)
    return detail


def topic_members(
    topic_id: int, *, limit: int = 200, session: Optional[Session] = None,
) -> list[dict]:
    """A topic's live citations, oldest event first: ``{event_id, thread_id,
    thread_title, quote}``. These are the verbatim receipts behind the topic — each
    ``event_id`` opens in ``thread_read`` via ``around_event``.

    Raises ``ValueError`` when the id isn't a topic."""
    with use_session(session) as s:
        _require_topic(s, topic_id)
        cited = Thread.__table__.alias("cited")
        rows = s.execute(
            select(TopicMessage.event_id, TopicMessage.thread_id, TopicMessage.quote,
                   cited.c.title, cited.c.name)
            .join(cited, cited.c.id == TopicMessage.thread_id)
            .where(TopicMessage.topic_id == int(topic_id),
                   TopicMessage.archived_at.is_(None))
            .order_by(TopicMessage.event_id)
            .limit(limit)
        ).all()
    return [
        {
            "event_id": r.event_id,
            "thread_id": r.thread_id,
            "thread_title": r.title or r.name,
            "quote": r.quote,
        }
        for r in rows
    ]


def topic_thread_ids(topic_id: int, *, session: Optional[Session] = None) -> list[int]:
    """The conversation threads a topic covers: every thread with a live citation
    under the topic, plus every *conversation* thread directly linked to it (either
    direction). This is the scope ``search(topic_id=...)`` restricts to.

    Raises ``ValueError`` when the id isn't a topic."""
    with use_session(session) as s:
        _require_topic(s, topic_id)
        ids = {
            r[0] for r in s.execute(
                select(TopicMessage.thread_id).distinct()
                .where(TopicMessage.topic_id == int(topic_id),
                       TopicMessage.archived_at.is_(None))
            )
        }
        other = Thread.__table__.alias("other")
        for own_col, other_col in (
            (ThreadLink.source_thread_id, ThreadLink.target_thread_id),
            (ThreadLink.target_thread_id, ThreadLink.source_thread_id),
        ):
            ids.update(
                r[0] for r in s.execute(
                    select(other.c.id)
                    .select_from(ThreadLink)
                    .join(other, other.c.id == other_col)
                    .where(own_col == int(topic_id),
                           other.c.thread_type != "topic")
                )
            )
    return sorted(ids)
