"""Latency measurement for the retrieval pipeline — the speed half of tuning.

The gold gate asks *does search still find the right thing*; this asks *how long
does it take*, and the two answers come off the same query set so one change reads
on both axes at once. That pairing is the point: the dominant quality lever (the
cross-encoder re-rank) is also the dominant latency, so rebuilding quality means
re-enabling the re-rank *within a latency budget* — a joint decision that needs
both numbers side by side.

Three things make measuring latency unlike scoring quality, and they shape this
API:

- **Latency is a distribution, not a value.** A single search is noise (GC,
  thermal, matvec load, CPU contention); the report is p50/p95/p99 over
  ``query × rep`` samples, and the tail is the number that bites an MCP client's
  timeout.
- **The pool cache is suspended for the duration.**
  :mod:`thread_archive._retrieval.pool_cache` makes a *quality* sweep fast by
  skipping the FTS scan, the embed, and the matvec — which are exactly the stages
  under measurement here. Timing a search through a cache would time cache
  lookups. :func:`measure` enforces the suspension; it is not left to the caller.
- **Cold and warm are separate regimes.** This measures WARM steady-state: a
  warmup pass loads the models and fills the process caches and its timings are
  discarded. The cold-load tail (tens of seconds, once per process) is a
  different problem with different knobs (``warm_models`` / ``defer_construction``)
  and is not what a ranking-knob sweep moves — so it is deliberately excluded
  rather than averaged in, where it would swamp the steady-state signal.

The per-stage split comes from the same :class:`~thread_archive._retrieval._probe.SearchProbe`
the usage ledger records in production, so a bench number and a production number
are the same measurement taken two ways.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Optional

from .._retrieval import _probe

logger = logging.getLogger(__name__)

LATENCY_RUNS_FILE = "latency-runs.jsonl"
LATENCY_BASELINE_FILE = "latency-baseline.json"

#: The stages the probe attributes wall-clock to. Total is measured outside the
#: probe (the wall-clock the caller feels); the arm totals sum to less than it (the
#: ranking arithmetic and enrichment are the unattributed remainder).
#:
#: Each arm's sub-stages ride along after the three arm totals, so a bench reports
#: the same split production does — a regression that lands entirely inside
#: ``semantic_ms`` still says which half of it moved. They nest inside their arm
#: rather than adding to it, so only the first three are summable against the total.
ARM_STAGES = ("fts_ms", "semantic_ms", "rerank_ms")
SEMANTIC_SUBSTAGES = _probe.SEMANTIC_SUBSTAGES
FTS_SUBSTAGES = _probe.FTS_SUBSTAGES
STAGES = ARM_STAGES + SEMANTIC_SUBSTAGES + FTS_SUBSTAGES


def percentile(xs: list[float], q: float) -> float:
    """The ``q`` quantile of ``xs`` by nearest-rank (0.0 for an empty list).

    Nearest-rank, not interpolated: with the modest sample counts a bench
    produces (a few reps over ~100 queries), an interpolated p99 invents a value
    between two real observations, and the honest answer is "the worst few
    searches actually took this long"."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    rank = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[rank]


@dataclass
class LatencyStats:
    """The warm-latency distribution of one configuration over one query set.

    ``total`` and each stage carry ``{p50, p95, p99}`` milliseconds; ``rerank_rate``
    is the fraction of searches that actually invoked the cross-encoder (the knob a
    ``rerank_auto`` change moves), ``by_shape`` the p50/p95 of total latency split
    by query shape (a code-identifier query and a conceptual one take different
    paths, and a mean over both describes neither)."""

    n_queries: int
    reps: int
    n_samples: int
    total: dict[str, float]
    stages: dict[str, dict[str, float]]
    rerank_rate: float
    pool_p50: float
    by_shape: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Per-query p50 total latency — the raw material for the pathological-query
    #: smoke test. Kept in the baseline, not the ledger row (it is a reference to
    #: cherry-pick from, not a timeseries datum).
    by_query: dict[str, float] = field(default_factory=dict)

    def as_record(self, *, include_by_query: bool = False) -> dict[str, Any]:
        """The distribution as a JSON row. The compact form (ledger timeseries)
        drops the per-query map; ``include_by_query`` keeps it (the baseline, which
        the smoke test reads back)."""
        rec: dict[str, Any] = {
            "n_queries": self.n_queries, "reps": self.reps, "n_samples": self.n_samples,
            "total": {k: round(v, 1) for k, v in self.total.items()},
            "stages": {s: {k: round(v, 1) for k, v in d.items()}
                       for s, d in self.stages.items()},
            "rerank_rate": round(self.rerank_rate, 3),
            "pool_p50": round(self.pool_p50, 1),
        }
        if include_by_query:
            rec["by_query"] = {q: round(v, 1) for q, v in self.by_query.items()}
        return rec


def _summarize(samples: list[dict], n_queries: int, reps: int) -> LatencyStats:
    """Aggregate raw per-rep records into the distribution. Split out from
    :func:`measure` so it can be tested without a search pipeline."""
    def dist(xs: list[float]) -> dict[str, float]:
        return {"p50": percentile(xs, 0.50), "p95": percentile(xs, 0.95),
                "p99": percentile(xs, 0.99)}

    totals = [s["total_ms"] for s in samples]
    by_shape: dict[str, dict[str, float]] = {}
    shapes: dict[str, list[float]] = {}
    per_query: dict[str, list[float]] = {}
    for s in samples:
        shapes.setdefault(s["shape"], []).append(s["total_ms"])
        per_query.setdefault(s["query"], []).append(s["total_ms"])
    for shape, xs in sorted(shapes.items()):
        by_shape[shape] = {"n": len(xs), "p50": percentile(xs, 0.50),
                           "p95": percentile(xs, 0.95)}
    reranked = sum(1 for s in samples if s.get("did_rerank"))
    return LatencyStats(
        n_queries=n_queries, reps=reps, n_samples=len(samples),
        total=dist(totals),
        stages={stage: dist([s.get(stage, 0.0) for s in samples]) for stage in STAGES},
        rerank_rate=reranked / len(samples) if samples else 0.0,
        pool_p50=percentile([float(s.get("pool_size", 0)) for s in samples], 0.50),
        by_shape=by_shape,
        by_query={q: percentile(xs, 0.50) for q, xs in per_query.items()},
    )


def smoke_set(baseline: Optional[dict[str, Any]], k: int) -> list[str]:
    """The ``k`` queries that were slowest at baseline — the corpus's own
    pathological cases, empirically rather than by guesswork.

    A latency regression shows worst on the queries already nearest the ceiling,
    so running these first turns a full ~140-query pass into a ~10-query smoke
    test that catches the common regression (a change that uniformly slows the
    pipeline, or worsens the already-heavy paths) in tens of seconds. Empty when
    no baseline has been recorded — nothing to cherry-pick from, so the smoke test
    simply doesn't run rather than guessing which queries are hard."""
    by_query = (baseline or {}).get("by_query") or {}
    return [q for q, _ in sorted(by_query.items(), key=lambda kv: -kv[1])[:k]]


def ceiling_ms(baseline: Optional[dict[str, Any]], *, budget_ms: Optional[float],
               factor: float) -> Optional[float]:
    """The p95 a smoke run must stay under. An explicit ``budget_ms`` is an
    absolute acceptability bar; otherwise the ceiling is ``factor`` times the
    baseline's total p95 — a relative "don't get materially slower" ratchet. None
    when neither is available (no budget, no baseline): nothing to check against,
    so the smoke test can only measure, not fail."""
    if budget_ms is not None:
        return budget_ms
    if baseline and baseline.get("total", {}).get("p95"):
        return baseline["total"]["p95"] * factor
    return None


def measure(
    queries: list[str],
    *,
    search: Optional[Callable] = None,
    reps: int = 3,
    warmup: bool = True,
    limit: int = 20,
    on_query: Optional[Callable[[int, int], None]] = None,
) -> LatencyStats:
    """Warm-latency distribution of ``search`` over ``queries``.

    Each query runs ``reps`` timed times; with ``warmup`` an extra untimed run
    precedes them so the models and process caches are hot and only steady-state
    is measured. The pool cache is **suspended** throughout — the whole cost is
    the point, so it must not be skipped. ``search`` defaults to the production
    pipeline; a candidate configuration is passed as a closure over
    ``search(params=...)`` (the same seam the gold gate's ``--set`` uses).
    ``on_query(i, n)`` is an optional progress callback fired per query.

    Total latency is the wall-clock around each ``search`` call; the per-stage
    split is read from a fresh probe installed per rep. Hits are discarded — this
    measures time, not relevance (the quality pass measures relevance, over the
    same queries)."""
    from .. import _api as api
    from .._eval import query_shape
    from .._retrieval import _probe, pool_cache

    if search is None:
        search = api.search

    samples: list[dict] = []
    # One suspension around the whole loop: even if an outer scope (a tuning
    # session) has a cache installed, latency must be measured against the live
    # index, not its cached pools.
    with pool_cache.suspend():
        for i, query in enumerate(queries):
            if on_query is not None:
                on_query(i, len(queries))
            if warmup:
                try:
                    search(query, limit=limit)
                except Exception:  # noqa: BLE001 — a query that errors is not a timing
                    logger.debug("latency warmup failed for %r", query, exc_info=True)
                    continue
            shape = query_shape(query)
            for _ in range(reps):
                with _probe.install() as probe:
                    t0 = perf_counter()
                    try:
                        search(query, limit=limit)
                    except Exception:  # noqa: BLE001 — skip the query, don't skew the stats
                        logger.debug("latency rep failed for %r", query, exc_info=True)
                        break
                    total_ms = (perf_counter() - t0) * 1000.0
                rec = probe.as_record()
                rec["total_ms"] = total_ms
                rec["shape"] = shape
                rec["query"] = query
                samples.append(rec)
    return _summarize(samples, n_queries=len(queries), reps=reps)


# --- the timeseries + baseline (mirrors _ops.gold_runs) ----------------------


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_LATENCY_LOG", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def record_run(
    home: Path, *, snapshot_id: Optional[str], stats: LatencyStats,
    config: Optional[dict[str, Any]] = None, overrides: Optional[dict[str, Any]] = None,
) -> None:
    """Append one latency measurement to ``<home>/latency-runs.jsonl`` — the
    speed timeseries beside the quality one, so a latency regression is a lookup,
    not a re-run of the old code. ``overrides`` flags a tuning run. Fail-soft."""
    if not _enabled():
        return
    from . import gold_runs

    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "latency-run",
        "snapshot_id": snapshot_id,
        "commit": gold_runs.git_commit(),
        "config": config if config is not None else gold_runs.active_config(),
        **stats.as_record(),
    }
    if overrides:
        record["overrides"] = overrides
    try:
        import json

        with open(home / LATENCY_RUNS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record latency run", exc_info=True)


def write_baseline(home: Path, *, snapshot_id: Optional[str], stats: LatencyStats) -> None:
    """Overwrite ``<home>/latency-baseline.json`` with the shipped config's warm
    distribution — the reference a ``--set`` run diffs against. Written only by a
    full, unmodified run, for the same reason its quality twin is."""
    if not _enabled():
        return
    import json

    from . import gold_runs

    blob = {
        "at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id, "commit": gold_runs.git_commit(),
        **stats.as_record(include_by_query=True),
    }
    try:
        path = home / LATENCY_BASELINE_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("could not write latency baseline", exc_info=True)


def read_baseline(home: Path, *, snapshot_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The recorded warm-latency baseline, or ``None``. A ``snapshot_id`` mismatch
    reads as absent: latency over a different corpus (a different pool size, a
    different vector count) is not a comparable reference."""
    import json

    path = home / LATENCY_BASELINE_FILE
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if snapshot_id is not None and blob.get("snapshot_id") != snapshot_id:
        return None
    return blob
