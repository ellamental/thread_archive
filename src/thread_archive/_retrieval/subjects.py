"""Relevant-subjects lens — the topic graph as orientation over a result set.

Not a ranking input. The topic-bridged recall arm (silently reordering results via
the subject graph) proved a wash against the semantic arm: bi-encoder embeddings
already bridge vocabulary-mismatched siblings, so an explicit topic graph buys no
measurable recall on top. This is the graph's *other* value, the one embeddings
can't supply — a curated, legible ontology of named subjects.

Given the hits a search already returned, this names the subjects they cluster
under: "what is this search about", as a pivot surface. It annotates, never
reorders — so it cannot regress ranking, and a wrong label is cheap and visible
where a silently-buried good result would be expensive and invisible. Coverage
across the result set ranks the subjects; specificity (IDF over how many chats a
subject links corpus-wide) damps the broad, stopword-like subjects that touch
everything. Fail-soft and a strict no-op when the subject graph has nothing for
these hits, so it can never break search.
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
from .fts import _in_clause

logger = logging.getLogger(__name__)

# How many top hits define the subject set. Deeper hits are weaker signals of what
# the result set is *about*, and each adds subject fan-out.
SEED_DEPTH = 40
# How many subjects to surface — a pivot list, not a taxonomy dump.
MAX_SUBJECTS = 6


def enabled() -> bool:
    """Master switch. On by default; ``THREAD_ARCHIVE_SUBJECTS=0`` omits the line."""
    return os.environ.get("THREAD_ARCHIVE_SUBJECTS", "1") != "0"


def subjects_for_results(
    hits: list[EventHit], *, session: Optional[Session] = None, limit: int = MAX_SUBJECTS
) -> list[tuple[str, str, int]]:
    """The subjects that best characterize ``hits`` — ``(topic_id, title, chats)``,
    coverage-ranked and specificity-damped, where ``chats`` is how many of the
    result conversations the subject links. Empty (a no-op) when there's no subject
    evidence for these hits; fail-soft on any error so it never breaks search."""
    if not hits:
        return []
    try:
        with use_session(session) as s:
            return _subjects(hits, s, limit)
    except Exception:  # noqa: BLE001 — the lens must never break search rendering
        logger.exception("relevant-subjects lens failed; omitted from output")
        return []


def _subjects(hits: list[EventHit], s: Session, limit: int) -> list[tuple[str, str, int]]:
    # Rank-decayed coverage weight per result conversation: a subject linked from
    # the top hits characterizes the result set more than one linked from the tail.
    thread_w: dict[str, float] = {}
    for rank, h in enumerate(hits[:SEED_DEPTH]):
        tid = h.get("thread_id")
        if tid is None:
            continue
        w = 1.0 / (rank + 1)
        if w > thread_w.get(tid, 0.0):
            thread_w[tid] = w
    if not thread_w:
        return []

    params: dict = {}
    where = _in_clause("thread_id", list(thread_w), "st", params, negate=False)
    rows = s.execute(
        sa_text(
            "SELECT topic_id, thread_id FROM topic_messages "
            "WHERE archived_at IS NULL AND " + where
        ),
        params,
    ).all()
    if not rows:
        return []

    rank_w: dict[str, float] = {}  # topic_id -> rank-decayed weight (tiebreak only)
    chats: dict[str, set] = {}     # topic_id -> distinct result conversations it links
    for topic_id, thread_id in rows:
        rank_w[topic_id] = rank_w.get(topic_id, 0.0) + thread_w.get(thread_id, 0.0)
        chats.setdefault(topic_id, set()).add(thread_id)
    topics = list(chats)

    # Specificity (IDF): a subject evidenced across many chats corpus-wide links
    # everything and means little — like a stopword — so its coverage is damped.
    bp: dict = {}
    breadth: dict[str, int] = {
        str(r[0]): int(r[1])
        for r in s.execute(
            sa_text(
                "SELECT topic_id, COUNT(DISTINCT thread_id) FROM topic_messages "
                "WHERE archived_at IS NULL AND "
                + _in_clause("topic_id", topics, "bt", bp, negate=False)
                + " GROUP BY topic_id"
            ),
            bp,
        ).all()
    }

    # Coverage is the primary axis — how many of the result conversations the
    # subject links — so the subjects that *characterize* the result set lead;
    # specificity only damps the broad, stopword-like subjects. Rank-decayed
    # weight breaks ties (a subject on the top hits over one on the tail).
    def score(t: str) -> float:
        return len(chats[t]) * (1.0 / (1.0 + math.log(1 + breadth.get(t, 1))))

    ranked = sorted(topics, key=lambda t: (-score(t), -rank_w[t], t))[:limit]

    tp: dict = {}
    titles: dict[str, str] = {
        str(r[0]): r[1]
        for r in s.execute(
            sa_text(
                "SELECT id, COALESCE(title, name) FROM threads WHERE "
                + _in_clause("id", ranked, "tt", tp, negate=False)
            ),
            tp,
        ).all()
    }
    return [(t, titles.get(t) or f"topic {t}", len(chats.get(t, set()))) for t in ranked]


def format_subjects_line(subjects: list[tuple[str, str, int]]) -> Optional[str]:
    """The one-line ``subjects:`` orientation header, or None when there's nothing
    to show. ``(N)`` is how many of the result conversations the subject links.
    Each subject carries its ``[topic <id>]`` so the lens is followable, not just
    legible: the id opens the curated topic page via ``thread_read(topic_id)`` and
    scopes a drill-in via ``thread_search(topic_id=...)``."""
    if not subjects:
        return None
    parts = [f"{title} [topic {tid}] ({chats})" for tid, title, chats in subjects]
    return "  subjects: " + " · ".join(parts)
