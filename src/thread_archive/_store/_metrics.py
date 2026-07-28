"""Incremental token/cost rollup behind the viewer's stats page.

Cost and token counts are recorded inside each ``api_request_completed`` event's
JSON ``payload`` (``input_tokens`` / ``cache_read_tokens`` /
``cache_read_input_tokens`` / ``output_tokens`` / ``thinking_tokens`` / ``cost`` /
``model``). Surveying them straight from
``events`` means JSON-extracting
across hundreds of thousands of fat payloads on a multi-GB index — seconds per pass,
far too slow to run per request. :func:`refresh_metrics` folds only the events past a
global cursor (:class:`MetricsCursor`), so the full survey is paid once and every
refresh after that touches only what landed since.

The fold has two stages, and the split is load-bearing. An event is *not* a request:
Claude Code repeats one response's usage object across every transcript row that
response produced, and those rows can arrive in different watcher polls. So the window
first collapses into :class:`RequestMetric`, one durable row per provider request,
where a duplicate meets the row it duplicates however late it shows up. Only then are
the per-(thread, model) sums in :class:`ThreadMetrics` re-derived from that ledger.
Deriving them from ``events`` instead counts a repeated response once per row.

:class:`ThreadMetrics` is therefore a pure function of the ledger, never an
accumulator, and re-deriving a thread is idempotent — which is what lets a refresh
rebuild just the threads the window touched instead of the whole table.

:class:`ThreadActivity` rides the same window for the stats page's time axis: the first
and last ``occurred_at`` per thread, over *every* event type rather than just the usage
events, so a source that logs no tokens still appears on the timeline. It accumulates
(min/max against what is stored) rather than re-deriving, which is safe because both
bounds are idempotent under a repeated fold.

Correctness rests on the event log being append-only with monotonic ids: folding
``through < id <= upto`` and advancing the cursor to ``upto`` visits each event
exactly once. Two things violate that, and both resolve to the same rebuild — a
reindex rebuilding the log (caught by the cursor running ahead of ``MAX(events.id)``,
since the ids the ledger keys on no longer name the same rows) and a change to the
fold itself (caught by ``projection_version``).

These writes go through a raw core connection (``engine.begin()``), never an
``ArchiveSession`` — the rollup is a derived index projection and must not reach the
JSONL truth drain that ORM sessions carry.
"""

from __future__ import annotations

import threading

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ._base import get_engine

# Payload ``model`` strings that name no real model — dropped when aggregating by
# model (mirrors the reader's placeholder set, _retrieval.read._NON_MODEL_VALUES).
NON_MODELS: tuple[str, ...] = ("", "unknown", "<synthetic>")

# One fold at a time within a process: concurrent /api/stats requests would otherwise
# double-apply the same delta. Cross-process is a non-issue — the viewer runs in the
# watcher's own process, the only writer of this cache.
_refresh_lock = threading.Lock()

# The shape of the projection. Bump it whenever a change to the fold makes sums
# produced by the old definition incomparable with new ones; a cursor carrying an
# older version has its projections discarded and rebuilt rather than added to.
PROJECTION_VERSION = 2

# The provider request a row belongs to. Claude Code repeats one response's usage
# object across several transcript rows; every other source keys on the event id,
# which is unique per row and so groups each event alone.
_REQUEST_KEY = """
        CASE
            WHEN t.source = 'claude-code'
            THEN COALESCE(
                json_extract(e.payload, '$.annotations.message_id'),
                printf('event:%d', e.id)
            )
            ELSE printf('event:%d', e.id)
        END
"""

# The threads the just-folded window can have changed. Scoping the rebuild to these
# is what keeps a refresh proportional to what arrived rather than to archive size.
_TOUCHED = """
        SELECT DISTINCT e.thread_id FROM events e
        WHERE e.event_type = 'api_request_completed'
          AND e.id > :through AND e.id <= :upto
"""

# Collapse the window's events into one canonical row per provider request, then
# carry that row forward. ``MAX`` per field is what absorbs a duplicate: the repeated
# usage objects either match or grow toward the response's final counts, so the
# largest is the true one. The ON CONFLICT repeats the same rule against whatever the
# ledger already holds, so a duplicate arriving in a *later* poll collapses too — the
# reason this ledger is durable rather than a per-batch subquery.
_FOLD_REQUESTS_SQL = text(
    f"""
    INSERT INTO request_metrics (
        thread_id, request_key, month, model,
        input_tokens, cache_read_tokens, output_tokens, thinking_tokens, cost
    )
    SELECT
        e.thread_id,
        {_REQUEST_KEY} AS request_key,
        MIN(strftime('%Y-%m', e.occurred_at)) AS month,
        COALESCE(MAX(COALESCE(json_extract(e.payload, '$.model'), '')), '') AS model,
        COALESCE(MAX(
            CASE
                WHEN COALESCE(
                    CAST(json_extract(e.payload, '$.input_tokens_includes_cache') AS INTEGER),
                    CASE WHEN t.source = 'codex' THEN 1 ELSE 0 END
                ) = 1
                THEN MAX(
                    CAST(json_extract(e.payload, '$.input_tokens') AS INTEGER)
                    - COALESCE(
                        CAST(json_extract(e.payload, '$.cache_read_tokens') AS INTEGER),
                        0
                    ),
                    0
                )
                ELSE CAST(json_extract(e.payload, '$.input_tokens') AS INTEGER)
            END
        ), 0),
        COALESCE(MAX(MAX(
            COALESCE(CAST(json_extract(e.payload, '$.cache_read_tokens') AS INTEGER), 0),
            COALESCE(CAST(json_extract(e.payload, '$.cache_read_input_tokens') AS INTEGER), 0)
        )), 0),
        COALESCE(MAX(CAST(json_extract(e.payload, '$.output_tokens') AS INTEGER)), 0),
        COALESCE(MAX(CAST(json_extract(e.payload, '$.thinking_tokens') AS INTEGER)), 0),
        MAX(CAST(json_extract(e.payload, '$.cost') AS REAL))
    FROM events e
    JOIN threads t ON t.id = e.thread_id
    WHERE e.event_type = 'api_request_completed'
      AND e.id > :through AND e.id <= :upto
    GROUP BY e.thread_id, request_key
    ON CONFLICT(thread_id, request_key) DO UPDATE SET
        model = excluded.model,
        -- The earliest month any row of this request was stamped with. A duplicate
        -- arriving in a later poll carries the same timestamp, so this only matters
        -- for a request straddling a month boundary: it lands where it started.
        month = MIN(
            COALESCE(request_metrics.month, excluded.month),
            COALESCE(excluded.month, request_metrics.month)
        ),
        input_tokens = MAX(request_metrics.input_tokens, excluded.input_tokens),
        cache_read_tokens = MAX(
            request_metrics.cache_read_tokens, excluded.cache_read_tokens
        ),
        output_tokens = MAX(request_metrics.output_tokens, excluded.output_tokens),
        thinking_tokens = MAX(request_metrics.thinking_tokens, excluded.thinking_tokens),
        -- Two-argument MAX() returns null if either side is, which would erase a
        -- recorded cost the moment a duplicate arrived without one.
        cost = CASE
            WHEN excluded.cost IS NULL THEN request_metrics.cost
            WHEN request_metrics.cost IS NULL THEN excluded.cost
            ELSE MAX(request_metrics.cost, excluded.cost)
        END
    """
)

# Re-derive the per-(thread, model) sums for the touched threads from the ledger.
# ``thread_metrics`` is a pure function of ``request_metrics``, never an accumulator,
# so re-folding a window that was already folded cannot drift. ``cost`` sums treating
# a null (a route that reported none, e.g. local models) as 0, while ``cost_requests``
# counts the requests that actually carried one so "$0" stays distinct from "no cost
# recorded".
_CLEAR_TOUCHED_SQL = text(f"DELETE FROM thread_metrics WHERE thread_id IN ({_TOUCHED})")

_REBUILD_TOUCHED_SQL = text(
    f"""
    INSERT INTO thread_metrics (
        thread_id, model, requests,
        input_tokens, cache_read_tokens, output_tokens, thinking_tokens,
        cost, cost_requests
    )
    SELECT
        r.thread_id,
        r.model,
        COUNT(*),
        COALESCE(SUM(r.input_tokens), 0),
        COALESCE(SUM(r.cache_read_tokens), 0),
        COALESCE(SUM(r.output_tokens), 0),
        COALESCE(SUM(r.thinking_tokens), 0),
        COALESCE(SUM(r.cost), 0),
        COUNT(r.cost)
    FROM request_metrics r
    WHERE r.thread_id IN ({_TOUCHED})
    GROUP BY r.thread_id, r.model
    """
)


# When each thread was live, folded over every event type — the stats page's time axis.
# Unlike the sums above this is a genuine accumulator (min/max against what's already
# there), which is safe because both are idempotent: re-folding a window that was already
# folded cannot move an existing bound. The scan is over the id range alone — no
# ``event_type`` filter and no JSON — because a source that logs no usage still has to
# appear on the timeline.
_FOLD_ACTIVITY_SQL = text(
    """
    INSERT INTO thread_activity (thread_id, first_at, last_at)
    SELECT e.thread_id, MIN(e.occurred_at), MAX(e.occurred_at)
    FROM events e
    WHERE e.id > :through AND e.id <= :upto
    GROUP BY e.thread_id
    ON CONFLICT(thread_id) DO UPDATE SET
        first_at = MIN(thread_activity.first_at, excluded.first_at),
        last_at = MAX(thread_activity.last_at, excluded.last_at)
    """
)


def invalidate_metrics(conn) -> None:  # noqa: ANN001 — Connection or Session, both execute()
    """Discard both projections and rewind the cursor so the next refresh rebuilds
    from the event log.

    For callers that change events *behind* the cursor — an amendment rewrites a row
    the append-only fold has already passed, so no future window would ever revisit it.
    """
    conn.execute(text("DELETE FROM thread_metrics"))
    conn.execute(text("DELETE FROM request_metrics"))
    conn.execute(text("DELETE FROM thread_activity"))
    conn.execute(
        text(
            "INSERT OR IGNORE INTO metrics_cursor "
            "(id, through_event_id, projection_version) VALUES (1, 0, :version)"
        ),
        {"version": PROJECTION_VERSION},
    )
    conn.execute(
        text(
            "UPDATE metrics_cursor SET through_event_id = 0, projection_version = :version "
            "WHERE id = 1"
        ),
        {"version": PROJECTION_VERSION},
    )


def refresh_metrics(engine: Engine | None = None) -> None:
    """Bring :class:`ThreadMetrics` and :class:`ThreadActivity` up to date with the
    event log.

    Idempotent and cheap when already current (an indexed ``MAX(id)`` read, then a
    no-op). The first call on a fresh cache — and the call after a reindex — pays the
    full survey once; every call after that folds only newly-arrived events and
    re-derives only the threads those events touched.
    """
    engine = engine or get_engine()
    with _refresh_lock, engine.begin() as conn:
        upto = conn.execute(text("SELECT MAX(id) FROM events")).scalar()
        if upto is None:
            return  # empty archive — nothing to roll up
        conn.execute(
            text(
                "INSERT OR IGNORE INTO metrics_cursor "
                "(id, through_event_id, projection_version) VALUES (1, 0, :version)"
            ),
            {"version": PROJECTION_VERSION},
        )
        cursor = conn.execute(
            text(
                "SELECT through_event_id, projection_version "
                "FROM metrics_cursor WHERE id = 1"
            )
        ).first()
        through = int(cursor[0] or 0) if cursor else 0
        version = int(cursor[1] or 0) if cursor else 0
        if version != PROJECTION_VERSION or through > upto:
            # Either the standing sums were folded by an older definition of the
            # projection, or the log shrank below the cursor — a reindex rebuilt it,
            # so the event ids the ledger keys on no longer name the same rows.
            # Neither can be reconciled with an incremental fold; rebuild instead.
            invalidate_metrics(conn)
            through = 0
        if through >= upto:
            return  # already current
        params = {"through": through, "upto": upto}
        conn.execute(_FOLD_REQUESTS_SQL, params)
        conn.execute(_CLEAR_TOUCHED_SQL, params)
        conn.execute(_REBUILD_TOUCHED_SQL, params)
        conn.execute(_FOLD_ACTIVITY_SQL, params)
        conn.execute(
            text(
                "UPDATE metrics_cursor "
                "SET through_event_id = :upto, projection_version = :version "
                "WHERE id = 1"
            ),
            {"upto": upto, "version": PROJECTION_VERSION},
        )


def _avg(total: float, n: int) -> float | None:
    """Mean over ``n`` items, or None when there are none (so the viewer can render an
    honest '—' instead of a fabricated 0 for a source that recorded no such data)."""
    return total / n if n else None


def _month_range(months: list[str]) -> list[str]:
    """Every calendar month from the earliest to the latest seen, gaps included.

    A month nobody used is data — it is the quiet stretch a chart should draw as a gap
    in the run rather than close up. Plotting only the months that appear would slide
    2024's scattered sessions up against 2026's and make a three-year ramp look steady.
    """
    if not months:
        return []
    lo, hi = min(months), max(months)
    y, m = int(lo[:4]), int(lo[5:7])
    out: list[str] = []
    while (stamp := f"{y:04d}-{m:02d}") <= hi:
        out.append(stamp)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _top_series(
    totals: dict[str, float], per_month: dict[str, dict[str, int]], months: list[str], keep: int
) -> list[dict]:
    """The ``keep`` biggest keys as dense per-month series, everything else summed into
    one ``other``.

    A stacked chart can only carry so many colors before neighboring bands stop being
    tellable apart, so the tail folds rather than minting more hues. The fold is by
    *total*, never by a month's own ranking — a series has to keep one color across the
    whole axis or the chart restates rank as identity.
    """
    ranked = sorted(totals, key=lambda k: -totals[k])
    head, tail = ranked[:keep], ranked[keep:]
    series = [
        {"key": k, "values": [int(per_month.get(m, {}).get(k, 0)) for m in months]} for k in head
    ]
    if tail:
        series.append(
            {
                "key": "other",
                "values": [
                    int(sum(per_month.get(m, {}).get(k, 0) for k in tail)) for m in months
                ],
            }
        )
    return series


# How many bands each monthly chart carries before the tail folds into "other".
# Sources are stacked, so the cap is the size of the viewer's categorical palette — three
# hues plus a gray for the tail, which is as far as a stack can go and keep every pair
# distinguishable to a colorblind reader (see the viewer's chartColor.ts). Models are
# drawn as one panel each and take their color from the model itself, so identity comes
# from the label and the cap is only about how many panels fit on a screen.
TIMELINE_SOURCES = 3
TIMELINE_MODELS = 7

# Upper edges of the per-session token histogram, in tokens. Roughly log-spaced: session
# size spans four orders of magnitude, so linear bins would put everything in one bar.
# The open-ended top bucket catches the tail above the last edge.
SESSION_SIZE_EDGES: tuple[int, ...] = (1_000, 5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000)


def _collect_timeline(s) -> dict:  # noqa: ANN001 — Session
    """The archive's activity month by month: conversations started (split by source)
    and tokens spent (split by model), over a dense month axis.

    Two different clocks, deliberately, because the two questions have different
    denominators. Conversations bucket by when the *session* started
    (``thread_activity.first_at``), which is the only figure a source recording no token
    usage can contribute to at all. Tokens bucket by when each *request* happened
    (``request_metrics.month``), so a session running across a month boundary spends in
    both. They are drawn as separate charts; nothing here puts them on one axis.
    """
    conv_month: dict[str, dict[str, int]] = {}
    conv_totals: dict[str, float] = {}
    tok_month: dict[str, dict[str, int]] = {}
    tok_totals: dict[str, float] = {}
    month_totals: dict[str, dict] = {}

    for month, source, n in s.execute(
        text(
            """
            SELECT strftime('%Y-%m', a.first_at) AS month,
                   COALESCE(t.source, '') AS source,
                   COUNT(*)
            FROM thread_activity a
            JOIN threads t ON t.id = a.thread_id
            WHERE t.thread_type = 'conversation' AND t.archived = 0
            GROUP BY month, source
            """
        )
    ).all():
        if not month:
            continue
        conv_month.setdefault(month, {})[source or "(unknown)"] = int(n)
        conv_totals[source or "(unknown)"] = conv_totals.get(source or "(unknown)", 0) + int(n)
        month_totals.setdefault(month, {"conversations": 0, "tokens": 0, "cost": 0.0, "cost_requests": 0})
        month_totals[month]["conversations"] += int(n)

    # Placeholder models are filtered below rather than in SQL: they are dropped from the
    # per-model split but still counted in the month's total, so the query has to return
    # them.
    for month, model, tokens, cost, cost_requests in s.execute(
        text(
            """
            SELECT r.month,
                   r.model,
                   SUM(r.input_tokens + r.output_tokens) AS tokens,
                   SUM(COALESCE(r.cost, 0)) AS cost,
                   COUNT(r.cost) AS cost_requests
            FROM request_metrics r
            JOIN threads t ON t.id = r.thread_id
            WHERE t.thread_type = 'conversation' AND t.archived = 0
              AND r.month IS NOT NULL
            GROUP BY r.month, r.model
            """
        )
    ).all():
        bucket = month_totals.setdefault(
            month, {"conversations": 0, "tokens": 0, "cost": 0.0, "cost_requests": 0}
        )
        bucket["tokens"] += int(tokens or 0)
        bucket["cost"] += float(cost or 0.0)
        bucket["cost_requests"] += int(cost_requests or 0)
        # Placeholder model ids are dropped from the *split* but not from the month's
        # total, so the stack's bands can sum to less than the line above them without
        # either number being wrong.
        if model in NON_MODELS:
            continue
        tok_month.setdefault(month, {})[model] = int(tokens or 0)
        tok_totals[model] = tok_totals.get(model, 0) + int(tokens or 0)

    # Conversations the timeline cannot place: a thread whose events are all gone (or
    # that never had any) has no first_at and so no month. Reported rather than dropped,
    # so the chart's total can be seen not to match the overview tile without either
    # being wrong.
    dated = sum(sum(by.values()) for by in conv_month.values())
    total = s.execute(
        text("SELECT COUNT(*) FROM threads WHERE thread_type = 'conversation' AND archived = 0")
    ).scalar() or 0

    months = _month_range(list(month_totals))
    return {
        "months": months,
        "undated": max(int(total) - dated, 0),
        "conversations": [month_totals.get(m, {}).get("conversations", 0) for m in months],
        "tokens": [month_totals.get(m, {}).get("tokens", 0) for m in months],
        "cost": [
            month_totals[m]["cost"] if month_totals.get(m, {}).get("cost_requests") else None
            for m in months
        ],
        "conversations_by_source": _top_series(
            conv_totals, conv_month, months, TIMELINE_SOURCES
        ),
        "tokens_by_model": _top_series(tok_totals, tok_month, months, TIMELINE_MODELS),
    }


def _collect_session_sizes(s) -> dict:  # noqa: ANN001 — Session
    """How big a session gets, as a histogram over :data:`SESSION_SIZE_EDGES` plus the
    median and p90 of the same population.

    Only sessions that recorded token usage are in it — a web export contributes no
    size, and binning it as "0 tokens" would invent a spike of tiny sessions that never
    happened. The count of what was left out ships alongside so the chart can say so.
    """
    sizes = [
        int(n or 0)
        for (n,) in s.execute(
            text(
                """
                SELECT SUM(m.input_tokens + m.output_tokens) AS tokens
                FROM thread_metrics m
                JOIN threads t ON t.id = m.thread_id
                WHERE t.thread_type = 'conversation' AND t.archived = 0
                GROUP BY m.thread_id
                """
            )
        ).all()
    ]
    sizes = sorted(n for n in sizes if n > 0)
    counted = s.execute(
        text(
            "SELECT COUNT(*) FROM threads WHERE thread_type = 'conversation' AND archived = 0"
        )
    ).scalar() or 0

    # ``hi`` is None on the last bucket alone — the tail is open-ended, since there is no
    # size a session cannot exceed.
    buckets: list[dict[str, int | None]] = [
        {"lo": 0 if i == 0 else SESSION_SIZE_EDGES[i - 1], "hi": hi, "count": 0}
        for i, hi in enumerate(SESSION_SIZE_EDGES)
    ]
    buckets.append({"lo": SESSION_SIZE_EDGES[-1], "hi": None, "count": 0})
    for n in sizes:
        for b in buckets:
            hi = b["hi"]
            if hi is None or n <= hi:
                b["count"] = (b["count"] or 0) + 1
                break

    return {
        "buckets": buckets,
        "sessions": len(sizes),
        "without_tokens": max(int(counted) - len(sizes), 0),
        "median": _median(sizes),
        "p90": float(sizes[min(int(len(sizes) * 0.9), len(sizes) - 1)]) if sizes else None,
    }


def _collect_rhythm(s) -> dict:  # noqa: ANN001 — Session
    """When conversations start, as a weekday × hour grid — the archive's working week.

    Counted off ``thread_activity.first_at`` in the **server's local time**, which is the
    only frame in which "3pm" means anything to the operator reading it. Rows are
    Monday-first (SQLite's ``%w`` is Sunday-first, so the index is rotated).
    """
    grid = [[0] * 24 for _ in range(7)]
    for dow, hour, n in s.execute(
        text(
            """
            SELECT CAST(strftime('%w', a.first_at, 'localtime') AS INTEGER) AS dow,
                   CAST(strftime('%H', a.first_at, 'localtime') AS INTEGER) AS hour,
                   COUNT(*)
            FROM thread_activity a
            JOIN threads t ON t.id = a.thread_id
            WHERE t.thread_type = 'conversation' AND t.archived = 0
            GROUP BY dow, hour
            """
        )
    ).all():
        if dow is None or hour is None:
            continue
        grid[(int(dow) + 6) % 7][int(hour)] += int(n)  # Sunday-first → Monday-first
    return {
        "grid": grid,
        "max": max((max(row) for row in grid), default=0),
        "total": sum(sum(row) for row in grid),
    }


def collect_stats(*, model_limit: int | None = None) -> dict:
    """Compute the stats payload from the (freshly refreshed) rollup + threads table.

    Six sections: ``overview`` totals, ``by_source`` / ``by_model`` tables, a monthly
    ``timeline`` (conversations by source, tokens by model), a ``session_sizes``
    histogram, and the weekday × hour ``rhythm`` of session starts.

    Everything here aggregates the compact rollup tables (one row per thread-model, per
    request, per thread) joined to ``threads``, so it stays fast regardless of event
    volume; the join also drops any rollup row orphaned by a since-deleted thread.
    """
    refresh_metrics()

    from ._base import get_session

    # Conversations per source — the full universe, including sources whose transcripts
    # carry no token/cost data at all (web exports), so they still show a session count.
    conv_by_source: dict[str, int] = {}
    metric_by_source: dict[str, dict] = {}
    with get_session() as s:
        for source, n in s.execute(
            text(
                "SELECT COALESCE(source, ''), COUNT(*) FROM threads "
                "WHERE thread_type = 'conversation' AND archived = 0 GROUP BY source"
            )
        ).all():
            conv_by_source[source] = int(n)

        for row in s.execute(
            text(
                """
                SELECT COALESCE(t.source, '') AS source,
                       SUM(m.input_tokens)  AS in_tok,
                       SUM(m.cache_read_tokens) AS cache_tok,
                       SUM(m.output_tokens) AS out_tok,
                       SUM(m.cost)          AS cost,
                       COUNT(DISTINCT m.thread_id) AS data_convos,
                       COUNT(DISTINCT CASE WHEN m.cost_requests > 0 THEN m.thread_id END) AS cost_convos
                FROM thread_metrics m
                JOIN threads t ON t.id = m.thread_id
                WHERE t.thread_type = 'conversation' AND t.archived = 0
                GROUP BY t.source
                """
            )
        ).all():
            metric_by_source[row[0]] = {
                "input_tokens": int(row[1] or 0),
                "cache_read_tokens": int(row[2] or 0),
                "output_tokens": int(row[3] or 0),
                "cost": float(row[4] or 0.0),
                "data_convos": int(row[5] or 0),
                "cost_convos": int(row[6] or 0),
            }

        # Every real model, busiest first — `model_limit` caps the list only when a caller
        # asks (the viewer shows them all, so a low-volume model like a one-off kimi-k3 run
        # is never silently dropped). The placeholder ids ('', 'unknown', '<synthetic>') are
        # excluded here and from the distinct-model count below, so both agree.
        placeholders = ",".join(f"'{v}'" for v in NON_MODELS)
        model_rows = s.execute(
            text(
                f"""
                SELECT m.model,
                       SUM(m.requests)       AS requests,
                       SUM(m.input_tokens)   AS in_tok,
                       SUM(m.cache_read_tokens) AS cache_tok,
                       SUM(m.output_tokens)  AS out_tok,
                       SUM(m.cost)           AS cost,
                       SUM(m.cost_requests)  AS cost_requests,
                       COUNT(DISTINCT m.thread_id) AS convos
                FROM thread_metrics m
                JOIN threads t ON t.id = m.thread_id
                WHERE t.thread_type = 'conversation' AND t.archived = 0
                  AND m.model NOT IN ({placeholders})
                GROUP BY m.model
                ORDER BY requests DESC
                {"LIMIT :lim" if model_limit else ""}
                """
            ),
            {"lim": model_limit} if model_limit else {},
        ).all()

        # The TRUE distinct-model count for the overview tile — not len(model_rows), which a
        # caller's `model_limit` would understate.
        model_count = s.execute(
            text(
                f"SELECT COUNT(DISTINCT m.model) FROM thread_metrics m JOIN threads t ON t.id = m.thread_id "
                f"WHERE t.thread_type = 'conversation' AND t.archived = 0 AND m.model NOT IN ({placeholders})"
            )
        ).scalar() or 0

        span = s.execute(text("SELECT MIN(occurred_at), MAX(occurred_at) FROM events")).first()

        timeline = _collect_timeline(s)
        session_sizes = _collect_session_sizes(s)
        rhythm = _collect_rhythm(s)

    by_source = []
    for source, convos in sorted(conv_by_source.items(), key=lambda kv: -kv[1]):
        m = metric_by_source.get(source, {})
        tokens = m.get("input_tokens", 0) + m.get("output_tokens", 0)
        cost = m.get("cost", 0.0)
        cost_convos = m.get("cost_convos", 0)
        data_convos = m.get("data_convos", 0)
        by_source.append(
            {
                "source": source or "(unknown)",
                "conversations": convos,
                "with_tokens": data_convos,
                "input_tokens": m.get("input_tokens", 0),
                "cache_read_tokens": m.get("cache_read_tokens", 0),
                "output_tokens": m.get("output_tokens", 0),
                "tokens": tokens,
                "avg_tokens": _avg(tokens, data_convos),
                "with_cost": cost_convos,
                "cost": cost if cost_convos else None,
                "avg_cost": _avg(cost, cost_convos),
            }
        )

    by_model = [
        {
            "model": r[0],
            "requests": int(r[1] or 0),
            "input_tokens": int(r[2] or 0),
            "cache_read_tokens": int(r[3] or 0),
            "output_tokens": int(r[4] or 0),
            "tokens": int(r[2] or 0) + int(r[4] or 0),
            "cost": float(r[5]) if (r[6] or 0) > 0 else None,
            "conversations": int(r[7] or 0),
        }
        for r in model_rows
    ]

    overview = {
        "conversations": sum(conv_by_source.values()),
        "sources": len([s for s in conv_by_source if conv_by_source[s]]),
        "models": int(model_count),
        "input_tokens": sum(m["input_tokens"] for m in metric_by_source.values()),
        "cache_read_tokens": sum(
            m["cache_read_tokens"] for m in metric_by_source.values()
        ),
        "output_tokens": sum(m["output_tokens"] for m in metric_by_source.values()),
        "tokens": sum(m["input_tokens"] + m["output_tokens"] for m in metric_by_source.values()),
        "cost": sum(m["cost"] for m in metric_by_source.values()),
        "cost_conversations": sum(m["cost_convos"] for m in metric_by_source.values()),
        "first_at": span[0] if span else None,
        "last_at": span[1] if span else None,
    }

    return {
        "overview": overview,
        "by_source": by_source,
        "by_model": by_model,
        "timeline": timeline,
        "session_sizes": session_sizes,
        "rhythm": rhythm,
    }


def _median(sorted_vals: list[int]) -> float | None:
    """Median of an already-sorted list, or None when empty."""
    n = len(sorted_vals)
    if not n:
        return None
    mid = n // 2
    return float(sorted_vals[mid]) if n % 2 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2

# The most token-hungry sessions listed on a model's detail page — enough to see
# the shape of the tail without the list becoming a second all-threads page.
TOP_SESSIONS = 12


def collect_model_stats(model: str) -> dict | None:
    """One model's story across the archive, for the per-model drill-down page:
    totals, a per-session token distribution, a monthly time series, and the
    heaviest sessions. Returns None for a model with no live conversation data.

    Sessions here are the conversations in which ``model`` answered at least one
    request; token/request/cost numbers are that model's share of each (a mixed-model
    session contributes only its ``model`` rows). Compactions are ``context_summary``
    events counted across those same sessions — the event doesn't record which model's
    context overflowed, so in a mixed-model session they read as "compactions in
    sessions this model took part in", not "compactions this model caused".

    Time comes from ``threads.inserted_at`` (a session's ingest time — first event
    time for anything the watcher tailed live), not from the events themselves:
    surveying occurred_at per request means JSON-extracting over the fat event log,
    seconds per pass (the reason ThreadMetrics exists). Whole sessions bucket into
    the month they started; compaction events carry their own occurred_at.
    """
    refresh_metrics()

    from ._base import get_session

    with get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT m.thread_id, t.title, COALESCE(t.source, '') AS source,
                       t.inserted_at,
                       m.requests, m.input_tokens, m.cache_read_tokens,
                       m.output_tokens, m.thinking_tokens, m.cost, m.cost_requests
                FROM thread_metrics m
                JOIN threads t ON t.id = m.thread_id
                WHERE m.model = :model AND t.thread_type = 'conversation' AND t.archived = 0
                """
            ),
            {"model": model},
        ).all()
        if not rows:
            return None

        # Compactions in those sessions, split by thread (for the top-sessions list)
        # and by the event's own month (for the time series).
        compact_by_thread: dict[str, int] = {}
        compact_by_month: dict[str, int] = {}
        for tid, month, n in s.execute(
            text(
                """
                SELECT e.thread_id, strftime('%Y-%m', e.occurred_at) AS month, COUNT(*)
                FROM events e
                WHERE e.event_type = 'context_summary'
                  AND e.thread_id IN (SELECT thread_id FROM thread_metrics WHERE model = :model)
                GROUP BY e.thread_id, month
                """
            ),
            {"model": model},
        ).all():
            compact_by_thread[str(tid)] = compact_by_thread.get(str(tid), 0) + int(n)
            if month:
                compact_by_month[month] = compact_by_month.get(month, 0) + int(n)

    sessions = [
        {
            "thread_id": str(tid),
            "title": title,
            "source": source or "(unknown)",
            "at": str(at) if at else None,
            "month": str(at)[:7] if at else None,
            "requests": int(req or 0),
            "input_tokens": int(itok or 0),
            "cache_read_tokens": int(ctok or 0),
            "output_tokens": int(otok or 0),
            "tokens": int(itok or 0) + int(otok or 0),
            "thinking_tokens": int(ttok or 0),
            "cost": float(cost or 0.0),
            "cost_requests": int(creq or 0),
            "compactions": compact_by_thread.get(str(tid), 0),
        }
        for tid, title, source, at, req, itok, ctok, otok, ttok, cost, creq in rows
    ]

    per_session_tokens = sorted(sess["tokens"] for sess in sessions)
    n_sessions = len(sessions)
    total_cost_requests = sum(sess["cost_requests"] for sess in sessions)
    cost_sessions = [sess for sess in sessions if sess["cost_requests"]]

    months: dict[str, dict] = {}
    for sess in sessions:
        if not sess["month"]:
            continue
        b = months.setdefault(
            sess["month"],
            {"sessions": 0, "requests": 0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0, "tokens": 0, "cost": 0.0, "cost_sessions": 0},
        )
        b["sessions"] += 1
        b["requests"] += sess["requests"]
        b["input_tokens"] += sess["input_tokens"]
        b["cache_read_tokens"] += sess["cache_read_tokens"]
        b["output_tokens"] += sess["output_tokens"]
        b["tokens"] += sess["tokens"]
        b["cost"] += sess["cost"]
        b["cost_sessions"] += 1 if sess["cost_requests"] else 0
    by_month = [
        {
            "month": month,
            "sessions": b["sessions"],
            "requests": b["requests"],
            "input_tokens": b["input_tokens"],
            "cache_read_tokens": b["cache_read_tokens"],
            "output_tokens": b["output_tokens"],
            "tokens": b["tokens"],
            "avg_tokens": _avg(b["tokens"], b["sessions"]),
            "cost": b["cost"] if b["cost_sessions"] else None,
            "compactions": compact_by_month.get(month, 0),
        }
        # Union with compaction-only months: a session started in one month can
        # compact in the next, and that activity shouldn't vanish from the series.
        for month, b in sorted(
            (months | {
                m: {"sessions": 0, "requests": 0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0, "tokens": 0, "cost": 0.0, "cost_sessions": 0}
                for m in compact_by_month if m not in months
            }).items()
        )
    ]

    top_sessions = [
        {
            k: sess[k]
            for k in (
                "thread_id", "title", "source", "at", "tokens",
                "cache_read_tokens", "requests", "compactions",
            )
        }
        for sess in sorted(sessions, key=lambda x: -x["tokens"])[:TOP_SESSIONS]
    ]

    return {
        "model": model,
        "overview": {
            "conversations": n_sessions,
            "requests": sum(sess["requests"] for sess in sessions),
            "input_tokens": sum(sess["input_tokens"] for sess in sessions),
            "cache_read_tokens": sum(
                sess["cache_read_tokens"] for sess in sessions
            ),
            "output_tokens": sum(sess["output_tokens"] for sess in sessions),
            "thinking_tokens": sum(sess["thinking_tokens"] for sess in sessions),
            "tokens": sum(sess["tokens"] for sess in sessions),
            "cost": sum(sess["cost"] for sess in sessions) if total_cost_requests else None,
            "cost_conversations": len(cost_sessions),
            "compactions": sum(compact_by_thread.values()),
            "first_at": min((sess["at"] for sess in sessions if sess["at"]), default=None),
            "last_at": max((sess["at"] for sess in sessions if sess["at"]), default=None),
        },
        "per_session": {
            "min_tokens": per_session_tokens[0],
            "max_tokens": per_session_tokens[-1],
            "avg_tokens": _avg(sum(per_session_tokens), n_sessions),
            "median_tokens": _median(per_session_tokens),
            "avg_requests": _avg(sum(sess["requests"] for sess in sessions), n_sessions),
        },
        "by_month": by_month,
        "top_sessions": top_sessions,
    }
