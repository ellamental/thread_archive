"""The curation subsystem's operational read surface — what the drains have done.

The drains are unattended: they fire on a schedule, spend a headless Claude
instance each time, and leave their trace in three places that nothing joins up.
This module joins them, for the viewer's curation page and for anyone asking
"is it working, and what is it costing":

* **queues** — how much work each drain still sees (the same gates the daemons
  fire on, so the page and the daemon can never disagree) plus the graph-shape
  counts from :mod:`.._knowledge.garden`;
* **output** — what curation actually produced, per day: citations, links, new
  topics. Read from the rows' own ``created_at``, so these are real per-day
  counts rather than an inferred activity signal;
* **runs** — the drains' own transcripts. Each run archives itself (its cwd is
  ``<home>/curation``, which is how it's identified here), so the archive can
  report its own curation cost from the same store it curates.

Deliberately absent: a per-day summary count. A stored summary has no set-time
of its own — only the thread's ``updated_at``, which any write touches — so any
daily figure would be a guess wearing a number's clothes. Summaries appear as
coverage (how many threads have one) instead, which is exact.

Cost is reported as **requests and output tokens**, not dollars: the drains run
on a subscription login that records no per-token price, so a cost figure here
would be fabricated. Input tokens are recorded but exclude cached context, so
they understate the real read volume and are left out rather than shown as a
misleading half-truth.

Reads only the archive's own state, with one write: folding newly-ingested
events into the shared token rollup, exactly as the stats page does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .._config import resolve_paths
from .._store import use_session

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30

# How a curation run identifies itself: the drain launches with its cwd set to
# <home>/curation (see _curation.run), and the claude-code importer records the
# cwd on the session. Title is LLM-written and unreliable; cwd is not.
_RUN_CWD_NAME = "curation"

# Which drain a run belongs to, read from the prompt it was launched with — the
# first user message carries the packaged prompt file verbatim.
_KIND_MARKERS = (("gardener", "# The gardener"), ("librarian", "# The librarian"))


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _heartbeat(kind: str, home: Optional[str]) -> dict:
    """When this drain last fired, launched or skipped. Absent file = never
    fired under this home (a fresh install), not a dead daemon."""
    hb = resolve_paths(home).home / "logs" / f"{kind}.heartbeat"
    try:
        mtime = hb.stat().st_mtime
    except OSError:
        return {"heartbeat_at": None, "heartbeat_age_s": None}
    at = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return {
        "heartbeat_at": _iso(at),
        "heartbeat_age_s": round((datetime.now(timezone.utc) - at).total_seconds(), 1),
    }


def _drains(home: Optional[str]) -> dict:
    """Per-drain: backlog (the daemon's own gate), cadence, model, heartbeat.

    The librarian also reports its catch-up policy and the history sitting behind
    it. Without that the page would show a forward-only backlog of a few dozen
    while thousands of older conversations go uncurated by design, and nothing on
    the page would say so.
    """
    from .. import _launchd
    from . import (
        DRAINS,
        curation_settings,
        gardener_backlog,
        librarian_backlog,
        librarian_catchup,
        librarian_counts,
        librarian_horizon,
    )

    hour, minute = _launchd.resolved_gardener_schedule(home)
    cadence = {
        "librarian": {
            "kind": "interval",
            "interval_s": _launchd.resolved_librarian_interval(home),
        },
        "gardener": {"kind": "daily", "at": f"{hour:02d}:{minute:02d}"},
    }
    out = {}
    for kind, backlog in (
        ("librarian", librarian_backlog(home)),
        ("gardener", gardener_backlog(home)),
    ):
        model, effort = curation_settings(kind, home)
        out[kind] = {
            # None = the gate query failed; the daemon fails open and launches,
            # so the page must not render that as "drained".
            "backlog": backlog,
            "batch": DRAINS[kind].batch,
            "model": model,
            "effort": effort or None,
            "cadence": cadence[kind],
            **_heartbeat(kind, home),
        }
    counts = librarian_counts(home)
    horizon = librarian_horizon(home)
    out["librarian"]["policy"] = {
        "horizon": _iso(horizon),
        "catchup_per_run": librarian_catchup(home),
        "forward": (counts or {}).get("forward"),
        "history": (counts or {}).get("history"),
    }
    return out


def _coverage(s: Session) -> dict:
    """Absolute state: how much of the corpus carries each half of the
    librarian's commit, and how big the topic graph is."""
    row = s.execute(text(
        "SELECT "
        "  (SELECT count(*) FROM threads WHERE thread_type = 'conversation' "
        "     AND NOT archived) AS conversations, "
        "  (SELECT count(*) FROM threads WHERE thread_type = 'conversation' "
        "     AND NOT archived AND summary IS NOT NULL AND trim(summary) != '') "
        "     AS summarized, "
        # Citations can anchor to any thread type; only conversations are what
        # the librarian's coverage is measured against, so scope the count to
        # them or 'cited' can exceed 'conversations'.
        "  (SELECT count(DISTINCT tm.thread_id) FROM topic_messages tm "
        "     JOIN threads th ON th.id = tm.thread_id "
        "     WHERE tm.archived_at IS NULL AND th.thread_type = 'conversation' "
        "       AND NOT th.archived) AS cited, "
        "  (SELECT count(*) FROM threads WHERE thread_type = 'topic' "
        "     AND NOT archived) AS topics_live, "
        "  (SELECT count(*) FROM threads WHERE thread_type = 'topic' "
        "     AND archived) AS topics_archived, "
        "  (SELECT count(*) FROM topic_messages WHERE archived_at IS NULL) "
        "     AS citations, "
        "  (SELECT count(*) FROM thread_links) AS links"
    )).mappings().one()
    return dict(row)


def _uncuratable(s: Session, limit: int = 10) -> dict:
    """Conversation threads that carry events but no message — nothing to cite,
    nothing to summarize, so no drain can ever clear them.

    These are an *ingest* condition, not curation backlog: a session that
    registered a thread and only ever wrote bookkeeping (an empty
    ``file_snapshot``, a ``queue_operation``). The queues exclude them by
    design, which is exactly why they need surfacing here — excluded work that
    nothing counts is work that silently accumulates.
    """
    from .._retrieval._extract import INDEXABLE_EVENT_TYPES

    params = {f"t{i}": t for i, t in enumerate(INDEXABLE_EVENT_TYPES)}
    types = ", ".join(f":{k}" for k in params)
    where = (
        "FROM threads t WHERE t.thread_type = 'conversation' AND NOT t.archived "
        "  AND EXISTS (SELECT 1 FROM events e WHERE e.thread_id = t.id) "
        f"  AND NOT EXISTS (SELECT 1 FROM events e WHERE e.thread_id = t.id "
        f"                  AND e.event_type IN ({types}))"
    )
    count = s.execute(text(f"SELECT count(*) {where}"), params).scalar_one()
    rows = s.execute(
        text(
            f"SELECT t.id, t.title, t.source, "
            f"  (SELECT group_concat(DISTINCT e.event_type) FROM events e "
            f"   WHERE e.thread_id = t.id) AS event_types "
            f"{where} ORDER BY t.id DESC LIMIT :lim"
        ),
        {**params, "lim": limit},
    ).mappings().all()
    return {"threads": int(count or 0), "sample": [dict(r) for r in rows]}


def _activity(s: Session, days: int) -> list[dict]:
    """Per-day curation output over the window, oldest first. Days on which
    nothing was written are present with zeros — a gap in a drain's record is
    a fact about the drain, and a sparse series hides it."""
    rows = s.execute(
        text(
            "WITH d(day, citations, links, topics) AS ( "
            "  SELECT date(created_at), count(*), 0, 0 FROM topic_messages "
            "    WHERE created_at >= date('now', :span) GROUP BY date(created_at) "
            "  UNION ALL "
            "  SELECT date(created_at), 0, count(*), 0 FROM thread_links "
            "    WHERE created_at >= date('now', :span) GROUP BY date(created_at) "
            "  UNION ALL "
            "  SELECT date(inserted_at), 0, 0, count(*) FROM threads "
            "    WHERE thread_type = 'topic' AND inserted_at >= date('now', :span) "
            "    GROUP BY date(inserted_at) "
            ") "
            "SELECT day, sum(citations) citations, sum(links) links, "
            "       sum(topics) topics FROM d GROUP BY day ORDER BY day"
        ),
        {"span": f"-{days} days"},
    ).mappings().all()
    return _fill_days({r["day"]: dict(r) for r in rows}, days,
                      ("citations", "links", "topics"))


def _fill_days(by_day: dict, days: int, fields: tuple[str, ...]) -> list[dict]:
    """The window as a dense oldest-first series, zero-filling missing days."""
    today = datetime.now(timezone.utc).date()
    out = []
    for back in range(days - 1, -1, -1):
        day = (today.toordinal() - back)
        key = datetime.fromordinal(day).date().isoformat()
        row = by_day.get(key)
        out.append({"day": key, **{f: int((row or {}).get(f) or 0) for f in fields}})
    return out


def _runs(s: Session, home: Optional[str], days: int, recent: int = 20) -> dict:
    """The drains' own archived transcripts: per-day counts and cost, plus the
    latest runs. Empty when the runs haven't been ingested (the watcher imports
    them like any other session — a just-finished run may not be here yet)."""
    # Fold any newly-ingested events into the shared token rollup first, the same
    # way the stats page does. Without it a run archived since the last fold
    # reports zero tokens — indistinguishable, on the page, from a run that was
    # free. Idempotent and a no-op when already current.
    from .._store._metrics import refresh_metrics

    try:
        refresh_metrics()
    except Exception:  # noqa: BLE001 — cost is a nice-to-have; queues are the point
        logger.debug("curation stats: metrics refresh failed", exc_info=True)

    run_cwd = str(resolve_paths(home).home / _RUN_CWD_NAME)
    kind_case = " ".join(
        f"WHEN instr(e.payload, '{marker}') > 0 THEN '{kind}'"
        for kind, marker in _KIND_MARKERS
    )
    # One run = one thread; a run's metrics can span several model rows, so the
    # run count is a DISTINCT while the cost columns sum.
    run_source = (
        "FROM threads t "
        "LEFT JOIN events e ON e.id = (SELECT min(id) FROM events "
        "     WHERE thread_id = t.id AND event_type = 'user_message_sent') "
        "LEFT JOIN thread_metrics m ON m.thread_id = t.id "
        "WHERE json_extract(t.source_metadata, '$.cwd') = :cwd"
    )
    by_day = s.execute(
        text(
            f"SELECT date(t.inserted_at) AS day, "
            f"  CASE {kind_case} ELSE 'unknown' END AS kind, "
            f"  count(DISTINCT t.id) AS runs, "
            f"  coalesce(sum(m.requests), 0) AS requests, "
            f"  coalesce(sum(m.output_tokens), 0) AS output_tokens "
            f"{run_source} AND t.inserted_at >= date('now', :span) "
            f"GROUP BY day, kind ORDER BY day"
        ),
        {"cwd": run_cwd, "span": f"-{days} days"},
    ).mappings().all()

    latest = s.execute(
        text(
            f"SELECT t.id, t.title, t.inserted_at AS started_at, "
            f"  CASE {kind_case} ELSE 'unknown' END AS kind, "
            f"  coalesce(sum(m.requests), 0) AS requests, "
            f"  coalesce(sum(m.output_tokens), 0) AS output_tokens, "
            f"  max(m.model) AS model "
            f"{run_source} "
            f"GROUP BY t.id ORDER BY t.id DESC LIMIT :lim"
        ),
        {"cwd": run_cwd, "lim": recent},
    ).mappings().all()

    merged: dict[str, dict] = {}
    for r in by_day:
        slot = merged.setdefault(r["day"], {"librarian": 0, "gardener": 0,
                                            "requests": 0, "output_tokens": 0})
        if r["kind"] in ("librarian", "gardener"):
            slot[r["kind"]] += int(r["runs"] or 0)
        slot["requests"] += int(r["requests"] or 0)
        slot["output_tokens"] += int(r["output_tokens"] or 0)
    return {
        "by_day": _fill_days(merged, days,
                             ("librarian", "gardener", "requests", "output_tokens")),
        "recent": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "title": r["title"],
                "started_at": _iso(r["started_at"]) if isinstance(
                    r["started_at"], datetime) else r["started_at"],
                "requests": int(r["requests"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "model": r["model"],
            }
            for r in latest
        ],
    }


def collect_curation_stats(
    *, days: int = DEFAULT_DAYS, home: Optional[str] = None,
    session: Optional[Session] = None,
) -> dict:
    """Everything the curation page shows, in one read-only pass.

    ``days`` bounds the two time series (activity and runs). The graph section
    is the live :func:`.._knowledge.garden.garden_status` — a projection walk,
    the one part of this that isn't a plain index query.
    """
    from .._knowledge.garden import garden_status

    days = max(1, min(int(days), 365))
    with use_session(session) as s:
        coverage = _coverage(s)
        uncuratable = _uncuratable(s)
        activity = _activity(s, days)
        runs = _runs(s, home, days)
        graph = garden_status(session=s)
    topics = graph.get("topics") or 0
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "days": days,
        "drains": _drains(home),
        "graph": {
            **graph,
            "hierarchy_pct": round(100 * (graph.get("in_hierarchy") or 0) / topics, 1)
            if topics else None,
        },
        "coverage": coverage,
        "uncuratable": uncuratable,
        "activity": activity,
        "runs": runs,
    }
