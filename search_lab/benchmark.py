#!/usr/bin/env python3
"""Run the bench as a set — every instrument, one command, one recorded run.

Every row here is a published-baseline yardstick: a public IR or conversational
benchmark whose relevance labels were made by someone else, scored beside the
number that dataset's own leaderboard reports. That is the whole set, and the
limit is worth stating plainly — these say whether the retrieval components are
competitive in general. None of them says whether search got better *on this
archive*, because a label made here would have to be made by searching here.

Running them by hand means remembering a dozen invocations and their flags, so in
practice they get run once at the end, if at all.

    python -m search_lab benchmark                    # the set
    python -m search_lab benchmark --list             # what it would run

**Built for the tuning loop.** Every row records what it measured against a
content hash of the ranking code (``search_lab.bench_runs``), and a row whose
code and corpus are unchanged since its last successful run is **fresh**: skipped
in milliseconds, its recorded numbers reported as though it had just run. So the
first pass costs the full set and every pass after it costs only what an edit
actually invalidated — edit a ``SearchParams`` default and everything re-runs;
edit the viewer and nothing does. Uncommitted edits count, which is the point: a
tuning loop does not commit between passes.

Each row prints its headline metrics beside the **delta against the last run at a
different configuration** — not against the previous run, which during a tuning
loop is usually the same configuration measured twice. That is the number a knob
turn is judged on, and it is why this exists as a set rather than a list of
commands: the deltas have to be read together, or a change that lifts one corpus
while sinking another reads as a win.

Rows run as **separate processes**, sequentially. The retrieval stack caches a
corpus graph and a vector pack per engine, so swapping corpora inside one process
is how one corpus gets scored against another's cached structures; a process
boundary makes that unrepresentable. Sequential rather than parallel because
every row is measuring a shared machine — two rows at once measure each other's
contention.

This runner builds nothing itself. The harnesses build their own corpus on first
run — an ingest and, with ``--vectors``, an embed pass, which is where a row's
cost estimate stops being a guide.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_runs  # noqa: E402
import eval_home  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Row:
    """One benchmark invocation, and how to read what it produced."""

    name: str
    argv: list[str]
    cost_min: int
    #: The built home whose ``snapshot.json`` identifies this row's corpus, when
    #: the corpus is one home. None for the per-question haystacks, which are
    #: hundreds of small homes — see ``bench_runs.is_fresh``.
    home: Path | None = None
    build_hint: str = ""
    measure_keys: tuple[str, ...] = field(default=("ndcg10",))
    #: Which dataset this row scores, when the name does not say. A row is
    #: conventionally ``<dataset>[arms]`` or ``<family>:<dataset>[arms]``, and
    #: :meth:`dataset_name` reads that — but a family whose rows vary along
    #: something *other* than the dataset (``mtrag`` by query form, ``beam`` by
    #: conversation length) would otherwise read as one dataset per variant, and
    #: the inventory page would list query forms where corpora belong.
    dataset: str = ""

    #: Queries (or, for the per-question haystacks, corpora) the quick tier scores
    #: instead of all of them. None means the row is already cheap enough to run
    #: whole in both tiers — which is the better answer when it is true, because
    #: then quick and full share one ledger series and one history.
    #:
    #: Sampled rows share one **n**, not one fraction. What a sampled row can
    #: resolve is set by how many queries it scored, not by what share of the set
    #: that was, so 300 of 1,583 and 300 of 8,588 carry the same error bar and
    #: belong at the same number.
    quick_sample: int | None = None

    def dataset_name(self) -> str:
        """The corpus this row runs on, declared or read off the name."""
        if self.dataset:
            return self.dataset
        head = self.name.split("[", 1)[0]
        return head.split(":", 1)[1] if ":" in head else head

    def quick(self) -> "Row":
        """This row as the quick tier runs it.

        A sampled row is **a different measurement**, not a cheaper look at the
        same one — its metrics are computed over a subset and cannot be compared
        to a full run's — so it gets its own name and therefore its own ledger
        series and its own deltas. A row with no ``quick_sample`` is returned
        unchanged, so the cheap rows keep one continuous history across both
        tiers instead of being split for no gain.

        ``cost_min`` is deliberately *not* discounted. Sampling cuts query time
        and nothing else, so on a corpus that has never been built the quick tier
        pays the same ingest-and-embed the full tier does — a scaled-down estimate
        would promise minutes and deliver an overnight run. Once the row has run
        once its measured elapsed replaces the guess anyway, and that number is
        the one that shows what sampling actually saved."""
        if not self.quick_sample:
            return self
        return replace(self,
                       name=f"{self.name}~{self.quick_sample}",
                       argv=[*self.argv, "--sample", str(self.quick_sample)])

    def corpus_id(self) -> str | None:
        """This row's corpus fingerprint, read from the built home's snapshot
        manifest. None when the row has no single home, or the home is not built —
        the run itself is what will say so, loudly."""
        if self.home is None:
            return None
        try:
            return json.loads((self.home / "snapshot.json").read_text()).get("snapshot_id")
        except (OSError, json.JSONDecodeError):
            return None

    def code_id(self) -> str:
        """The code this row's numbers are a measurement of: the shared ranking and
        scoring source, plus the harness that produced them. Per-row so that
        editing one harness re-runs its own rows and leaves the rest fresh."""
        script = Path(self.argv[0])
        try:
            own = (str(script.relative_to(REPO)),)
        except ValueError:
            own = ()
        return bench_runs.code_id(own)


def manifest() -> list[Row]:
    """Every row the bench knows, in run order.

    Seven datasets, grouped by what each one is here to measure — the grouping is
    the point, because an undifferentiated row adds a number without adding a
    question anyone asked.

    **Document length.** The bm25/density term's effective strength scales
    inversely with document length (density normalizes to a fixed window but is
    never bounded), so a weight calibrated on one length regime can read flat on
    another. ``nfcorpus``'s short medical documents sit deliberately below
    ``scifact``'s abstracts.

    **Completeness.** Every other row is effectively single-gold and therefore
    scores findability alone. ``beam``'s median question needs 2–3 messages and
    its worst needs 16, so ``recall_all@k`` there is the one number on the bench
    that asks whether a window holds *everything* bearing on a question.

    **Retrieval granularity.** ``locomo`` retrieves a turn, ``longmemeval`` a
    session, and ``perltqa`` a curated memory *unit* — three granularities of the
    same underlying task, which is the axis a memory store is organised around.

    **Conversational queries.** ``cdr``.

    Absences, all of them deliberate:

    - **``trec-covid`` and ``mtrag`` are held off on cost.** Between them they are
      537K documents and about 22 hours of embedding, against roughly 4 for
      everything above. Both harnesses stay runnable by hand — ``beir_eval.py
      --dataset trec-covid`` and ``mtrag_eval.py`` — and both are worth returning
      to: trec-covid is the only set with deep enough per-query judgments to make
      a recall@100 mean anything, and MTRAG is the only external read on **query
      shape**, shipping one information need as a terse last turn and as a
      standalone rewrite with a published baseline for each. A single MTRAG domain
      (``--domain govt``, 49.6K passages) buys that comparison for about a seventh
      of the embed, at the cost of comparability with the published 4-domain
      macro-average.
    - **LongMemEval's ``--vectors`` pass** would embed 470 per-question corpora for
      a number whose published reference is measured on a different split, so the
      cost buys no comparison.
    - **LongMemEval-V2**, despite being the closest published corpus to this one:
      its questions carry an answer string and an evaluator, with no annotation of
      which trajectory holds the answer, so scoring it as retrieval would mean
      inventing the labels."""
    homes = eval_home.CACHE_ROOT / "homes"
    beir = str(REPO / "search_lab" / "beir_eval.py")
    cdr = str(REPO / "search_lab" / "cdr_eval.py")
    hay = str(REPO / "search_lab" / "haystack_eval.py")
    perltqa = str(REPO / "search_lab" / "perltqa_eval.py")
    first_run = "the harness builds it on first run (ingest + embed, tens of minutes)"
    ir = ("ndcg10", "mrr10", "recall10")
    hay_keys = ("recall10", "recall_all10", "ndcg10")

    # First-run estimates are priced off this box's measured throughput —
    # ingest ~3,600 docs/min, embed ~452 docs/min — because a cold pass over this
    # set is a decision about a whole day and the plan is what that decision gets
    # made on. Every one of them is replaced by the row's own elapsed time as
    # soon as it has run once (see `estimate`), so they only ever have to be
    # right to the nearest hour.
    # Ordered cheapest-first: a cold pass is dominated by whichever corpora have
    # to be built, and a set that runs those first is a set nobody watches to the
    # end — every quick row would sit behind hours of embed before printing
    # anything. Within a dataset the lexical row precedes the vectors one, which
    # is the same principle: the lexical row pays the ingest and the vectors row
    # adds only the embed pass on top of the corpus already built.
    return [
        Row(name="beir:scifact[lexical]", cost_min=2,
            argv=[beir, "--dataset", "scifact"],
            home=homes / "scifact", build_hint=first_run, measure_keys=ir),
        Row(name="beir:scifact[vectors]", cost_min=5,
            argv=[beir, "--dataset", "scifact", "--vectors"],
            home=homes / "scifact", build_hint=first_run, measure_keys=ir),
        Row(name="beir:nfcorpus[lexical]", cost_min=2,
            argv=[beir, "--dataset", "nfcorpus"],
            home=homes / "nfcorpus", build_hint=first_run, measure_keys=ir),
        Row(name="beir:nfcorpus[vectors]", cost_min=3,
            argv=[beir, "--dataset", "nfcorpus", "--vectors"],
            home=homes / "nfcorpus", build_hint=first_run, measure_keys=ir),
        Row(name="locomo[lexical]", cost_min=5,
            argv=[hay, "--dataset", "locomo"],
            build_hint=first_run, measure_keys=hay_keys),
        Row(name="locomo[vectors]", cost_min=10,
            argv=[hay, "--dataset", "locomo", "--vectors"],
            build_hint=first_run, measure_keys=hay_keys),
        Row(name="longmemeval[lexical]", cost_min=15,
            argv=[hay, "--dataset", "longmemeval"],
            build_hint=first_run, measure_keys=hay_keys),
        Row(name="beam:100K[lexical]", cost_min=4, dataset="beam",
            argv=[hay, "--dataset", "beam", "--beam-tier", "100K"],
            build_hint=first_run, measure_keys=hay_keys, quick_sample=8),
        Row(name="beam:100K[vectors]", cost_min=16, dataset="beam",
            argv=[hay, "--dataset", "beam", "--beam-tier", "100K", "--vectors"],
            build_hint=first_run, measure_keys=hay_keys, quick_sample=8),
        Row(name="perltqa[lexical]~2000", cost_min=4,
            argv=[perltqa, "--sample", "2000"], home=homes / "perltqa",
            build_hint=first_run, measure_keys=ir),
        Row(name="perltqa[vectors]~2000", cost_min=5,
            argv=[perltqa, "--vectors", "--sample", "2000"],
            home=homes / "perltqa", build_hint=first_run, measure_keys=ir),
        Row(name="cdr[vectors]", cost_min=20,
            argv=[cdr, "--vectors"],
            home=homes / "cdr", build_hint=first_run, measure_keys=ir,
            quick_sample=300),
    ]


def select(rows: list[Row], *, only: list[str]) -> list[Row]:
    """The rows to run, narrowed by ``--only`` substrings."""
    if not only:
        return rows
    return [r for r in rows if any(frag in r.name for frag in only)]


# ── reading what a row produced ──────────────────────────────────────────────

def measures_from_report(payload: dict) -> dict:
    """Headline numbers out of an external harness's ``--json-out`` report.

    The two report shapes differ (a shared-corpus run reports flat metrics, a
    haystack run nests them under ``overall`` with cutoffs as keys), and JSON has
    turned the integer cutoffs into strings on the way out — normalized here so a
    ledger row's ``measures`` means one thing across every benchmark."""
    if "overall" in payload:
        overall = payload["overall"]
        out = {"n": overall.get("n")}
        for metric, key in (("recall", "recall"), ("ndcg", "ndcg"),
                            ("recall_all", "recall_all")):
            at = overall.get(metric) or {}
            for k in ("5", "10"):
                if k in at or int(k) in at:
                    out[f"{key}{k}"] = at.get(k, at.get(int(k)))
        return out
    recall = payload.get("recall") or {}
    return {
        "n": payload.get("n"),
        "ndcg10": payload.get("ndcg10"),
        "mrr10": payload.get("mrr10"),
        "recall10": recall.get("10", recall.get(10)),
        "recall100": recall.get("100", recall.get(100)),
        "query_p50_ms": payload.get("query_p50_ms"),
    }


def per_query_from_report(payload: dict) -> list:
    """Every scored query's own result, out of the same report.

    Two shapes again: the external harnesses carry ``per_query`` in the shape
    :func:`search_lab.eval_core.query_row` defines, and the in-house evaluator
    carries ``per_case``, which predates it and holds the query, its reciprocal
    rank and its latency but no gold accounting. Lifted rather than dropped —
    a failed case with its query text is the most useful thing either harness
    produces, and the older shape has most of it."""
    rows = payload.get("per_query")
    if isinstance(rows, list) and rows:
        return rows
    cases = payload.get("per_case")
    if not isinstance(cases, list):
        return []
    out = []
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        rr = case.get("rr")
        out.append({
            "qid": str(i),
            "query": case.get("query", ""),
            "latency_ms": case.get("latency_ms"),
            # The evaluator records a reciprocal rank, which is 1/rank — so the
            # rank it came from is recoverable exactly, and 0 means not found.
            "rank": round(1 / rr) if isinstance(rr, (int, float)) and rr else None,
            "n_gold": None,
            "found": None,
            "measures": {"rr": rr},
            **({"group": case["difficulty"]} if case.get("difficulty") else {}),
        })
    return out


def performance_from_report(payload: dict) -> dict:
    """What the run cost, out of the same report the measures come from.

    Kept beside ``measures`` rather than folded into it, because the two answer
    different questions and a reader mixing them gets a wrong one: ``measures``
    is what the row is *scored* on and moves against a published baseline, while
    this is what the box did to produce them and moves with the machine. A p99
    listed among the nDCGs reads as a result; listed here it reads as a cost.

    Two report shapes. The external harnesses carry a ``performance`` block built
    by :func:`search_lab.eval_core.performance`; the in-house evaluator predates
    it and carries ``latency`` in the same shape minus the throughput fields, so
    it is lifted rather than left behind — the rows it produced are the oldest
    history in the ledger and dropping their profile would put a hole in exactly
    the comparison the ledger exists for."""
    block = payload.get("performance")
    if isinstance(block, dict):
        return block
    latency = payload.get("latency")
    if not isinstance(latency, dict):
        return {}
    return {
        "queries": payload.get("n"),
        "total": latency.get("total") or {},
        "stages": latency.get("stages") or {},
        "staged": latency.get("n"),
        "cold": latency.get("cold"),
        "pool_p50": latency.get("pool_p50"),
    }


# ── running ──────────────────────────────────────────────────────────────────

def child_env() -> dict[str, str]:
    """The environment a row runs in.

    ``THREAD_ARCHIVE_HOME`` is dropped: every row names its own corpus by building
    it, and an inherited value would either be ignored — making it a lie in the
    recorded row — or, worse, quietly redirect one. The arm switches are dropped
    for the same reason: each
    row's arms are part of what it is measuring, and a stray ``EMBED=off`` in the
    shell would silently turn a ``[vectors]`` row into a lexical one under a name
    that says otherwise."""
    env = dict(os.environ)
    for var in ("THREAD_ARCHIVE_HOME", "THREAD_ARCHIVE_EMBED",
                "THREAD_ARCHIVE_RERANK", "THREAD_ARCHIVE_COHERENCE"):
        env.pop(var, None)
    return env


def run_row(row: Row, *, quiet: bool) -> dict:
    """Run one row to completion and record it. Returns the ledger record.

    The child's output is streamed through rather than captured: rows run for
    minutes each, and a set you cannot watch is a set you cannot interrupt when the
    first row already says what you needed to know."""
    t0 = time.monotonic()
    fd, name = tempfile.mkstemp(prefix="bench-", suffix=".json")
    os.close(fd)
    report_path = Path(name)
    argv = [sys.executable, *row.argv, "--json-out", str(report_path)]

    print(f"\n=== {row.name} (~{row.cost_min} min) ===", flush=True)
    proc = subprocess.Popen(argv, cwd=REPO, env=child_env(), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    for line in proc.stdout:
        if not quiet:
            print(f"  {line.rstrip()}", flush=True)
    code = proc.wait()
    elapsed = time.monotonic() - t0

    measures: dict = {}
    performance: dict = {}
    per_query: list = []
    if code == 0 and report_path.exists():
        try:
            payload = json.loads(report_path.read_text())
            measures = measures_from_report(payload)
            performance = performance_from_report(payload)
            per_query = per_query_from_report(payload)
        except (OSError, json.JSONDecodeError):
            measures = {}
    report_path.unlink(missing_ok=True)

    record = bench_runs.record_run(
        row=row.name, argv=row.argv, corpus_id=row.corpus_id(), measures=measures,
        elapsed_s=elapsed, status="ok" if code == 0 else "failed",
        code=row.code_id(), performance=performance)
    # The detail lands beside the ledger under the run's own id, so the run has
    # to be recorded before it can be filed. A failure to store it is not a
    # failure of the run — see ``bench_runs.write_queries``.
    if per_query:
        bench_runs.write_queries(bench_runs.run_id(record), per_query)
    if code != 0:
        print(f"  FAILED (exit {code})"
              + (f" — corpus may not be built: {row.build_hint}" if row.build_hint else ""),
              flush=True)
    return record


# ── reporting ────────────────────────────────────────────────────────────────

def previous_configuration(row: str, *, current_code: str,
                           home: Path | None = None) -> dict | None:
    """The most recent successful run of ``row`` under *different* code.

    Not simply the previous run: during a tuning loop the same configuration gets
    measured repeatedly (a re-run after an unrelated edit, a forced pass), and a
    delta against an identical configuration is zero — which reads as "the change
    did nothing" rather than "this is the same measurement twice"."""
    for record in bench_runs.read_runs(home, row=row):
        if record.get("status") == "ok" and record.get("code_id") != current_code:
            return record
    return None


def report_lines(results: list[tuple[Row, dict, str]]) -> list[str]:
    """The summary table: one line per row, its headline metrics, and the delta
    against the last differently-configured run."""
    lines = [f"{'row':<30}{'state':<8}{'metrics':<62}vs last config",
             "-" * 118]
    for row, record, state in results:
        measures = record.get("measures") or {}
        if not measures:
            lines.append(f"{row.name:<30}{state:<8}{'(no numbers recorded)':<62}")
            continue
        shown = [(k, measures[k]) for k in row.measure_keys
                 if isinstance(measures.get(k), (int, float))]
        metrics = "  ".join(f"{k} {v:.3f}" for k, v in shown)[:60]
        prior = previous_configuration(row.name, current_code=row.code_id())
        deltas = ""
        if prior:
            before = prior.get("measures") or {}
            parts = [f"{k} {measures[k] - before[k]:+.3f}" for k, _ in shown
                     if isinstance(before.get(k), (int, float))]
            deltas = "  ".join(parts)
        lines.append(f"{row.name:<30}{state:<8}{metrics:<62}{deltas}")
    return lines


def estimate(row: Row, prior: dict | None) -> float:
    """Minutes this row is likely to take: what it actually took last time, else
    the manifest's guess.

    A measured time is the better estimate for the obvious reason, and for a
    less obvious one — a row's cost is dominated by whether its corpus is already
    built, which the manifest cannot know and the last run's elapsed time does."""
    recorded = (prior or {}).get("elapsed_s")
    if isinstance(recorded, (int, float)) and recorded > 0:
        return recorded / 60
    return float(row.cost_min)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", action="append", default=[], metavar="FRAGMENT",
                    help="run just the rows whose name contains this (repeatable)")
    ap.add_argument("--quick", action="store_true",
                    help="the quick tier: score a deterministic query sample on "
                         "the rows heavy enough to need one, every query on the "
                         "rest. Minutes rather than hours, over the same ten "
                         "datasets. Sampled rows are recorded under their own "
                         "names (`row~N`), never mixed with full-run history")
    ap.add_argument("--force", action="store_true",
                    help="re-run rows the ledger says are already fresh")
    ap.add_argument("--list", action="store_true",
                    help="print the plan — what would run, what is fresh — and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress each row's own output; print only the summary")
    ap.add_argument("--json-out", type=Path, default=None, metavar="FILE",
                    help="also write the summary as JSON")
    args = ap.parse_args(argv)

    # Tier first, then --only: the quick tier renames the rows it samples, and a
    # fragment the caller typed should match what will actually run.
    rows = manifest()
    if args.quick:
        rows = [row.quick() for row in rows]
    rows = select(rows, only=args.only)
    if not rows:
        print("no rows selected")
        return 1

    plan: list[tuple[Row, dict | None, bool]] = []
    for row in rows:
        prior = bench_runs.latest_ok(row=row.name)
        fresh = (not args.force) and bench_runs.is_fresh(
            prior, corpus_id=row.corpus_id(), code=row.code_id())
        plan.append((row, prior, fresh))

    budget = sum(estimate(row, prior) for row, prior, fresh in plan if not fresh)
    to_run = sum(1 for _, _, fresh in plan if not fresh)
    sampled = sum(1 for row in rows if "~" in row.name)
    print(f"bench{' [quick]' if args.quick else ''}: {len(rows)} row(s), "
          f"{to_run} to run (~{budget:.0f} min), "
          f"{len(rows) - to_run} fresh   [code {bench_runs.code_id()}]")
    if args.quick:
        print(f"  {sampled} row(s) sampled, {len(rows) - sampled} scored whole. "
              f"A sampled row resolves deltas no finer than 1/n — read it as a "
              f"smoke test, and re-run without --quick before claiming a change.")
        if any(not fresh and not prior for _, prior, fresh in plan):
            print("  Rows that have never run still pay the full ingest+embed: "
                  "sampling cuts query time, not corpus building.")
    for row, prior, fresh in plan:
        when = (prior or {}).get("at", "")[:16]
        est = estimate(row, prior)
        print(f"  {row.name:<30}{'fresh' if fresh else 'run':<7}"
              f"{('  <1' if est < 1 else f'~{est:>3.0f}')} min   "
              f"{('last ' + when) if prior else 'never run'}")
    if args.list:
        return 0

    results: list[tuple[Row, dict, str]] = []
    failures = 0
    for row, prior, fresh in plan:
        if fresh:
            results.append((row, prior or {}, "fresh"))
            continue
        record = run_row(row, quiet=args.quiet)
        failures += record.get("status") != "ok"
        results.append((row, record, "ran"))

    print()
    for line in report_lines(results):
        print(line)
    if failures:
        print(f"\n{failures} row(s) failed")

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "code_id": bench_runs.code_id(),
            "rows": [{"row": r.name, "state": state, **rec}
                     for r, rec, state in results],
        }, indent=1) + "\n")
        print(f"wrote {args.json_out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
