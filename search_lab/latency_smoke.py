"""latency_smoke — the speed regression gate, for the CI sweeper to run.

    .venv/bin/python search_lab/latency_smoke.py            # measure and check
    .venv/bin/python search_lab/latency_smoke.py --seed     # (re)establish the reference

Everything else in this lab is an instrument a human points at a question. This
one is a gate: it runs unattended on every commit, it measures a fixed handful of
calls, and it exits non-zero when they got materially slower. The pieces it is
built from — the pathological-query pick, the relative ceiling, the per-set
baseline — have existed in :mod:`speed` all along with nothing invoking them, so a
latency regression was caught only when somebody thought to look. This is the
thing that looks.

**What it measures.** The slowest calls agents actually made
(``retrieval-usage.jsonl``, via :func:`speed.smoke_set` once a reference exists),
replayed with their recorded parameters. Not a curated set: the parameters are
most of the cost, and the tail is where a regression shows first. Warm, pool cache
off, exactly as :func:`speed.measure` defines the regime.

**What it compares against.** :data:`speed.SMOKE_SET`'s own baseline, priced by
:func:`speed.ceiling_ms` against the recorded cost of *these* queries rather than a
corpus-wide percentile. No baseline means no verdict: the run seeds one and passes,
which is the only honest thing a first run can do.

**Why it is allowed to be a CI row when the quality bench is not.** A quality
number over this archive's own labels grades the ranker with itself
(``docs/search-quality.md``). A millisecond has no such problem — it is the same
millisecond however the labels were made, which is the reason
``search_lab/speed.py`` exists at all. What a latency gate has instead is *noise*,
and that is a measurement problem with measurement answers, applied here:

- its own baseline, recorded from this same lane, so the reference carries the
  same background scheduling tier as the run;
- a ratchet on the slowest queries' own recorded timings, not a corpus p95;
- a generous factor — this catches an order-of-magnitude regression, which is what
  latency regressions here have actually looked like, not a 20% drift;
- and a confirm pass: a breach is re-measured before it fails the row, because a
  single unlucky window on a shared machine is the likeliest explanation for one
  bad number and the cheapest to rule out.

It reports the machine's load with every verdict, so a failure that *was* the
machine says so on its face.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import speed  # noqa: E402

from thread_archive._config import resolve_paths  # noqa: E402
from thread_archive._ops import machine  # noqa: E402
from thread_archive._retrieval import usage  # noqa: E402
from thread_archive._retrieval.usage import PROBE_QUERIES  # noqa: E402

#: How many of the slowest calls the gate replays. Enough that one anomalous query
#: cannot own the p95, few enough that the row costs tens of seconds rather than
#: the minutes a full replay does.
SMOKE_K = 8

#: Timed runs per call. Three is the smallest number from which a p95 is not
#: simply the maximum of two samples.
SMOKE_REPS = 3

#: How much slower than baseline is a regression. Deliberately loose: the gate runs
#: beside whatever else the sweeper and the operator's machine are doing, and a
#: tight ratchet on a shared box fails for reasons that have nothing to do with the
#: commit — which is how a gate gets ignored, and then removed.
SMOKE_FACTOR = 2.0

#: An absolute ceiling, when the operator wants one instead of a ratchet
#: (``THREAD_ARCHIVE_LATENCY_BUDGET_MS``). Unset by default: what matters here is
#: not getting slower, and the archive it runs against grows on its own.
BUDGET_ENV = "THREAD_ARCHIVE_LATENCY_BUDGET_MS"


def _budget_ms() -> float | None:
    raw = os.environ.get(BUDGET_ENV, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"latency_smoke: ignoring unparseable {BUDGET_ENV}={raw!r}")
        return None


def pick_calls(baseline: dict | None, calls: list) -> list:
    """The calls to replay: the ones baseline says are slowest, else the newest.

    A call is ``(query, kwargs)``; the baseline's ``by_query`` is keyed by query
    text alone, so the pick is by text and the *parameters come from the ledger* —
    which is what keeps a browse walk's page 30 measured as page 30. When several
    recorded calls share a query text, the first (most recent) wins: they are the
    same question asked with different arguments, and the recent one is the shape
    traffic currently takes.

    Without a baseline there is nothing to call slowest, so the newest calls stand
    in — they only have to be a *fixed* set for the reference to mean anything, and
    the next run inherits the real pick from the baseline this one writes.
    """
    wanted = speed.smoke_set(baseline, SMOKE_K)
    if not wanted:
        return calls[:SMOKE_K]
    by_text: dict[str, object] = {}
    for entry in calls:
        text = entry[0] if isinstance(entry, tuple) else entry
        by_text.setdefault(text, entry)
    picked = [by_text[q] for q in wanted if q in by_text]
    # A baseline query that has aged out of the ledger's retained window leaves the
    # set short; top it up from the newest rather than measuring three calls and
    # calling it a distribution.
    if len(picked) < SMOKE_K:
        seen = {e[0] if isinstance(e, tuple) else e for e in picked}
        for entry in calls:
            text = entry[0] if isinstance(entry, tuple) else entry
            if text not in seen:
                picked.append(entry)
                seen.add(text)
            if len(picked) >= SMOKE_K:
                break
    return picked


def _texts(calls: list) -> list[str]:
    return [c[0] if isinstance(c, tuple) else c for c in calls]


def main(argv: list[str] | None = None, *, search=None) -> int:
    """Run the gate. ``search`` is :func:`speed.measure`'s own seam — a callable
    standing in for the production pipeline — so a tuning pass can gate a candidate
    configuration, and so this is checkable without a model load."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", action="store_true",
                    help="record this run as the reference and pass, replacing any baseline")
    ap.add_argument("--reps", type=int, default=SMOKE_REPS, metavar="N",
                    help=f"timed runs per call (default {SMOKE_REPS})")
    ap.add_argument("--factor", type=float, default=SMOKE_FACTOR, metavar="X",
                    help=f"how much slower than baseline fails (default {SMOKE_FACTOR})")
    ap.add_argument("--json", metavar="PATH", help="also write the verdict as JSON")
    args = ap.parse_args(argv)

    home = resolve_paths().home
    calls = usage.read_calls(exclude=PROBE_QUERIES)
    if not calls:
        # Not a failure: a fresh install has no traffic, and a gate that reds on an
        # archive nobody has searched yet is a gate that gets disabled on day one.
        print(f"latency_smoke: no searches recorded at {home / usage.LEDGER_FILE} — nothing to gate")
        return 0

    baseline = None if args.seed else speed.read_baseline(home, query_set=speed.SMOKE_SET)
    picked = pick_calls(baseline, calls)
    texts = _texts(picked)
    ceiling = speed.ceiling_ms(
        baseline, budget_ms=_budget_ms(), factor=args.factor, queries=texts,
    )

    load = machine.load1()
    print(f"latency_smoke: {len(picked)} call(s) x {args.reps} rep(s), warm, pool cache off"
          + (f" (load {load})" if load is not None else ""))
    stats = speed.measure(picked, reps=args.reps, search=search)
    p95 = stats.total.get("p95", 0.0)
    print(f"  p50 {stats.total.get('p50', 0.0):8.1f} ms")
    print(f"  p95 {p95:8.1f} ms" + (f"   ceiling {ceiling:.1f} ms" if ceiling else ""))

    verdict: dict = {
        "query_set": speed.SMOKE_SET, "n_calls": len(picked), "reps": args.reps,
        "p50": stats.total.get("p50", 0.0), "p95": p95, "ceiling_ms": ceiling,
        "load1": load, "queries": texts,
    }

    if ceiling is None:
        speed.write_baseline(home, snapshot_id=None, stats=stats,
                             query_set=speed.SMOKE_SET)
        print(f"  no reference — baseline written to {home / speed.baseline_file(speed.SMOKE_SET)}")
        print("  (the next run is the first that can fail)")
        verdict["outcome"] = "seeded"
    elif p95 <= ceiling:
        speed.record_run(home, snapshot_id=None, stats=stats, query_set=speed.SMOKE_SET)
        print("  OK")
        verdict["outcome"] = "ok"
    else:
        # Confirm before failing. One measurement on a shared machine is one
        # sample of the machine as much as of the code, and a gate that cries
        # wolf on that is worse than no gate.
        print(f"  over ceiling by {p95 - ceiling:.1f} ms — re-measuring to confirm")
        again = speed.measure(picked, reps=args.reps, search=search)
        p95_again = again.total.get("p95", 0.0)
        load_again = machine.load1()
        print(f"  p95 {p95_again:8.1f} ms   ceiling {ceiling:.1f} ms"
              + (f" (load {load_again})" if load_again is not None else ""))
        verdict["p95_confirm"] = p95_again
        verdict["load1_confirm"] = load_again
        speed.record_run(home, snapshot_id=None, stats=again, query_set=speed.SMOKE_SET)
        if p95_again <= ceiling:
            print("  OK on the confirm pass — first pass was noise, not a regression")
            verdict["outcome"] = "ok-on-confirm"
        else:
            print(f"  FAIL: p95 {p95_again:.1f} ms over ceiling {ceiling:.1f} ms "
                  f"({p95_again / ceiling:.1f}x) on both passes")
            print("  if this is the archive growing rather than the code slowing, "
                  "re-seed: python search_lab/latency_smoke.py --seed")
            verdict["outcome"] = "fail"

    if args.json:
        Path(args.json).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return 1 if verdict["outcome"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
