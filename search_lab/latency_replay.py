"""latency_replay — warm latency over the searches agents actually ran.

The speed bench's other query set. ``retrieval_gold_gate.py --latency`` measures
the gold files; this measures the usage ledger. They are different populations and
the difference is the point: a gold case is *mined to be gradeable* — a query with
a knowable right answer — and that selection quietly excludes most of what real
traffic looks like. Measured on this archive, time-scoped asks, browse walks, and
the sentence punctuation an agent writes with are all common in the ledger and
near-absent from the golds, so a change to any of them scores flat on the gold
latency pass while moving real searches by an order of magnitude.

Real *calls*, not real query text. The parameters are part of the cost: a recorded
``group='browse', match='substring'`` ask replayed as bare text at the default
limit understates it by 12x, and a walk's page 30 carries a pool an order of
magnitude deeper than its page 1. Replaying only the words measures a workload
nobody ran.

**A bench number is not a production number, and this prints both.** The bench
measures warm steady-state with the pool cache off; production is whatever the
serving process happened to be. On this archive they diverge by an order of
magnitude — the run prints the live ratio — while every bench reads "fast", which
is the failure mode the comparison exists to make un-ignorable. Restarts are what
drive it: the ledger's own cold/settled split is printed beside the ratio, and the
settled half tracks the bench. The ledger half comes straight from ``retrieval-usage.jsonl`` over
the same window, so a run says both "what the pipeline costs" and "what agents got"
— and when they disagree the gap is the finding, not a rounding error.

    .venv/bin/python search_lab/latency_replay.py                   # replay the ledger
    .venv/bin/python search_lab/latency_replay.py --limit 40         # 40 most recent calls
    .venv/bin/python search_lab/latency_replay.py --cold             # first-sight regime
    .venv/bin/python search_lab/latency_replay.py --reps 5 --baseline
    .venv/bin/python search_lab/latency_replay.py --json out.json

``--cold`` drops the per-query warmup and times a single first sight of each call,
which is the regime production actually serves — agents rarely repeat a query. On
this corpus it measures ~1.1x the warm number, so the *page cache* is not where
production loses its time; keep it for confirming that on a corpus that has grown,
not as the default (one rep is one sample, and latency is a distribution).

Runs against the **live archive** — deliberately, unlike the gold instruments,
which bind to a frozen snapshot. These calls were served by production against
production's corpus, and the point is what they cost there. That has a cost worth
naming: the corpus grows under the measurement, so a delta across weeks is
confounded by ingest and only a same-day before/after is clean. ``--baseline``
records the reference the next run diffs against; the timeseries lands in
``<home>/latency-runs.jsonl`` tagged ``query_set=observed``, beside the gold rows
and never averaged with them.

The cold-*start* tail is deliberately not measured here. A first search after a
restart runs an order of magnitude slower (a cold embedder alone is measured at
~5 s against ~20 ms warm) and restarts are frequent, so it is a real cost — but it
is a different problem with different knobs, and averaging it in swamps everything
a ranking or scan change moves. ``uptime_s`` in the usage ledger is where that
regime is measured instead, and the production summary below reports it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# The lab dir too, so bare sibling imports (speed, eval_core, …) resolve however
# this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import speed  # noqa: E402

from thread_archive._config import resolve_paths  # noqa: E402
from thread_archive._retrieval import usage  # noqa: E402

#: Queries a bench or a smoke test left in the ledger rather than an agent asking
#: something. Replaying them measures nothing and skews the distribution toward the
#: trivial — an empty query and a one-character probe both return in ~1 ms.
PROBE_QUERIES = ("x", "test", "warmup", "hello")


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(int(len(xs) * q), len(xs) - 1)]


def _delta(now: float, before: float | None) -> str:
    """A stage's movement against the baseline, or blank when there is none."""
    if not before:
        return ""
    pct = 100.0 * (now - before) / before
    return "        =" if abs(pct) < 2.0 else f"  {pct:+6.1f}%"


def observed_production(home: Path, *, days: int) -> dict | None:
    """What agents actually got, from the ledger, over the last ``days``.

    The other half of the comparison, and the one the bench cannot produce: it is
    served latency, whatever the process's cache state and whatever else the
    machine was doing. Reported beside the bench distribution rather than instead
    of it — the bench says what the pipeline costs under control, this says what
    that turned into, and a wide gap is a fact about the conditions rather than
    about the code."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows: list[dict] = []
    try:
        with open(home / usage.LEDGER_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (rec.get("kind") == "search" and rec.get("duration_ms")
                        and rec.get("at", "") >= cutoff
                        and rec.get("query") not in PROBE_QUERIES):
                    rows.append(rec)
    except OSError:
        return None
    if not rows:
        return None
    d = [r["duration_ms"] for r in rows]
    aged = [r for r in rows if r.get("uptime_s") is not None]
    young = [r["duration_ms"] for r in aged if r["uptime_s"] < 120]
    settled = [r["duration_ms"] for r in aged if r["uptime_s"] >= 120]
    return {
        "n": len(rows), "p50": _pct(d, 0.5), "p90": _pct(d, 0.9), "p99": _pct(d, 0.99),
        "n_aged": len(aged),
        "young_p50": _pct(young, 0.5) if young else None, "n_young": len(young),
        "settled_p50": _pct(settled, 0.5) if settled else None, "n_settled": len(settled),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="replay only the N most recent distinct calls (default: all)")
    ap.add_argument("--reps", type=int, default=3, metavar="N",
                    help="timed runs per call (default 3); latency is a distribution")
    ap.add_argument("--cold", action="store_true",
                    help="no per-query warmup, one rep — first sight, production's regime")
    ap.add_argument("--days", type=int, default=7, metavar="N",
                    help="window for the production comparison (default 7)")
    ap.add_argument("--baseline", action="store_true",
                    help="record this run as the reference later runs diff against")
    ap.add_argument("--json", metavar="PATH", help="also write the distribution as JSON")
    args = ap.parse_args(argv)

    home = resolve_paths().home
    calls = usage.read_calls(limit=args.limit, exclude=PROBE_QUERIES)
    if not calls:
        print(f"latency_replay: no searches recorded at {home / usage.LEDGER_FILE}")
        print("  the ledger is the query set — let agents search, then replay.")
        return 0

    reps = 1 if args.cold else args.reps
    before = speed.read_baseline(home, query_set=speed.OBSERVED_SET)
    print(f"latency_replay: {len(calls)} distinct calls x {reps} rep(s), "
          f"{'first sight' if args.cold else 'warm'}, pool cache off")
    if before:
        print(f"  baseline: {before['at'][:19]} @ {before.get('commit', '?')} "
              f"(n={before.get('n_queries')})")

    def progress(i: int, n: int) -> None:
        if i and i % 20 == 0:
            print(f"  {i}/{n}...", flush=True)

    stats = speed.measure(calls, reps=reps, warmup=not args.cold, on_query=progress)

    b_total = (before or {}).get("total") or {}
    print(f"\n  {'':16s} {'p50':>8s} {'p95':>8s} {'p99':>8s}   vs baseline (p50)")
    print(f"  {'total':16s} {stats.total['p50']:8.1f} {stats.total['p95']:8.1f} "
          f"{stats.total['p99']:8.1f} {_delta(stats.total['p50'], b_total.get('p50'))}")
    b_stages = (before or {}).get("stages") or {}
    for stage, dist in stats.stages.items():
        if dist["p95"] < 1.0:
            continue  # a stage that never costs a millisecond is noise in the table
        base = (b_stages.get(stage) or {}).get("p50")
        print(f"  {stage:16s} {dist['p50']:8.1f} {dist['p95']:8.1f} {dist['p99']:8.1f} "
              f"{_delta(dist['p50'], base)}")

    print(f"\n  pool p50 {stats.pool_p50:.0f} rows "
          f"· {stats.n_samples} samples")
    for shape, d in stats.by_shape.items():
        print(f"  {shape:16s} {int(d['n']):5d} {d['p50']:8.1f} {d['p95']:8.1f}")

    print("\n  slowest calls (p50):")
    for q, ms in sorted(stats.by_query.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {ms:8.0f}ms  {q[:64]!r}")

    prod = observed_production(home, days=args.days)
    if prod:
        print(f"\n  what agents actually got, last {args.days}d "
              f"(n={prod['n']}, from the usage ledger):")
        print(f"  {'served':16s} {prod['p50']:8.1f} {prod['p90']:8.1f} {prod['p99']:8.1f}")
        ratio = prod["p50"] / max(stats.total["p50"], 0.1)
        print(f"  served p50 is {ratio:.1f}x the bench p50.")
        if prod["n_aged"]:
            y = f"{prod['young_p50']:.0f}ms (n={prod['n_young']})" if prod["young_p50"] else "—"
            s_ = f"{prod['settled_p50']:.0f}ms (n={prod['n_settled']})" if prod["settled_p50"] else "—"
            print(f"    process <2min old: {y}   ·   settled: {s_}")
        else:
            print("    (no uptime_s on these rows — cold and warm are not separable "
                  "in this window)")
        if ratio > 2.0:
            print("    A gap this size is about the conditions, not the pipeline: the "
                  "bench is warm\n    with the pool cache off, production is whatever "
                  "the serving process happened to be.")

    speed.record_run(home, snapshot_id=None, stats=stats, query_set=speed.OBSERVED_SET)
    if args.baseline:
        speed.write_baseline(home, snapshot_id=None, stats=stats,
                             query_set=speed.OBSERVED_SET)
        print(f"\n  baseline written to {home / speed.baseline_file(speed.OBSERVED_SET)}")
    if args.json:
        blob = stats.as_record(include_by_query=True)
        blob["production"] = prod
        Path(args.json).write_text(json.dumps(blob, indent=2), encoding="utf-8")
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
