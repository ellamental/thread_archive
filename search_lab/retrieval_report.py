"""How retrieval is doing, assembled from the three ledgers that record it.

A bench instrument, read deliberately::

    .venv/bin/python search_lab/retrieval_report.py --hours 336

It reads latency and quality series and says nothing a user of the archive could
act on, so it stays lab-side: a served-latency percentile is a fact about the
machine and the model cache, and the quality series is scored against golds whose
limits only someone holding the protocol can weigh. The viewer renders it at
``/retrieval`` through ``thread_archive._dev``, which is a *dev* page — excluded
from the wheel, 404 in an install — rather than a second home for these numbers.

Three files answer three different questions and none of them answers alone:

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

HOUR = "hour"
DAY = "day"

#: The default window, and the one the viewer opens on.
DEFAULT_HOURS = 14 * 24

#: Windows this short or shorter are bucketed by the hour. Three days of hourly
#: points is 72 — fine enough to show today's shape and to place a slow patch
#: against the restart that caused it, coarse enough that a bucket usually holds
#: more than one search. Past it the buckets are days: an hourly median over a
#: month is mostly a chart of when nobody was working.
HOURLY_MAX_HOURS = 72


def default_bucket(hours: int) -> str:
    """``hour`` or ``day`` for a window, by :data:`HOURLY_MAX_HOURS`."""
    return HOUR if hours <= HOURLY_MAX_HOURS else DAY


def percentile(xs: list[float], q: float) -> float:
    """Nearest-rank percentile. Zero for an empty sample — a chart point that does
    not exist is drawn as absent by the caller, never as a real zero."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    return ordered[min(int(len(ordered) * q), len(ordered) - 1)]


def _bucket_key(at: str, bucket: str) -> str:
    """Which bucket an ISO timestamp falls in, sliced rather than parsed.

    UTC, because that is what the ledger records; rendering the label in the
    operator's own clock is the viewer's job, and only for hourly buckets — a
    daily bucket is a UTC calendar day, and relabelling it locally would name a
    different day than the one it aggregates."""
    return at[:13] if bucket == HOUR else at[:10]


def _bucket_span(hours: int, bucket: str) -> list[str]:
    """Every bucket key in the window, including the ones with nothing in them.

    Emitting only the buckets that saw traffic would compress the axis onto the
    times something happened, and a chart drawn over that joins Tuesday to Friday
    as though the days between were steady. A quiet stretch is a fact about the
    window, and at hourly resolution it is most of one."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    if bucket == HOUR:
        t, step, width = cutoff.replace(minute=0, second=0, microsecond=0), timedelta(hours=1), 13
    else:
        t, step, width = (cutoff.replace(hour=0, minute=0, second=0, microsecond=0),
                          timedelta(days=1), 10)
    keys = []
    while t <= now:
        keys.append(t.isoformat()[:width])
        t += step
    return keys


def _band(xs: list[float], *, p99: bool = False) -> dict[str, Any]:
    out = {"n": len(xs), "p50": round(percentile(xs, 0.50), 1),
           "p90": round(percentile(xs, 0.90), 1)}
    if p99:
        out["p99"] = round(percentile(xs, 0.99), 1)
    return out


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


def _searches(home: Path, *, hours: int) -> list[dict]:
    """Real agent searches within the window — probes dropped, failures kept.

    Failures stay because dropping slow errors biases every percentile toward the
    searches that happened to succeed, which is the same reason the ledger records
    them in the first place."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    from thread_archive._retrieval.usage import LEDGER_FILE

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


def served(home: Path, *, hours: int = DEFAULT_HOURS,
           bucket: Optional[str] = None) -> dict[str, Any]:
    """What agents got, per bucket and per regime.

    ``buckets`` carries one entry per hour or per day — see :func:`default_bucket`
    — with a count and p50/p90 per regime, so the chart can draw warm and cold as
    separate series. Drawing them as one is the thing this exists to prevent: over
    a restart-heavy window the combined median tracks the restart rate rather than
    the code."""
    bucket = bucket or default_bucket(hours)
    rows = _searches(home, hours=hours)
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        grouped[_bucket_key(r["at"], bucket)][_regime(r)].append(r["duration_ms"])
    buckets = []
    for key in _bucket_span(hours, bucket):
        found = grouped.get(key, {})
        entry: dict[str, Any] = {"at": key, "n": sum(len(v) for v in found.values())}
        for regime, xs in found.items():
            entry[regime] = _band(xs)
        buckets.append(entry)
    warm = [r["duration_ms"] for r in rows if _regime(r) == "warm"]
    cold = [r["duration_ms"] for r in rows if _regime(r) == "cold"]
    return {
        "hours": hours,
        "bucket": bucket,
        "n": len(rows),
        "n_unknown_regime": sum(1 for r in rows if _regime(r) == "unknown"),
        "buckets": buckets,
        "warm": _band(warm, p99=True),
        "cold": _band(cold, p99=True),
    }


def stages(home: Path, *, hours: int = DEFAULT_HOURS) -> dict[str, Any]:
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
    rows = [r for r in _searches(home, hours=hours) if _regime(r) != "cold"]
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


def restarts(home: Path, *, hours: int = DEFAULT_HOURS,
             bucket: Optional[str] = None) -> dict[str, Any]:
    """Process starts per bucket, counted off the warm-pass rows, with what they cost.

    On this deployment the restart rate is the single largest influence on served
    latency — every cache retrieval leans on is process-local — so it belongs
    beside the latency charts rather than in a separate operational corner.

    Unlike :func:`served` this is sparse: it is read as a table, and a table of
    empty rows is only noise where an empty chart point is information."""
    bucket = bucket or default_bucket(hours)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    from thread_archive._retrieval.usage import LEDGER_FILE

    rows = [r for r in _rows(home / LEDGER_FILE)
            if r.get("kind") == "warm" and r.get("at", "") >= cutoff]
    per_bucket = Counter(_bucket_key(r["at"], bucket) for r in rows)
    durations = [r["duration_ms"] for r in rows if r.get("duration_ms")]
    return {
        "n": len(rows),
        "bucket": bucket,
        "buckets": [{"at": b, "n": per_bucket[b]} for b in sorted(per_bucket)],
        "p50_ms": round(percentile(durations, 0.50), 1),
        "total_s": round(sum(durations) / 1000.0, 1),
    }


def bench(home: Path, *, limit: int = 40) -> dict[str, list[dict[str, Any]]]:
    """The controlled latency timeseries, split by query set.

    Never merged: the gold files and the usage ledger are different populations of
    query, so a p50 over one is not a point on the other's line."""
    import speed

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
    import gold_runs

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


def report(home: Optional[Path] = None, *, hours: int = DEFAULT_HOURS,
           bucket: Optional[str] = None) -> dict[str, Any]:
    """The whole page's data in one call. Every section is independently fail-soft:
    a missing or unreadable ledger yields an empty section, never a failed page —
    an operator view that goes blank when one input is absent is the least useful
    thing it could do."""
    if home is None:
        from thread_archive._config import resolve_paths

        home = resolve_paths().home
    bucket = bucket or default_bucket(hours)
    out: dict[str, Any] = {"home": str(home), "hours": hours, "bucket": bucket,
                           "at": datetime.now(timezone.utc).isoformat()}

    def section(fn, **kw):
        try:
            return fn(home, **kw)
        except Exception:  # noqa: BLE001 — one bad ledger must not blank the page
            logger.debug("retrieval report: %s section failed", fn.__name__, exc_info=True)
            return None

    out["served"] = section(served, hours=hours, bucket=bucket)
    out["stages"] = section(stages, hours=hours)
    out["restarts"] = section(restarts, hours=hours, bucket=bucket)
    out["bench"] = section(bench)
    out["quality"] = section(quality)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    """Print the report as JSON — the series, for reading or piping into a plot."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="search_lab/retrieval_report.py",
        description="Latency and quality series off the retrieval ledgers.")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help=f"window to summarize (default {DEFAULT_HOURS})")
    parser.add_argument("--bucket", choices=(HOUR, DAY),
                        help="series granularity (default: by window size)")
    parser.add_argument("--home", help="archive home (default: $THREAD_ARCHIVE_HOME)")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else None
    print(json.dumps(report(home, hours=args.hours, bucket=args.bucket), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
