"""Query-less browse: the thread-granular list view behind an empty search.

An empty ``thread_search`` query is a *browse* — "what threads happened
lately", "list recent cursor sessions" — so the result unit is a thread, not
an event: one row per thread, ordered by last activity (the newest event's
``occurred_at``, falling back to the row's ``updated_at`` for event-less
threads; the raw ``updated_at`` alone can't mean "recently active" — it is the
truth checkpoint's dirty-flag, bumped by any metadata write). The structural
search filters compose: ``since``/``until`` window the last activity,
``source`` restricts providers, ``types`` picks ``thread_type`` values
(without it, topics and system threads — subagent runs — are hidden, the same
default as the web viewer's recent list), a resolved ``thread_ids`` scope
(e.g. a topic's members) restricts the population, and ``oldest_first`` flips
the order for "where did this start".

Rows come back as :class:`EventHit` dicts flagged ``_browse=True`` so the
ordinary pipeline — renderer, usage ledger, ``output='linkable'`` — consumes
them unchanged. ``event_id`` is the thread's newest event, a ready anchor for
``thread_read(thread_id, around_event=…)`` to open a thread at its tail.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, func, select
from sqlalchemy.orm import Session

from .._store import Event, Thread, use_session
from ._types import EventHit

# Hidden from a default browse (mirrors the web recent list): topics are
# curated artifacts (thread_read(topic_id) keeps rendering one) and
# 'system' threads (Task-tool subagent runs) are machinery, not sessions anyone
# revisits by recency. Both stay reachable through an explicit ``types``.
DEFAULT_HIDDEN_TYPES = ("topic", "system")


def _time_bound(value: Optional[str]) -> Optional[datetime]:
    """A resolved since/until bound as a datetime for ORM comparison; ``None``
    for an unparseable value (the bound is dropped rather than raising —
    matching the lexical pass-through the FTS path gives such values)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def browse_threads(
    *,
    limit: int = 10,
    since: Optional[str] = None,
    until: Optional[str] = None,
    source: Optional[list[str]] = None,
    types: Optional[list[str]] = None,
    agents: str = "exclude",
    thread_id: Optional[str] = None,
    thread_ids: Optional[list[str]] = None,
    oldest_first: bool = False,
    session: Optional[Session] = None,
) -> list[EventHit]:
    """List threads by last activity — the engine behind an empty-query search.

    Archived threads never list. The per-thread search blacklist
    (``threads.exclude_from_search``) is honored except under an explicit
    ``thread_id``/``thread_ids`` scope, mirroring keyword search.

    ``agents`` mirrors keyword search's switch over agent-run threads
    (``thread_type='system'``): 'exclude' (default) hides them alongside topics,
    'include' lists them among the conversations (topics stay hidden), 'only'
    lists nothing else. An explicit ``types`` list wins over ``agents``."""
    newest_event_at = (
        select(Event.occurred_at)
        .where(Event.thread_id == Thread.id)
        .order_by(Event.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    newest_event_id = (
        select(Event.id)
        .where(Event.thread_id == Thread.id)
        .order_by(Event.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    n_events = (
        select(func.count()).where(Event.thread_id == Thread.id).scalar_subquery()
    )
    last_active = func.coalesce(
        newest_event_at, Thread.updated_at, type_=DateTime(timezone=True)
    ).label("last_active")

    stmt = (
        select(Thread.id, Thread.title, Thread.name, Thread.source,
               Thread.thread_type, last_active,
               newest_event_id.label("newest_event_id"), n_events.label("n_events"))
        .where(Thread.archived.is_(False))
    )
    if thread_id is not None:
        stmt = stmt.where(Thread.id == thread_id)
    elif thread_ids is not None:
        if not thread_ids:
            return []
        stmt = stmt.where(Thread.id.in_(thread_ids))
    else:
        stmt = stmt.where(Thread.exclude_from_search.is_(False))
    if types:
        stmt = stmt.where(Thread.thread_type.in_(types))
    elif thread_id is None and thread_ids is None:
        if agents == "only":
            stmt = stmt.where(Thread.thread_type == "system")
        elif agents == "include":
            stmt = stmt.where(Thread.thread_type != "topic")
        else:
            stmt = stmt.where(Thread.thread_type.not_in(DEFAULT_HIDDEN_TYPES))
    if source:
        stmt = stmt.where(Thread.source.in_(source))
    since_dt, until_dt = _time_bound(since), _time_bound(until)
    if since_dt is not None:
        stmt = stmt.where(last_active >= since_dt)
    if until_dt is not None:
        stmt = stmt.where(last_active <= until_dt)
    stmt = stmt.order_by(
        last_active.asc() if oldest_first else last_active.desc()
    ).limit(max(1, limit))

    with use_session(session) as s:
        rows = s.execute(stmt).all()

    hits: list[EventHit] = []
    for r in rows:
        title = r.title or r.name or "(untitled)"
        hits.append({
            "event_id": r.newest_event_id or 0,
            "thread_id": r.id,
            "thread_title": title,
            "event_type": "thread",
            "content_type": r.thread_type,
            "snippet": f"{title} · {r.source or '?'} · {r.n_events} events",
            "full_content": title,
            "occurred_at": r.last_active,
            "_browse": True,
            "thread_source": r.source,
            "n_events": r.n_events,
        })
    return hits
