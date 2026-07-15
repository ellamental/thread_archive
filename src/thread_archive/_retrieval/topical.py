"""Topic-bridged recall arm — the subject graph as a retrieval expander.

thread's premise is in its name: subjects ("topics") are the threads that weave
*between* conversations. The librarian links each conversation message to the
subjects it touches (``topic_messages``); this arm walks those links to raise
recall. Given the seed hits the lexical + vector arms already found, it follows
each seed conversation to the subjects it's evidenced under — optionally out to
those subjects' Leiden community peers, the sibling subjects — then pulls the
*other* conversations evidenced under that subject set into the candidate pool.

The payoff is the recall regime FTS and the bi-encoder can't reach: a
conversation that shares a subject with a strong hit but shares none of the
query's words is invisible to a term index and mid-list to an embedding, yet it
is one curated topic-link hop from a hit. This arm makes that hop.

Its output is a ranked :class:`EventHit` list that joins the RRF fusion as a
third arm (the same seam the vector arm uses), so a subject-linked chat rides the
ranker's ``fusion_weight`` exactly as a semantic-only hit does. Fail-soft: any
error, an empty subject graph, or no seed yields no hits and search continues
byte-for-byte as if the arm weren't here.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from .._store import use_session
from ._types import EventHit
from .fts import _in_clause, build_event_hit

logger = logging.getLogger(__name__)

# How many top seed hits to follow to subjects. Deeper seeds are weak signals of
# what the query is about, and each extra thread widens the subject fan-out.
SEED_DEPTH = 40
# Bridge subjects (highest-weight matched subjects) whose community peers we pull
# in, and how many peers each contributes — bounds the fan-out of a big community.
PEER_BRIDGE_LIMIT = 30
PEER_FANOUT = 12
# A peer subject inherits this fraction of its bridge subject's weight: a sibling
# is a weaker signal than a directly-linked subject.
PEER_DECAY = 0.5
# Subjects carried into the evidence query, highest-weight first.
MAX_TOPICS = 80
# The most candidate events the arm contributes (bounds the third RRF list).
MAX_CANDIDATES = 200


def enabled() -> bool:
    """Master switch. On by default; ``THREAD_ARCHIVE_TOPICAL=0`` forces the arm
    out (the baseline for an A/B on the retrieval eval, and the prod kill switch)."""
    return os.environ.get("THREAD_ARCHIVE_TOPICAL", "1") != "0"


def _peers_enabled() -> bool:
    """Whether to widen matched subjects to their Leiden community peers.
    ``THREAD_ARCHIVE_TOPICAL_PEERS=0`` keeps the arm to directly-linked subjects."""
    return os.environ.get("THREAD_ARCHIVE_TOPICAL_PEERS", "1") != "0"


def _subject_weights_from_seed(seed: list[EventHit], session: Session) -> dict[int, float]:
    """Map the seed pool's conversations to the subjects they're evidenced under,
    weighting each subject by how highly its conversations ranked. A subject linked
    from several strong hits outweighs one linked from a single weak hit."""
    seed_threads: dict[int, float] = {}
    for rank, h in enumerate(seed[:SEED_DEPTH]):
        tid = h.get("thread_id")
        if tid is None:
            continue
        w = 1.0 / (rank + 1)
        if w > seed_threads.get(tid, 0.0):
            seed_threads[tid] = w
    if not seed_threads:
        return {}

    params: dict = {}
    where = _in_clause("thread_id", list(seed_threads), "st", params, negate=False)
    rows = session.execute(
        sa_text(
            "SELECT DISTINCT topic_id, thread_id FROM topic_messages "
            "WHERE archived_at IS NULL AND " + where
        ),
        params,
    ).all()
    topic_w: dict[int, float] = {}
    for topic_id, thread_id in rows:
        topic_w[topic_id] = topic_w.get(topic_id, 0.0) + seed_threads.get(thread_id, 0.0)
    return topic_w


def _widen_to_peers(topic_w: dict[int, float]) -> dict[int, float]:
    """Add each bridge subject's Leiden community peers, at a decayed weight.
    Fail-soft: an unavailable / empty subject graph leaves ``topic_w`` untouched."""
    try:
        from .._knowledge import community_members_for
    except Exception:  # noqa: BLE001 — the knowledge layer is optional; never break search
        return topic_w
    bridges = sorted(topic_w, key=lambda t: (-topic_w[t], t))[:PEER_BRIDGE_LIMIT]
    peers = community_members_for(bridges)
    if not peers:
        return topic_w
    widened = dict(topic_w)
    for tid in bridges:
        inherit = topic_w[tid] * PEER_DECAY
        for member in peers.get(tid, [])[:PEER_FANOUT]:
            if inherit > widened.get(member, 0.0):
                widened[member] = inherit
    return widened


def _gather_candidates(
    topic_w: dict[int, float],
    *,
    seen_keys: set,
    content_types: Optional[list[str]],
    exclude_content_types: Optional[list[str]],
    since: Optional[str],
    until: Optional[str],
    source: Optional[list[str]],
    session: Session,
) -> list[EventHit]:
    """Pull the evidence under the top-weighted subjects — the conversations that
    share a subject with the seed — as ranked hits. Honors the same scope filters
    as the lexical arm (content-type, search blacklist, time window, provider) and
    skips events already in the seed pool, so the arm only ever *adds* recall."""
    topics = sorted(topic_w, key=lambda t: (-topic_w[t], t))[:MAX_TOPICS]
    if not topics:
        return []

    params: dict = {}
    clauses = [
        "tm.archived_at IS NULL",
        _in_clause("tm.topic_id", topics, "tp", params, negate=False),
        # The FTS content lives on the events_fts shadow (indexed on event_id);
        # occurred_at comes from the events row (PK join). Both joins are indexed.
        "f.thread_id NOT IN (SELECT id FROM threads WHERE exclude_from_search)",
    ]
    if content_types:
        clauses.append(_in_clause("f.content_type", content_types, "ct", params, negate=False))
    if exclude_content_types:
        clauses.append(_in_clause("f.content_type", exclude_content_types, "xct", params, negate=True))
    if since:
        clauses.append("e.occurred_at >= :since")
        params["since"] = since
    if until:
        clauses.append("e.occurred_at <= :until")
        params["until"] = until
    if source:
        clauses.append(
            "f.thread_id IN (SELECT id FROM threads WHERE "
            + _in_clause("source", source, "src", params, negate=False) + ")"
        )

    # Subject specificity (IDF-style): a broad subject evidenced across many chats
    # links everything and means little — like a stopword — so its vouch is damped;
    # a narrow subject genuinely means "the same thing". Breadth = distinct chats.
    breadth_params: dict = {}
    breadth: dict[int, int] = {
        int(r[0]): int(r[1])
        for r in session.execute(
            sa_text(
                "SELECT topic_id, COUNT(DISTINCT thread_id) FROM topic_messages "
                "WHERE archived_at IS NULL AND "
                + _in_clause("topic_id", topics, "bt", breadth_params, negate=False)
                + " GROUP BY topic_id"
            ),
            breadth_params,
        ).all()
    }
    spec = {t: 1.0 / (1.0 + math.log(1 + breadth.get(t, 1))) for t in topics}

    sql = sa_text(
        "SELECT tm.topic_id, f.event_id, f.thread_id, f.event_type, f.content_type, "
        "e.occurred_at, f.content "
        "FROM topic_messages tm "
        "JOIN events_fts f ON f.event_id = tm.event_id "
        "JOIN events e ON e.id = f.event_id "
        "WHERE " + " AND ".join(clauses)
    )
    rows = session.execute(sql, params).all()

    # One hit per (event_id, content_type) — the RRF fusion key — scored by the
    # strongest subject that reaches it. A message evidenced under two matched
    # subjects takes the higher weight, not the sum: it's one chat, one hit.
    best: dict = {}
    for topic_id, event_id, thread_id, event_type, content_type, occurred_at, content in rows:
        key = (event_id, content_type)
        if key in seen_keys:
            continue
        score = topic_w.get(topic_id, 0.0) * spec.get(topic_id, 1.0)
        cur = best.get(key)
        if cur is None or score > cur[0]:
            best[key] = (score, event_id, thread_id, event_type, content_type, occurred_at, content)

    # Order by subject weight, most-recent-first within a tie (stable two-pass).
    items = list(best.values())
    items.sort(key=lambda r: str(r[5] or ""), reverse=True)
    items.sort(key=lambda r: r[0], reverse=True)

    out: list[EventHit] = []
    for score, event_id, thread_id, event_type, content_type, occurred_at, content in items[:MAX_CANDIDATES]:
        hit = build_event_hit(
            event_id=event_id,
            thread_id=thread_id,
            event_type=event_type,
            content_type=content_type,
            snippet=(content or "")[:300],
            full_content=content or "",
            occurred_at=str(occurred_at) if occurred_at is not None else None,
        )
        hit["_topical"] = round(float(score), 6)
        out.append(hit)
    return out


def topical_hits(
    seed: list[EventHit],
    *,
    content_types: Optional[list[str]] = None,
    exclude_content_types: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    source: Optional[list[str]] = None,
    session: Optional[Session] = None,
) -> list[EventHit]:
    """Follow the seed pool's conversations through the subject graph and return the
    subject-linked conversations FTS and the vector arm couldn't reach, ranked for
    the RRF fusion. Empty (a no-op) when there's no seed, no subject graph, or the
    arm is disabled; fail-soft on any error so it can never break search."""
    if not seed:
        return []
    try:
        with use_session(session) as s:
            topic_w = _subject_weights_from_seed(seed, s)
            if not topic_w:
                return []
            if _peers_enabled():
                topic_w = _widen_to_peers(topic_w)
            seen_keys = {(h["event_id"], h.get("content_type")) for h in seed}
            return _gather_candidates(
                topic_w,
                seen_keys=seen_keys,
                content_types=content_types,
                exclude_content_types=exclude_content_types,
                since=since,
                until=until,
                source=source,
                session=s,
            )
    except Exception:  # noqa: BLE001 — the topic arm must never break lexical/vector search
        logger.exception("topical arm failed; search continues without it")
        return []
