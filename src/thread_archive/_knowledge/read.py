"""The knowledge-graph read API — getting curated topics *back out*.

The write layer (a separate curation package's) accumulates topics, links,
and citations; this module
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

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .._store import Thread, ThreadLink, TopicMessage, use_session

# The hierarchy vocabulary: the two link types the topic tree is built from.
# Data-plane constants — the gardener's structural queues share them from here.
HIERARCHY_UP = "part-of"
HIERARCHY_DOWN = "contains"


def _require_topic(session: Session, topic_id: str) -> Thread:
    topic = session.get(Thread, topic_id)
    if topic is None or topic.thread_type != "topic":
        raise ValueError(f"no topic with id {topic_id}")
    return topic


def topic_get(topic_id: str, *, session: Optional[Session] = None) -> dict:
    """One topic with everything attached: metadata, links (both directions),
    citation count, and member threads. Graph metadata and community peers are
    filled in when the ``thread-librarian`` package (the analytics owner) is
    installed; without it ``graph`` is None and ``peers`` is empty.

    Raises ``ValueError`` when the id isn't a topic (conversations have
    ``thread_read``)."""
    try:
        from thread_librarian.graph import get_community_peers, get_topic_graph_meta
    except ImportError:
        get_community_peers = get_topic_graph_meta = None  # type: ignore[assignment]

    with use_session(session) as s:
        t = _require_topic(s, topic_id)
        links: list[dict] = []
        other = Thread.__table__.alias("other")
        for direction, own_col, other_col in (
            ("out", ThreadLink.source_thread_id, ThreadLink.target_thread_id),
            ("in", ThreadLink.target_thread_id, ThreadLink.source_thread_id),
        ):
            rows = s.execute(
                select(ThreadLink.link_type, ThreadLink.strength, ThreadLink.evidence,
                       other.c.id, other.c.title, other.c.name, other.c.thread_type)
                .join(other, other.c.id == other_col)
                .where(own_col == topic_id)
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
            .where(TopicMessage.topic_id == topic_id,
                   TopicMessage.archived_at.is_(None))
        ).scalar_one()
        member = Thread.__table__.alias("member")
        member_threads = [
            {"thread_id": r.thread_id, "title": r.title or r.name, "citations": r.n}
            for r in s.execute(
                select(TopicMessage.thread_id, member.c.title, member.c.name,
                       func.count().label("n"))
                .join(member, member.c.id == TopicMessage.thread_id)
                .where(TopicMessage.topic_id == topic_id,
                       TopicMessage.archived_at.is_(None))
                .group_by(TopicMessage.thread_id, member.c.title, member.c.name)
                .order_by(func.count().desc(), TopicMessage.thread_id)
            ).all()
        ]
        detail: dict[str, Any] = {
            "id": t.id,
            "title": t.title or t.name,
            "topic_kind": t.topic_kind,
            "description": t.description,
            "archived": bool(t.archived),
            "citation_count": citation_count,
            "member_threads": member_threads,
        }
    detail["links"] = links
    detail["graph"] = get_topic_graph_meta(topic_id) if get_topic_graph_meta else None
    detail["peers"] = get_community_peers(topic_id, limit=8) if get_community_peers else []
    return detail


def topic_members(
    topic_id: str, *, limit: int = 200, session: Optional[Session] = None,
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
            .where(TopicMessage.topic_id == topic_id,
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


def topic_thread_ids(topic_id: str, *, session: Optional[Session] = None) -> list[str]:
    """The conversation threads a topic covers: every thread with a live citation
    under the topic, plus every *conversation* thread directly linked to it (either
    direction). This is the scope ``search(topic_id=...)`` restricts to.

    Raises ``ValueError`` when the id isn't a topic."""
    with use_session(session) as s:
        _require_topic(s, topic_id)
        ids = {
            r[0] for r in s.execute(
                select(TopicMessage.thread_id).distinct()
                .where(TopicMessage.topic_id == topic_id,
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
                    .where(own_col == topic_id,
                           other.c.thread_type != "topic")
                )
            )
    return sorted(ids)


def topic_tree(*, session: Optional[Session] = None) -> dict:
    """The derived topic hierarchy: a forest over live topics' part-of/contains
    links. Roots are parents that are no one's child. A child with several
    parents appears under each (it's a DAG rendered as a tree); a cycle is cut
    at the edge that would revisit an ancestor. Conversations and archived
    topics never enter — an edge to one simply doesn't shape the tree.

    Returns ``{"roots": [...], "topics_in_hierarchy": N, "topics_total": M}``;
    each node is ``{"id", "title", "topic_kind", "children": [...]}``. Shared
    by the web viewer's tree endpoint and ``thread_read('topics')``."""
    with use_session(session) as s:
        live = {
            r.id: {"id": r.id, "title": r.title or r.name, "topic_kind": r.topic_kind}
            for r in s.execute(
                select(Thread.id, Thread.title, Thread.name, Thread.topic_kind)
                .where(Thread.thread_type == "topic")
                .where(Thread.archived.is_(False))
            )
        }
        rows = s.execute(
            select(ThreadLink.source_thread_id, ThreadLink.target_thread_id, ThreadLink.link_type)
            .where(ThreadLink.link_type.in_((HIERARCHY_UP, HIERARCHY_DOWN)))
        ).all()

    children: dict[str, set[str]] = {}
    for src, tgt, link_type in rows:
        child, parent = (src, tgt) if link_type == HIERARCHY_UP else (tgt, src)
        if child == parent or child not in live or parent not in live:
            continue
        children.setdefault(parent, set()).add(child)

    child_ids = {c for kids in children.values() for c in kids}

    def build(tid: str, ancestors: frozenset) -> dict:
        node = dict(live[tid])
        kids = sorted(
            children.get(tid, set()) - ancestors,
            key=lambda c: ((live[c]["title"] or "").lower(), c),
        )
        node["children"] = [build(c, ancestors | {tid}) for c in kids]
        return node

    def weight(node: dict) -> int:
        return 1 + sum(weight(c) for c in node["children"])

    forest = [build(r, frozenset()) for r in sorted(set(children) - child_ids)]
    forest.sort(key=lambda n: (-weight(n), (n["title"] or "").lower(), n["id"]))
    return {
        "roots": forest,
        "topics_in_hierarchy": len((child_ids | set(children)) & set(live)),
        "topics_total": len(live),
    }
