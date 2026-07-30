"""latency_replay — warm latency over the searches agents actually ran.

The speed bench's query set: what agents actually searched for, replayed from the
usage ledger. That population is the point. Any curated query set — one mined to
be gradeable, one written to exercise a feature — is selected for something, and
the selection quietly excludes most of what real traffic looks like. Measured on
this archive, time-scoped asks, browse walks, and the sentence punctuation an
agent writes with are all common in the ledger and rare in anything hand-built, so
a change to any of them reads flat on a curated pass while moving real searches by
an order of magnitude.

Real *calls*, not real query text. The parameters are part of the cost: a recorded
``group='browse', match='substring'`` ask replayed as bare text at the default
limit understates it by 12x, and a walk's page 30 carries a pool an order of
magnitude deeper than its page 1. Replaying only the words measures a workload
nobody ran.

**A bench number is not a production number, and this prints both.** The bench
measures warm steady-state with the pool cache off; production is whatever the
serving process happened to be. On this archive they diverge by an order of
magnitude — the run prints the live ratio — while every bench reads "fast", which
is the failure mode the comparison exists to make un-ignorable. The ledger half
comes straight from ``retrieval-usage.jsonl`` over the same window, so a run says
both "what the pipeline costs" and "what agents got" — and when they disagree the
gap is the finding, not a rounding error.

Two things make that comparison honest, and both are easy to get wrong:

- **Weight both sides the same.** The bench replays *distinct* calls, one apiece;
  the ledger is *rows*. One browse walk repeating a query a hundred times can own
  the served median while the bench counts it once, and the ratio then reports a
  difference in traffic mix as a difference in speed. The ``per query`` line is the
  like-for-like one — each query collapsed to its own median first.
- **Split the served population before reading it.** A one-shot ``thread_archive
  search`` process pays a model load by construction and exits; the shared server
  does not; an evicted server pays a fault-in that no warmth flag can see. Those
  have different fixes and a median over all three describes none of them, so the
  run prints them apart — by surface, by whether the request paid a load, and by
  whether the process was still resident.

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

Runs against the **live archive** — deliberately, unlike the benchmark harnesses,
which bind to a frozen corpus. These calls were served by production against
production's corpus, and the point is what they cost there. That has a cost worth
naming: the corpus grows under the measurement, so a delta across weeks is
confounded by ingest and only a same-day before/after is clean. ``--baseline``
records the reference the next run diffs against; the timeseries lands in
``<home>/latency-runs.jsonl`` tagged ``query_set=observed``, never averaged with
rows from another population.

The cold-*start* tail is deliberately not measured here. A first search after a
restart runs an order of magnitude slower (a cold embedder alone is measured at
~5 s against ~20 ms warm) and restarts are frequent, so it is a real cost — but it
is a different problem with different knobs, and averaging it in swamps everything
a ranking or scan change moves. The usage ledger is where that regime is measured
instead, and the production summary below reports it as its own band.

Read that band off the ledger's ``cold`` / ``embed_cold`` / ``matrix_built`` flags,
which are set by the stages that did the work — never off process age. Age is a
proxy that mixes two unrelated populations in both directions: every cache
retrieval leans on is lazy, so an hours-old server serving its first query is fully
cold, while a server restarted a minute ago that has already warmed is not.
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
from thread_archive._retrieval.usage import PROBE_QUERIES  # noqa: E402


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


#: Below this share of its own peak, a process has been evicted rather than merely
#: having freed something.
#:
#: Deliberately far below half. Peak is inflated by transients a healthy server does
#: not hold — a graph build, a torch allocator high-water mark handed back
#: afterwards — so a perfectly resident process sits around 45-60% of its own peak
#: and a threshold near half would flag it. What this has to catch is the
#: catastrophic case, and that one is not close: measured on this archive, an
#: evicted server held 1.3% of peak and paid 1.7 s faulting the vector matrix back.
#: A coarse signal for a coarse condition — read the band as "near-total eviction",
#: never as a residency percentage.
_RESIDENT_FRACTION = 0.25


def _paid_a_load(rec: dict) -> bool:
    """Whether this search built inside the request what a warm one reuses.

    The ledger's own flags, not a proxy for them. Process age was the proxy this
    replaced, and it conflates two unrelated populations: every cache retrieval
    leans on is lazy, so an hours-old server serving its first query is cold, while
    a server restarted a minute ago that has already warmed is not. The flags are
    set by the stages that actually did the work."""
    return bool(rec.get("cold") or rec.get("embed_cold") or rec.get("matrix_built"))


def _evicted(rec: dict) -> bool | None:
    """Whether the serving process had been paged out when this search arrived.

    ``None`` when the row cannot say — the pair of readings is what answers it, and
    peak alone (a high-water mark, which never falls) cannot. This is the regime no
    other field sees: the models are constructed, so :func:`_paid_a_load` is false
    and ``uptime_s`` is large, while the pages backing them are in swap and the
    query pays to fault them back."""
    peak, now = rec.get("rss_mb"), rec.get("rss_now_mb")
    if not peak or now is None:
        return None
    return now < peak * _RESIDENT_FRACTION


def _band(rows: list[dict], keep) -> dict | None:
    """``{n, p50}`` over the rows ``keep`` selects, or None when it selects none."""
    d = [r["duration_ms"] for r in rows if keep(r)]
    return {"n": len(d), "p50": _pct(d, 0.5)} if d else None


def observed_production(home: Path, *, days: int) -> dict | None:
    """What agents actually got, from the ledger, over the last ``days``.

    The other half of the comparison, and the one the bench cannot produce: it is
    served latency, whatever the process's cache state and whatever else the
    machine was doing. Reported beside the bench distribution rather than instead
    of it — the bench says what the pipeline costs under control, this says what
    that turned into, and a wide gap is a fact about the conditions rather than
    about the code.

    Split three ways, because the aggregate over all served searches is a mixture
    of populations with different fixes and describes none of them: by **surface**
    (a one-shot CLI process cannot be warm — it exits before a second query, and no
    amount of daemon warming reaches it), by whether the search **paid a load**
    inside the request, and by whether the serving process had been **evicted**.

    ``by_query_p50`` is the comparable number. The bench replays *distinct* calls,
    one apiece; this file is *rows*, so a single browse walk repeating one query a
    hundred times can carry the served median on its own while the bench weights it
    at one. Collapsing each query to its own median before taking the percentile
    puts both sides on the bench's weighting."""
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

    per_query: dict[str, list[float]] = {}
    for r in rows:
        per_query.setdefault(r.get("query", ""), []).append(r["duration_ms"])
    query_medians = [_pct(xs, 0.5) for xs in per_query.values()]

    by_surface = {}
    for surface in sorted({r.get("surface") or "unattributed" for r in rows}):
        band = _band(rows, lambda r, s=surface: (r.get("surface") or "unattributed") == s)
        if band:
            band["n_paid_load"] = sum(
                1 for r in rows
                if (r.get("surface") or "unattributed") == surface and _paid_a_load(r)
            )
            by_surface[surface] = band

    return {
        "n": len(rows), "p50": _pct(d, 0.5), "p90": _pct(d, 0.9), "p99": _pct(d, 0.99),
        "n_queries": len(per_query), "by_query_p50": _pct(query_medians, 0.5),
        "by_surface": by_surface,
        "warm": _band(rows, lambda r: not _paid_a_load(r)),
        "paid_load": _band(rows, _paid_a_load),
        "resident": _band(rows, lambda r: _evicted(r) is False),
        "evicted": _band(rows, lambda r: _evicted(r) is True),
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
              f"(n={prod['n']} rows over {prod['n_queries']} distinct queries):")
        print(f"  {'served':16s} {prod['p50']:8.1f} {prod['p90']:8.1f} {prod['p99']:8.1f}")
        # Against the bench's own weighting, not the row-weighted median: the two
        # populations differ, and comparing across that difference is what makes a
        # busy browse walk read as a pipeline regression.
        ratio = prod["by_query_p50"] / max(stats.total["p50"], 0.1)
        print(f"  {'per query':16s} {prod['by_query_p50']:8.1f}"
              f"   — {ratio:.1f}x the bench p50, like for like")

        def band(label: str, b: dict | None, note: str = "") -> None:
            if b:
                print(f"    {label:22s} n={b['n']:4d}  p50={b['p50']:8.1f}ms  {note}")

        print("\n    by surface:")
        for surface, b in prod["by_surface"].items():
            paid = b["n_paid_load"]
            note = f"({paid} paid a load in-request)" if paid else ""
            if surface == "cli":
                note += "  one-shot: no daemon warming reaches these"
            band(surface, b, note)

        print("\n    by what the request actually paid for:")
        band("warm", prod["warm"])
        band("paid a load", prod["paid_load"], "model load or matrix build inside the request")

        if prod["resident"] or prod["evicted"]:
            print("\n    by residency (current RSS against this process's own peak):")
            band("resident", prod["resident"])
            band("evicted", prod["evicted"], "pages faulted back from swap")
        else:
            print("\n    (no rss_now_mb on these rows — eviction is not separable here)")

        if ratio > 2.0:
            print("\n    A like-for-like gap this size is about the conditions rather "
                  "than the pipeline:\n    the bench is warm with the pool cache off, "
                  "production is whatever the serving\n    process happened to be. The "
                  "bands above say which condition.")

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
