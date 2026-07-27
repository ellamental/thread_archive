#!/usr/bin/env python3
"""Run the bench as a set — every instrument, one command, one recorded run.

The lab's instruments each answer a different question, and a ranking change
needs several of them: the gold gates say whether the change helped on
corpus-grounded labels, the hold-out corpus says whether that survives on a
corpus nobody tuned against, and the external benchmarks say whether the
components are still competitive in general. Running them by hand means
remembering six invocations, their flags, and which of them a given change can
even move — so in practice they get run once at the end, if at all.

    python -m search_lab benchmark                    # the standard set
    python -m search_lab benchmark --tier smoke       # the gold gates alone
    python -m search_lab benchmark --list             # what a tier would run

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
turn is judged on, and it is why this exists as a set rather than six commands:
the deltas have to be read together, or a change that lifts one corpus while
sinking another reads as a win.

Rows run as **separate processes**, sequentially. The retrieval stack caches a
corpus graph and a vector pack per engine, so swapping corpora inside one process
is how one corpus gets scored against another's cached structures; a process
boundary makes that unrepresentable. Sequential rather than parallel because
every row is measuring a shared machine — two rows at once measure each other's
contention.

This runner builds nothing itself. The external harnesses build their own corpus
on first run — an ingest and, with ``--vectors``, an embed pass, which is where a
row's cost estimate stops being a guide — and the gold rows simply fail when their
snapshot is absent, naming the builder. That asymmetry is deliberate: a mined gold
corpus is an operator artifact with hours of agent time in it, and a benchmark run
must never quietly decide to remake one.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_runs  # noqa: E402
import eval_home  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

#: Tiers are nested: smoke ⊂ standard ⊂ full. The split is by what a run costs
#: against what it can tell you — smoke is the pair of instruments that can credit
#: a ranking change at all (~7 min warm), standard adds every external yardstick
#: whose corpus is already built and embedded (~20 min warm), and full adds the
#: cross-encoder passes, which are the bench's dominant cost (~an hour on LoCoMo
#: alone) and are measuring an arm production ships with off. Warm is the ordinary
#: case; a row whose corpus has never been built pays for building it once.
TIERS = ("smoke", "standard", "full")


@dataclass
class Row:
    """One benchmark invocation, and how to read what it produced."""

    name: str
    tier: str
    argv: list[str]
    cost_min: int
    #: The built home whose ``snapshot.json`` identifies this row's corpus, when
    #: the corpus is one home. None for the per-question haystacks, which are
    #: hundreds of small homes — see ``bench_runs.is_fresh``.
    home: Path | None = None
    #: Set for the gold-gate rows: their numbers come from the gate's own per-file
    #: ledger in this directory rather than from a JSON report.
    gold_dir: Path | None = None
    needs_json_out: bool = True
    build_hint: str = ""
    measure_keys: tuple[str, ...] = field(default=("ndcg10",))

    def corpus_id(self) -> str | None:
        """This row's corpus fingerprint, read from the built home's snapshot
        manifest. None when the row has no single home, or the home is not built —
        the run itself is what will say so, loudly."""
        target = self.snap if self.gold_dir is not None else self.home
        if target is None:
            return None
        try:
            return json.loads((target / "snapshot.json").read_text()).get("snapshot_id")
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

    @property
    def snap(self) -> Path | None:
        """The snapshot a gold row scores over, taken from its own argv so the two
        can never drift apart."""
        if "--snap" not in self.argv:
            return None
        return Path(self.argv[self.argv.index("--snap") + 1]).expanduser()


def _gold_rows() -> list[Row]:
    """The two grounded-gold corpora: the operator's archive, and the SWE-chat
    hold-out. These are the only rows that can credit an improvement, so they are
    the whole of the smoke tier."""
    archive_snap = Path(os.environ.get("THREAD_ARCHIVE_SNAP",
                                       str(Path.home() / ".thread" / "archive-snap")))
    archive_gold = Path(os.environ.get("THREAD_ARCHIVE_GOLD_DIR",
                                       str(Path.home() / ".thread" / "archive")))
    swe_home = eval_home.CACHE_ROOT / "homes" / "swe-chat"
    swe_gold = Path.home() / "dev" / "swe-chat-data" / "gold"
    gate = str(REPO / "scripts" / "retrieval_gold_gate.py")
    return [
        Row(name="gold-gate:archive", tier="smoke", cost_min=7,
            argv=[gate, "--snap", str(archive_snap), "--gold-dir", str(archive_gold)],
            gold_dir=archive_gold, needs_json_out=False,
            build_hint="freeze a snapshot with `python search_lab/snapshot.py <dir>`",
            measure_keys=("mrr", "success10", "recall10", "ndcg10")),
        Row(name="gold-gate:swe-chat", tier="smoke", cost_min=6,
            argv=[gate, "--snap", str(swe_home), "--gold-dir", str(swe_gold)],
            gold_dir=swe_gold, needs_json_out=False,
            build_hint="build it with `python search_lab/swechat_corpus.py`",
            measure_keys=("mrr", "success10", "recall10", "ndcg10")),
    ]


def _external_rows() -> list[Row]:
    """The published-baseline yardsticks. Standard covers every corpus that is
    already built and embedded; the cross-encoder passes are full-tier because
    they cost hours and measure an arm the shipped stack keeps off."""
    homes = eval_home.CACHE_ROOT / "homes"
    beir = str(REPO / "search_lab" / "beir_eval.py")
    cdr = str(REPO / "search_lab" / "cdr_eval.py")
    hay = str(REPO / "search_lab" / "haystack_eval.py")
    first_run = "the harness builds it on first run (ingest + embed, tens of minutes)"
    return [
        Row(name="beir:scifact[lexical]", tier="standard", cost_min=2,
            argv=[beir, "--dataset", "scifact"],
            home=homes / "scifact", build_hint=first_run,
            measure_keys=("ndcg10", "mrr10", "recall10")),
        Row(name="beir:scifact[vectors]", tier="standard", cost_min=5,
            argv=[beir, "--dataset", "scifact", "--vectors"],
            home=homes / "scifact", build_hint=first_run,
            measure_keys=("ndcg10", "mrr10", "recall10")),
        Row(name="cdr[vectors]", tier="standard", cost_min=20,
            argv=[cdr, "--vectors"],
            home=homes / "cdr", build_hint=first_run,
            measure_keys=("ndcg10", "mrr10", "recall10")),
        Row(name="locomo[lexical]", tier="standard", cost_min=5,
            argv=[hay, "--dataset", "locomo"],
            build_hint=first_run, measure_keys=("recall10", "ndcg10")),
        Row(name="locomo[vectors]", tier="standard", cost_min=10,
            argv=[hay, "--dataset", "locomo", "--vectors"],
            build_hint=first_run, measure_keys=("recall10", "ndcg10")),
        Row(name="longmemeval[lexical]", tier="standard", cost_min=15,
            argv=[hay, "--dataset", "longmemeval"],
            build_hint=first_run, measure_keys=("recall10", "ndcg10")),
        Row(name="beir:scifact[vectors+rerank]", tier="full", cost_min=15,
            argv=[beir, "--dataset", "scifact", "--vectors", "--rerank", "on"],
            home=homes / "scifact", build_hint=first_run,
            measure_keys=("ndcg10", "mrr10", "recall10")),
        Row(name="cdr[vectors+rerank]", tier="full", cost_min=60,
            argv=[cdr, "--vectors", "--rerank", "on"],
            home=homes / "cdr", build_hint=first_run,
            measure_keys=("ndcg10", "mrr10", "recall10")),
        Row(name="locomo[vectors+rerank]", tier="full", cost_min=60,
            argv=[hay, "--dataset", "locomo", "--vectors", "--rerank", "on"],
            build_hint=first_run, measure_keys=("recall10", "ndcg10")),
    ]


def manifest() -> list[Row]:
    """Every row the bench knows, in run order.

    LongMemEval's ``--vectors`` pass is deliberately absent: it would embed 470
    per-question corpora for a number whose published reference is measured on a
    different split of the dataset, so the cost buys no comparison. Run it by hand
    if that changes."""
    return _gold_rows() + _external_rows()


def select(rows: list[Row], *, tier: str, only: list[str]) -> list[Row]:
    """The rows a tier runs, narrowed by ``--only`` substrings. Tiers are nested,
    so ``standard`` includes ``smoke``."""
    depth = TIERS.index(tier)
    chosen = [r for r in rows if TIERS.index(r.tier) <= depth]
    if only:
        chosen = [r for r in chosen if any(frag in r.name for frag in only)]
    return chosen


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


def measures_from_gold_ledger(gold_dir: Path, *, since: str) -> dict:
    """Pooled headline numbers from the gate's own per-file ledger.

    The gate already records every file's metrics where the corpus lives; reading
    them back beats parsing its printout, and it means the run-level row and the
    per-file detail can never disagree. Pooling is case-weighted — files differ in
    size by an order of magnitude, and a plain mean over files would let a 7-case
    topic file outvote a 75-case protocol one."""
    import gold_runs

    for record in gold_runs.read_runs(gold_dir, limit=5):
        if record.get("at", "") < since:
            continue
        files = record.get("files") or {}
        total = sum(m.get("n", 0) for m in files.values())
        if not total:
            continue
        pooled = {
            metric: round(sum(m.get(metric, 0.0) * m.get("n", 0)
                              for m in files.values()) / total, 4)
            for metric in ("mrr", "success10", "recall10", "ndcg10")
        }
        return {**pooled, "n": total, "files": len(files),
                "passed": record.get("passed")}
    return {}


# ── running ──────────────────────────────────────────────────────────────────

def child_env() -> dict[str, str]:
    """The environment a row runs in.

    ``THREAD_ARCHIVE_HOME`` is dropped: every row names its own corpus (the gold
    rows by flag, the harnesses by building their own), and an inherited value
    would either be ignored — making it a lie in the recorded row — or, worse,
    quietly redirect one. The arm switches are dropped for the same reason: each
    row's arms are part of what it is measuring, and a stray ``EMBED=off`` in the
    shell would silently turn a ``[vectors]`` row into a lexical one under a name
    that says otherwise."""
    env = dict(os.environ)
    for var in ("THREAD_ARCHIVE_HOME", "THREAD_ARCHIVE_EMBED",
                "THREAD_ARCHIVE_RERANK", "THREAD_ARCHIVE_COHERENCE"):
        env.pop(var, None)
    return env


def run_row(row: Row, *, tier: str, quiet: bool) -> dict:
    """Run one row to completion and record it. Returns the ledger record.

    The child's output is streamed through rather than captured: rows run for
    minutes each, and a set you cannot watch is a set you cannot interrupt when the
    first row already says what you needed to know."""
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    t0 = time.monotonic()
    report_path: Path | None = None
    argv = [sys.executable, *row.argv]
    if row.needs_json_out:
        fd, name = tempfile.mkstemp(prefix="bench-", suffix=".json")
        os.close(fd)
        report_path = Path(name)
        argv += ["--json-out", str(report_path)]

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
    if code == 0:
        if row.gold_dir is not None:
            measures = measures_from_gold_ledger(row.gold_dir, since=started)
        elif report_path is not None and report_path.exists():
            try:
                measures = measures_from_report(json.loads(report_path.read_text()))
            except (OSError, json.JSONDecodeError):
                measures = {}
    if report_path is not None:
        report_path.unlink(missing_ok=True)

    record = bench_runs.record_run(
        row=row.name, argv=row.argv, corpus_id=row.corpus_id(), measures=measures,
        elapsed_s=elapsed, status="ok" if code == 0 else "failed",
        code=row.code_id(), tier=tier)
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
    ap.add_argument("--tier", choices=TIERS, default="standard",
                    help="how much to run; tiers nest (default: standard)")
    ap.add_argument("--only", action="append", default=[], metavar="FRAGMENT",
                    help="run just the rows whose name contains this (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="re-run rows the ledger says are already fresh")
    ap.add_argument("--list", action="store_true",
                    help="print the plan — what would run, what is fresh — and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress each row's own output; print only the summary")
    ap.add_argument("--json-out", type=Path, default=None, metavar="FILE",
                    help="also write the summary as JSON")
    args = ap.parse_args(argv)

    rows = select(manifest(), tier=args.tier, only=args.only)
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
    print(f"bench: tier {args.tier}, {len(rows)} row(s), "
          f"{to_run} to run (~{budget:.0f} min), "
          f"{len(rows) - to_run} fresh   [code {bench_runs.code_id()}]")
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
        record = run_row(row, tier=args.tier, quiet=args.quiet)
        failures += record.get("status") != "ok"
        results.append((row, record, "ran"))

    print()
    for line in report_lines(results):
        print(line)
    if failures:
        print(f"\n{failures} row(s) failed")

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "tier": args.tier, "code_id": bench_runs.code_id(),
            "rows": [{"row": r.name, "state": state, **rec}
                     for r, rec, state in results],
        }, indent=1) + "\n")
        print(f"wrote {args.json_out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
