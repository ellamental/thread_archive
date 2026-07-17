"""Gardener diagnostics — the structural-health read surface of the topic graph.

The librarian's read helpers answer "which *conversations* still need curation";
this module answers "which *topics* do" — the graph-shape problems that accumulate
as per-thread curation only ever adds: topics nobody linked, topics with no
evidence left, topics outside the hierarchy, near-duplicate titles. The gardener
(the ``/gardener-lite`` skill) drains these queues with the same write tools the
librarian uses (:mod:`.write`); this module is read-only.

Like the librarian's ``review_queue``, state is the data: an issue leaves its
queue the instant the graph no longer exhibits it, so every queue is idempotent
and safe to redrain. The kinds deliberately partition where they overlap — a
singleton is by construction also outside the hierarchy, so ``unparented``
excludes singletons rather than listing the same topic under two names.

The hierarchy vocabulary lives here (``HIERARCHY_UP`` / ``HIERARCHY_DOWN``):
a ``part-of`` link reads child→parent, a ``contains`` link parent→child. The web
viewer's topic tree derives from the same two constants.
"""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .._store import Thread, ThreadLink, TopicMessage, use_session

HIERARCHY_UP = "part-of"
HIERARCHY_DOWN = "contains"

GARDEN_KINDS = ("singleton", "uncited", "unparented", "dupes")

# Near-duplicate titles: minimum token-set Jaccard for a pair to enter the
# `dupes` queue. Candidates, not verdicts — the gardener reads both topics
# before merging.
DUPE_THRESHOLD = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _live_topics(s: Session) -> dict[int, dict]:
    rows = s.execute(
        select(Thread.id, Thread.title, Thread.name, Thread.description)
        .where(
            Thread.thread_type == "topic",
            (Thread.archived.is_(False)) | (Thread.archived.is_(None)),
        )
    ).all()
    return {
        r.id: {"topic_id": r.id, "title": r.title or r.name, "description": r.description}
        for r in rows
    }


def _hierarchy_member_ids(s: Session, live: set[int]) -> set[int]:
    """Topics participating in the derived hierarchy — either endpoint of a
    part-of/contains edge between two distinct live topics (the same edges the
    web viewer's tree is built from)."""
    rows = s.execute(
        select(ThreadLink.source_thread_id, ThreadLink.target_thread_id)
        .where(ThreadLink.link_type.in_((HIERARCHY_UP, HIERARCHY_DOWN)))
    ).all()
    members: set[int] = set()
    for src, tgt in rows:
        if src != tgt and src in live and tgt in live:
            members.update((src, tgt))
    return members


def _uncited_ids(s: Session, live: set[int]) -> set[int]:
    """Live topics with no live (non-tombstoned) citation."""
    cited = {
        r[0] for r in s.execute(
            select(TopicMessage.topic_id).distinct()
            .where(TopicMessage.archived_at.is_(None))
        )
    }
    return live - cited


def _degrees(live: set[int]) -> dict[int, int]:
    """Projection degree per live topic (0 for topics the graph has no edges for)."""
    from .graph import get_topic_graph_metadata

    meta = get_topic_graph_metadata(sorted(live))
    return {tid: (meta.get(tid) or {}).get("degree", 0) for tid in live}


def _title_tokens(title: str) -> frozenset[str]:
    """Lowercased alnum tokens with a poor-man's plural stem — candidates only,
    the gardener verifies before merging."""
    toks = _TOKEN_RE.findall((title or "").lower())
    return frozenset(t[:-1] if len(t) >= 4 and t.endswith("s") else t for t in toks)


def _dupe_pairs(live: dict[int, dict]) -> list[dict]:
    """Near-duplicate title pairs by token-set Jaccard, best-first. Pairs are
    generated via an inverted token index, so only titles sharing a token are
    compared."""
    tokens = {tid: _title_tokens(t["title"]) for tid, t in live.items()}
    by_token: dict[str, list[int]] = {}
    for tid, toks in tokens.items():
        for tok in toks:
            by_token.setdefault(tok, []).append(tid)
    seen: set[tuple[int, int]] = set()
    pairs: list[dict] = []
    for ids in by_token.values():
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                ta, tb = tokens[key[0]], tokens[key[1]]
                if not ta or not tb:
                    continue
                score = len(ta & tb) / len(ta | tb)
                if score >= DUPE_THRESHOLD:
                    pairs.append({
                        "a_id": key[0], "a_title": live[key[0]]["title"],
                        "b_id": key[1], "b_title": live[key[1]]["title"],
                        "score": round(score, 3),
                    })
    pairs.sort(key=lambda p: (-p["score"], p["a_id"], p["b_id"]))
    return pairs


def garden_status(*, session: Optional[Session] = None) -> dict:
    """The gardener's dashboard: per-kind issue counts + hierarchy coverage +
    graph status. Counts, not lists — pull the lists with :func:`garden_queue`."""
    from .graph import get_status

    with use_session(session) as s:
        live = _live_topics(s)
        live_ids = set(live)
        in_hier = _hierarchy_member_ids(s, live_ids)
        uncited = _uncited_ids(s, live_ids)
    degrees = _degrees(live_ids)
    singletons = {tid for tid, d in degrees.items() if d == 0}
    unparented = live_ids - in_hier - singletons
    return {
        "topics": len(live_ids),
        "in_hierarchy": len(in_hier),
        "singletons": len(singletons),
        "uncited": len(uncited),
        "unparented": len(unparented),
        "dupe_pairs": len(_dupe_pairs(live)),
        "graph": get_status(),
    }


def garden_queue(kind: str, limit: int = 20, *, session: Optional[Session] = None) -> list[dict]:
    """One kind's prioritized issue list.

    * ``singleton`` — live topics with no link to any other live topic, most-cited
      first (the best-evidenced islands are the most worth connecting):
      ``{topic_id, title, description, citations}``.
    * ``uncited`` — live topics with no live citation, least-linked first (a
      linkless, citation-less husk is the strongest archive candidate):
      ``{topic_id, title, description, degree}``.
    * ``unparented`` — linked topics outside the part-of/contains hierarchy,
      highest-pagerank first (structure the load-bearing ones first); singletons
      are excluded (they have their own queue): ``{topic_id, title, pagerank,
      degree}``.
    * ``dupes`` — near-duplicate title pairs, best score first:
      ``{a_id, a_title, b_id, b_title, score}``.
    """
    if kind not in GARDEN_KINDS:
        raise ValueError(f"unknown kind {kind!r} — one of {', '.join(GARDEN_KINDS)}")
    with use_session(session) as s:
        live = _live_topics(s)
        live_ids = set(live)
        if kind == "dupes":
            return _dupe_pairs(live)[:limit]
        degrees = _degrees(live_ids)
        if kind == "singleton":
            singles = [tid for tid, d in degrees.items() if d == 0]
            counts: dict[int, int] = dict(
                s.execute(
                    select(TopicMessage.topic_id, func.count())
                    .where(
                        TopicMessage.topic_id.in_(singles),
                        TopicMessage.archived_at.is_(None),
                    )
                    .group_by(TopicMessage.topic_id)
                ).tuples().all()
            ) if singles else {}
            singles.sort(key=lambda t: (-counts.get(t, 0), t))
            return [
                {**live[t], "citations": counts.get(t, 0)} for t in singles[:limit]
            ]
        if kind == "uncited":
            ids = sorted(_uncited_ids(s, live_ids), key=lambda t: (degrees.get(t, 0), t))
            return [
                {**live[t], "degree": degrees.get(t, 0)} for t in ids[:limit]
            ]
        # unparented
        from .graph import get_topic_graph_metadata

        in_hier = _hierarchy_member_ids(s, live_ids)
    ids = [t for t in live_ids - in_hier if degrees.get(t, 0) > 0]
    meta = get_topic_graph_metadata(ids)
    ids.sort(key=lambda t: (-(meta.get(t) or {}).get("pagerank", 0.0), t))
    return [
        {
            "topic_id": t,
            "title": live[t]["title"],
            "pagerank": round((meta.get(t) or {}).get("pagerank", 0.0), 6),
            "degree": degrees.get(t, 0),
        }
        for t in ids[:limit]
    ]
