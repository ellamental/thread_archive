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
from ._types import EventHit, Results

# Hidden from a default browse (mirrors the web recent list): topics are
# separate artifacts (thread_read(topic_id) keeps rendering one) and
# 'system' threads (Task-tool subagent runs) are machinery, not sessions anyone
# revisits by recency. Both stay reachable through an explicit ``types``.
DEFAULT_HIDDEN_TYPES = ("topic", "system")

#: How many conversations the code-axis browse resolves before it stops. Unlike
#: the rest of this module the code axis is not a plain indexed SELECT — it
#: resolves per-thread op tallies through :func:`.code.blame_path` — so it is the
#: one browse shape whose enumeration can be a floor rather than a total.
_CODE_AXIS_CAP = 2000


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
    preserve_order: bool = False,
    path: Optional[str] = None,
    path_ops: Optional[list[str]] = None,
    oldest_first: bool = False,
    page: int = 1,
    session: Optional[Session] = None,
) -> list[EventHit]:
    """List threads by last activity — the engine behind an empty-query search.

    Archived threads never list. The per-thread search blacklist
    (``threads.exclude_from_search``) is honored except under an explicit
    ``thread_id``/``thread_ids`` scope, mirroring keyword search.

    ``preserve_order`` keeps a caller-supplied ``thread_ids`` in the order it was
    given instead of re-sorting by last activity — for a scope whose *ranking* is
    the answer (the commit scope ranks by share of the commit), where re-sorting
    would put a different session at the top than the one the caller ranked first.

    ``path`` turns the list into the **code-axis** browse: the conversations that
    touched a file, and each row carries what it did to it — the op tally, the first
    and last touch of *that path*, how many files under it the thread matched — with
    ``event_id`` re-pointed at the strongest, newest touch so opening the row lands
    on the edit rather than on the thread's tail. Ordering follows the evidence
    (changes before looks, then recency) instead of last activity, unless
    ``oldest_first`` asks for the chronological view. That is the whole "which
    conversations edited this, and when" answer, in the shape a browse already has.

    ``agents`` mirrors keyword search's switch over agent-run threads
    (``thread_type='system'``): 'exclude' (default) hides them alongside topics,
    'include' lists them among the conversations (topics stay hidden), 'only'
    lists nothing else. An explicit ``types`` list wins over ``agents``.

    ``page`` (1-based) walks the list. This is the shape pagination is exact for:
    the population is a plain indexed SELECT over ``threads``, so the total is one
    ``count(*)`` and each page is an OFFSET into a total order — no candidate pool,
    nothing cut, every row reachable. The result carries that total, so a caller
    can tell the last page from a page that merely came back short. The code axis
    is the one exception: ``path`` resolves its population through
    :func:`.code.blame_path`, which stops at :data:`_CODE_AXIS_CAP`, so a pattern
    that wide reports ``capped`` and is not ``exhaustive``."""
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
            return Results()
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
    # The code-axis scope is resolved rather than subqueried here: the browse row
    # has to carry the per-thread op tally and the touch anchor, and the ordering
    # follows them — none of which a bare id filter could supply.
    code_stats: dict = {}
    if path:
        from .code import blame_path

        blamed = blame_path(path, ops=path_ops, limit=_CODE_AXIS_CAP, agents=agents,
                            sources=source, session=session)
        code_stats = {t["thread_id"]: t for t in blamed["threads"]}
        if not code_stats:
            return Results()
        stmt = stmt.where(Thread.id.in_(list(code_stats)))
    since_dt, until_dt = _time_bound(since), _time_bound(until)
    if since_dt is not None:
        stmt = stmt.where(last_active >= since_dt)
    if until_dt is not None:
        stmt = stmt.where(last_active <= until_dt)
    # The population, before ordering and before the page is cut out of it — what
    # the total counts. Captured here so the count can't drift from the list by
    # picking up a LIMIT the list applies later.
    population = stmt
    stmt = stmt.order_by(last_active.asc() if oldest_first else last_active.desc())
    # A reordered browse must not be truncated by SQL's LIMIT before its own sort
    # runs — the rows that survive would be the wrong ones.
    given_order = list(thread_ids) if (preserve_order and thread_ids) else None
    limit = max(1, limit)
    offset = (max(1, page) - 1) * limit
    if not code_stats and given_order is None:
        stmt = stmt.limit(limit).offset(offset)

    with use_session(session) as s:
        total = int(s.execute(
            select(func.count()).select_from(population.subquery())
        ).scalar_one())
        rows = s.execute(stmt).all()

    hits: list[EventHit] = []
    for r in rows:
        title = r.title or r.name or "(untitled)"
        hit: EventHit = {
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
        }
        stats = code_stats.get(r.id)
        if stats is not None:
            hit["_path_ops"] = stats["ops"]
            hit["_path_first"] = stats["first"]
            hit["_path_last"] = stats["last"]
            hit["_path_files"] = stats["n_paths"]
            hit["_path_sample"] = stats["paths"]
            # Open the row where the file was worked on, not at the thread's tail.
            hit["event_id"] = stats["event_id"] or hit["event_id"]
        hits.append(hit)

    ranking = list(code_stats) if code_stats else given_order
    if ranking is not None:
        order = {tid: i for i, tid in enumerate(ranking)}
        hits.sort(key=lambda h: order.get(h["thread_id"], len(order)))
        if oldest_first:
            hits.reverse()
        hits = hits[offset:offset + limit]
        # The renderer's header names the ordering, and "by last activity" would be
        # a plain untruth about a list the caller ranked.
        for hit in hits:
            hit["_browse_order"] = "given"
    return Results(
        hits, total=total, total_threads=total, page=max(1, page),
        pages=(total + limit - 1) // limit,
        # The code axis resolves its population through blame_path, which stops at
        # _CODE_AXIS_CAP; every other browse shape counts its whole population.
        capped=len(code_stats) >= _CODE_AXIS_CAP,
        exhaustive=len(code_stats) < _CODE_AXIS_CAP,
    )
