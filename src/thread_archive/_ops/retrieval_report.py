"""How retrieval is doing, assembled from the three ledgers that record it.

The read-only summary behind the viewer's retrieval page. Three files answer three
different questions and none of them answers alone:

``retrieval-usage.jsonl``
    What agents actually *got* — served latency, per stage, under whatever
    conditions the serving process happened to be in. The only source that
    describes reality, and the noisiest.

``latency-runs.jsonl``
    What the pipeline *costs* under control — warm, pool cache off, a fixed query
    set. Comparable across days in a way served latency is not, and consistently
    faster than it (see :func:`served`).

``gold-runs.jsonl``
    Whether it still finds the right thing. Latency without quality is half a
    verdict: every one of the cheap ways to make search faster is a way to make it
    worse, so the two series belong on one page.

Three rules are baked in here rather than left to the caller, because getting any
of them wrong produces a plausible chart that is simply false:

- **Probe queries are excluded.** A bench or a smoke test leaves one-character
  queries in the ledger; they return in ~1 ms and pull every percentile down.
- **Cold and warm are separated, never averaged.** A process's first search runs an
  order of magnitude slower than its thousandth, and restarts are frequent, so a
  single daily median is mostly a measure of how often the daemon bounced.
  ``uptime_s`` is what makes the split possible and is absent on older rows.
- **Query sets and pooled runs stay apart.** Latency over the gold files and over
  the usage ledger are different populations; a gold run scored from persisted
  pools measures the cache rather than the pipeline. Both are flagged in the rows
  and both are honoured here.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

#: Queries a bench or a smoke test left behind rather than an agent asking
#: something. Excluded everywhere: they are ~1 ms and there is no question they
#: are the honest answer to.
PROBE_QUERIES = ("x", "test", "warmup", "hello", "bogus")

#: Below this many seconds of process uptime, a search is charged to the cold
#: regime. Two minutes covers a warm pass that has not finished (measured at
#: 15-45 s) plus the first searches racing it. Rows without ``uptime_s`` predate
#: the field and are counted as neither rather than guessed at.
COLD_UPTIME_S = 120.0

#: Stages worth charting, in pipeline order. The two arms run concurrently, so
#: they do not sum to the total and are never drawn as a stacked share of one.
STAGES = ("fts_ms", "semantic_ms", "match_ms", "scan_ms", "embed_ms", "scope_ms",
          "matrix_ms", "knn_ms", "hydrate_ms", "set_ms", "rerank_ms", "rank_ms",
          "extend_ms", "enrich_ms", "render_ms")


def percentile(xs: list[float], q: float) -> float:
    """Nearest-rank percentile. Zero for an empty sample — a chart point that does
    not exist is drawn as absent by the caller, never as a real zero."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    return ordered[min(int(len(ordered) * q), len(ordered) - 1)]


def _rows(path: Path) -> Iterator[dict]:
    """Every JSON object in a ledger, skipping what cannot be parsed. A torn final
    line is a concurrent append, not a corrupt file."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _searches(home: Path, *, days: int) -> list[dict]:
    """Real agent searches within the window — probes dropped, failures kept.

    Failures stay because dropping slow errors biases every percentile toward the
    searches that happened to succeed, which is the same reason the ledger records
    them in the first place."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    from .._retrieval.usage import LEDGER_FILE

    return [
        r for r in _rows(home / LEDGER_FILE)
        if r.get("kind") == "search" and r.get("duration_ms") is not None
        and r.get("at", "") >= cutoff
        and (r.get("query") or "") not in PROBE_QUERIES and (r.get("query") or "").strip()
    ]


def _regime(rec: dict) -> str:
    """``cold`` / ``warm`` / ``unknown`` for one search, by the age of the process
    that served it. ``unknown`` is its own bucket rather than a default, so a chart
    can show how much of the window predates the measurement instead of quietly
    folding it into whichever regime is more flattering."""
    uptime = rec.get("uptime_s")
    if uptime is None:
        return "unknown"
    return "cold" if uptime < COLD_UPTIME_S else "warm"


def served(home: Path, *, days: int = 14) -> dict[str, Any]:
    """What agents got, by day and by regime.

    ``daily`` carries one entry per day with a count and p50/p90 per regime, so the
    chart can draw warm and cold as separate series. Drawing them as one is the
    thing this exists to prevent: over a restart-heavy window the combined median
    tracks the restart rate rather than the code."""
    rows = _searches(home, days=days)
    by_day: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_day[r["at"][:10]][_regime(r)].append(r["duration_ms"])
    daily = []
    for day in sorted(by_day):
        entry: dict[str, Any] = {"day": day, "n": sum(len(v) for v in by_day[day].values())}
        for regime, xs in by_day[day].items():
            entry[regime] = {"n": len(xs), "p50": round(percentile(xs, 0.50), 1),
                             "p90": round(percentile(xs, 0.90), 1)}
        daily.append(entry)
    warm = [r["duration_ms"] for r in rows if _regime(r) == "warm"]
    cold = [r["duration_ms"] for r in rows if _regime(r) == "cold"]
    return {
        "days": days,
        "n": len(rows),
        "n_unknown_regime": sum(1 for r in rows if _regime(r) == "unknown"),
        "daily": daily,
        "warm": {"n": len(warm), "p50": round(percentile(warm, 0.50), 1),
                 "p90": round(percentile(warm, 0.90), 1),
                 "p99": round(percentile(warm, 0.99), 1)},
        "cold": {"n": len(cold), "p50": round(percentile(cold, 0.50), 1),
                 "p90": round(percentile(cold, 0.90), 1),
                 "p99": round(percentile(cold, 0.99), 1)},
    }


def stages(home: Path, *, days: int = 14) -> dict[str, Any]:
    """Where a served search spends its time, p50/p90 per stage.

    Known-cold searches are excluded — a cold process's first search is nearly all
    model and pack loading, and averaging that in makes every stage look like the
    embedder. Excluded rather than *warm required*, because ``uptime_s`` is recent
    and most of the window predates it: demanding proof of warmth would empty the
    chart for as long as the ledger's memory is older than the field. Those rows
    are counted in ``n_unproven`` so the caller can say so rather than imply a
    certainty the data does not carry.

    Stages with nothing to report are dropped rather than drawn at zero, and the
    two arms are returned flat rather than as shares of a whole: they run
    concurrently, so their sum exceeds the total and any stacked chart of them is a
    lie about where the wall-clock went."""
    rows = [r for r in _searches(home, days=days) if _regime(r) != "cold"]
    out = []
    for stage in STAGES:
        xs = [r[stage] for r in rows if isinstance(r.get(stage), (int, float))]
        if not xs or not any(xs):
            continue
        out.append({"stage": stage, "n": len(xs),
                    "p50": round(percentile(xs, 0.50), 1),
                    "p90": round(percentile(xs, 0.90), 1)})
    return {
        "n": len(rows),
        "n_unproven": sum(1 for r in rows if _regime(r) == "unknown"),
        "stages": sorted(out, key=lambda s: -s["p50"]),
    }


def restarts(home: Path, *, days: int = 14) -> dict[str, Any]:
    """Process starts per day, counted off the warm-pass rows, with what they cost.

    On this deployment the restart rate is the single largest influence on served
    latency — every cache retrieval leans on is process-local — so it belongs
    beside the latency charts rather than in a separate operational corner."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    from .._retrieval.usage import LEDGER_FILE

    rows = [r for r in _rows(home / LEDGER_FILE)
            if r.get("kind") == "warm" and r.get("at", "") >= cutoff]
    per_day = Counter(r["at"][:10] for r in rows)
    durations = [r["duration_ms"] for r in rows if r.get("duration_ms")]
    return {
        "n": len(rows),
        "daily": [{"day": d, "n": per_day[d]} for d in sorted(per_day)],
        "p50_ms": round(percentile(durations, 0.50), 1),
        "total_s": round(sum(durations) / 1000.0, 1),
    }


def bench(home: Path, *, limit: int = 40) -> dict[str, list[dict[str, Any]]]:
    """The controlled latency timeseries, split by query set.

    Never merged: the gold files and the usage ledger are different populations of
    query, so a p50 over one is not a point on the other's line."""
    from . import speed

    series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _rows(home / speed.LATENCY_RUNS_FILE):
        total = r.get("total") or {}
        if not total.get("p50"):
            continue
        series[r.get("query_set") or speed.GOLD_SET].append({
            "at": r.get("at"), "commit": r.get("commit"),
            "p50": total.get("p50"), "p95": total.get("p95"), "p99": total.get("p99"),
            "n_queries": r.get("n_queries"),
            "tuning": bool(r.get("overrides")),
        })
    return {k: v[-limit:] for k, v in series.items()}


def quality(home: Path, *, limit: int = 40) -> dict[str, Any]:
    """The gold timeseries — weighted MRR and nDCG per run, newest last.

    Pooled runs are excluded outright rather than flagged: they score from
    persisted candidate pools, which leaves the *scores* meaningful but makes them
    a different measurement from the run beside them, and a line that silently
    mixes the two is worse than a shorter line."""
    from . import gold_runs

    points = []
    for r in _rows(home / gold_runs.LEDGER_FILE):
        files = r.get("files") or {}
        if not files or r.get("pool_cache") or r.get("overrides"):
            continue
        n = sum(f.get("n", 0) for f in files.values())
        if not n:
            continue
        points.append({
            "at": r.get("at"), "commit": r.get("commit"), "passed": r.get("passed"),
            "mrr": round(sum(f.get("mrr", 0) * f.get("n", 0) for f in files.values()) / n, 4),
            "ndcg": round(sum(f.get("ndcg10", 0) * f.get("n", 0) for f in files.values()) / n, 4),
            "n": n,
        })
    latest = points[-1] if points else None
    return {"points": points[-limit:], "latest": latest}


def report(home: Optional[Path] = None, *, days: int = 14) -> dict[str, Any]:
    """The whole page's data in one call. Every section is independently fail-soft:
    a missing or unreadable ledger yields an empty section, never a failed page —
    an operator view that goes blank when one input is absent is the least useful
    thing it could do."""
    if home is None:
        from .._config import resolve_paths

        home = resolve_paths().home
    out: dict[str, Any] = {"home": str(home), "days": days,
                           "at": datetime.now(timezone.utc).isoformat()}
    for name, fn in (("served", served), ("stages", stages), ("restarts", restarts)):
        try:
            out[name] = fn(home, days=days)
        except Exception:  # noqa: BLE001 — one bad ledger must not blank the page
            logger.debug("retrieval report: %s section failed", name, exc_info=True)
            out[name] = None
    for name, fn in (("bench", bench), ("quality", quality)):
        try:
            out[name] = fn(home)
        except Exception:  # noqa: BLE001
            logger.debug("retrieval report: %s section failed", name, exc_info=True)
            out[name] = None
    return out
