"""Incremental token/cost rollup behind the viewer's stats page.

Cost and token counts are recorded inside each ``api_request_completed`` event's
JSON ``payload`` (``input_tokens`` / ``output_tokens`` / ``thinking_tokens`` /
``cost`` / ``model``). Surveying them straight from ``events`` means JSON-extracting
across hundreds of thousands of fat payloads on a multi-GB index — seconds per pass,
far too slow to run per request. :class:`ThreadMetrics` is the standing aggregate,
and this module keeps it current: :func:`refresh_metrics` folds only the events past
a global cursor (:class:`MetricsCursor`) into per-(thread, model) running sums, so the
full survey is paid once and every refresh after that touches only what landed since.

Correctness rests on the event log being append-only with monotonic ids: folding
``through < id <= upto`` and advancing the cursor to ``upto`` sums each event exactly
once. The one violation is a reindex rebuilding the log; that's caught by the cursor
running ahead of ``MAX(events.id)``, which resets the cache to a clean rebuild.

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

# Fold new completed-request events into the per-(thread, model) running sums. Null
# token fields coalesce to 0; ``cost`` sums treating a null (a route that reported
# none, e.g. local models) as 0, while ``cost_requests`` counts the requests that
# actually carried a cost so "$0" stays distinct from "no cost recorded".
_FOLD_SQL = text(
    """
    INSERT INTO thread_metrics (
        thread_id, model, requests,
        input_tokens, output_tokens, thinking_tokens, cost, cost_requests
    )
    SELECT
        e.thread_id,
        COALESCE(json_extract(e.payload, '$.model'), '') AS model,
        COUNT(*),
        COALESCE(SUM(CAST(json_extract(e.payload, '$.input_tokens') AS INTEGER)), 0),
        COALESCE(SUM(CAST(json_extract(e.payload, '$.output_tokens') AS INTEGER)), 0),
        COALESCE(SUM(CAST(json_extract(e.payload, '$.thinking_tokens') AS INTEGER)), 0),
        COALESCE(SUM(CAST(json_extract(e.payload, '$.cost') AS REAL)), 0),
        SUM(CASE WHEN json_extract(e.payload, '$.cost') IS NOT NULL THEN 1 ELSE 0 END)
    FROM events e
    WHERE e.event_type = 'api_request_completed'
      AND e.id > :through AND e.id <= :upto
    GROUP BY e.thread_id, model
    ON CONFLICT(thread_id, model) DO UPDATE SET
        requests        = requests        + excluded.requests,
        input_tokens    = input_tokens    + excluded.input_tokens,
        output_tokens   = output_tokens   + excluded.output_tokens,
        thinking_tokens = thinking_tokens + excluded.thinking_tokens,
        cost            = cost            + excluded.cost,
        cost_requests   = cost_requests   + excluded.cost_requests
    """
)


def refresh_metrics(engine: Engine | None = None) -> None:
    """Bring :class:`ThreadMetrics` up to date with the event log.

    Idempotent and cheap when already current (an indexed ``MAX(id)`` read, then a
    no-op). The first call on a fresh cache — and the call after a reindex — pays the
    full survey once; every call after folds only newly-arrived events.
    """
    engine = engine or get_engine()
    with _refresh_lock, engine.begin() as conn:
        upto = conn.execute(text("SELECT MAX(id) FROM events")).scalar()
        if upto is None:
            return  # empty archive — nothing to roll up
        conn.execute(text("INSERT OR IGNORE INTO metrics_cursor (id, through_event_id) VALUES (1, 0)"))
        through = conn.execute(text("SELECT through_event_id FROM metrics_cursor WHERE id = 1")).scalar() or 0
        if through > upto:
            # The log shrank below the cursor — a reindex rebuilt it. Reset and rebuild
            # from zero rather than trust sums whose events may no longer exist.
            conn.execute(text("DELETE FROM thread_metrics"))
            through = 0
        if through >= upto:
            return  # already current
        conn.execute(_FOLD_SQL, {"through": through, "upto": upto})
        conn.execute(
            text("UPDATE metrics_cursor SET through_event_id = :upto WHERE id = 1"),
            {"upto": upto},
        )


def _avg(total: float, n: int) -> float | None:
    """Mean over ``n`` items, or None when there are none (so the viewer can render an
    honest '—' instead of a fabricated 0 for a source that recorded no such data)."""
    return total / n if n else None


def collect_stats(*, model_limit: int | None = None) -> dict:
    """Compute the stats payload from the (freshly refreshed) rollup + threads table.

    Everything here aggregates the compact ``thread_metrics`` table (one row per
    thread-model) joined to ``threads``, so it stays fast regardless of event volume;
    the join also drops any rollup row orphaned by a since-deleted thread.
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
                "output_tokens": int(row[2] or 0),
                "cost": float(row[3] or 0.0),
                "data_convos": int(row[4] or 0),
                "cost_convos": int(row[5] or 0),
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
            "output_tokens": int(r[3] or 0),
            "tokens": int(r[2] or 0) + int(r[3] or 0),
            "cost": float(r[4]) if (r[5] or 0) > 0 else None,
            "conversations": int(r[6] or 0),
        }
        for r in model_rows
    ]

    overview = {
        "conversations": sum(conv_by_source.values()),
        "sources": len([s for s in conv_by_source if conv_by_source[s]]),
        "models": int(model_count),
        "input_tokens": sum(m["input_tokens"] for m in metric_by_source.values()),
        "output_tokens": sum(m["output_tokens"] for m in metric_by_source.values()),
        "tokens": sum(m["input_tokens"] + m["output_tokens"] for m in metric_by_source.values()),
        "cost": sum(m["cost"] for m in metric_by_source.values()),
        "cost_conversations": sum(m["cost_convos"] for m in metric_by_source.values()),
        "first_at": span[0] if span else None,
        "last_at": span[1] if span else None,
    }

    return {"overview": overview, "by_source": by_source, "by_model": by_model}


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
                       m.requests, m.input_tokens, m.output_tokens, m.thinking_tokens,
                       m.cost, m.cost_requests
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
            "output_tokens": int(otok or 0),
            "tokens": int(itok or 0) + int(otok or 0),
            "thinking_tokens": int(ttok or 0),
            "cost": float(cost or 0.0),
            "cost_requests": int(creq or 0),
            "compactions": compact_by_thread.get(str(tid), 0),
        }
        for tid, title, source, at, req, itok, otok, ttok, cost, creq in rows
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
            {"sessions": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0, "tokens": 0, "cost": 0.0, "cost_sessions": 0},
        )
        b["sessions"] += 1
        b["requests"] += sess["requests"]
        b["input_tokens"] += sess["input_tokens"]
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
                m: {"sessions": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0, "tokens": 0, "cost": 0.0, "cost_sessions": 0}
                for m in compact_by_month if m not in months
            }).items()
        )
    ]

    top_sessions = [
        {k: sess[k] for k in ("thread_id", "title", "source", "at", "tokens", "requests", "compactions")}
        for sess in sorted(sessions, key=lambda x: -x["tokens"])[:TOP_SESSIONS]
    ]

    return {
        "model": model,
        "overview": {
            "conversations": n_sessions,
            "requests": sum(sess["requests"] for sess in sessions),
            "input_tokens": sum(sess["input_tokens"] for sess in sessions),
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
