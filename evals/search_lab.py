"""search_lab — race retrieval configurations against the shipped one on a leaderboard.

The experiment bench of the quality ladder (docs/search-quality.md): every
module in ``evals/experiments/`` is one configuration of the search stack — a
:class:`thread_archive._retrieval.SearchParams` value or a full ``SEARCH``
callable (contract in ``evals/experiments/README.md``). The runner scores the
baseline and every configuration on identical cases with the same
MRR/success/true-recall/nDCG loop as the live-archive harness, and prints a
leaderboard with deltas against the baseline.

Two benches, run **both by default** (``--gold`` / ``--synthetic`` narrow to one):

- **Gold.** Scores over the snapshot-bound gold case files (``evals/README.md``
  → "Taking a baseline") — graded, corpus-grounded pools scored over the frozen
  snapshot they were mined against, the fused production pipeline running
  natively over the snapshot's vector pack. This is the measurement of record: a
  gold-file delta scored on both sides of a change is the evidence that credits a
  promotion. Auto-discovers the gold files at the gold dir (or takes explicit
  ``--cases``), scores each over its own snapshot, one leaderboard per file.
  ``--sample FRAC`` scores a deterministic subset of each file (same slice every
  run, so the delta stays comparable) — a fast direction while iterating, not the
  promotion delta; drop it to confirm before changing defaults.

- **Synthetic.** Builds the checked-in synthetic corpus
  (``tests/quality_corpus.py``) in a throwaway archive home — no snapshot, no
  models, seconds (``--models`` embeds it and runs the fused pipeline). Lexically
  easy by design, so a delta here is a *direction*, not a verdict: the fast
  "did I obviously break something" check, not the promotion bar.

Running both is the point — the synthetic leaderboard lands in seconds while the
gold pass (minutes: real models over the whole snapshot) is still going, so a
gross regression shows up immediately and the grounded verdict follows.

    .venv/bin/python evals/search_lab.py                       # gold + synthetic (default)
    .venv/bin/python evals/search_lab.py --gold                # gold bench only
    .venv/bin/python evals/search_lab.py --synthetic --models  # synthetic only, fused pipeline
    .venv/bin/python evals/search_lab.py --cases ~/.thread/archive/judged-cases.jsonl
    .venv/bin/python evals/search_lab.py --only pool_order --sample 0.15   # fast iterate
    .venv/bin/python evals/search_lab.py --only no_recency,pool_order --json out.json

The gold bench needs a snapshot (``thread_archive snapshot <dir>``; default
``~/.thread/archive-snap``, ``$THREAD_ARCHIVE_SNAP`` to override) and gold files
mined against it (``thread_archive mine``). With neither on hand the default run
still prints the synthetic leaderboard and notes the gold skip; ``--synthetic``
asks for that explicitly. Both benches keep coherence off so the
baseline-vs-experiment delta stays deterministic; the coherence-on absolute
number is the CI gold gate's (``scripts/retrieval_gold_gate.py``). A gold file
whose snapshot fingerprint no longer matches is skipped (re-mine), never scored
against a moved corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = Path(__file__).resolve().parent / "experiments"


@dataclass
class Experiment:
    """One named configuration: a hypothesis and the search callable that tests it."""
    name: str
    hypothesis: str
    search: Callable


def _load_module(path: Path, name: str):
    """Import a module by file path (registered in sys.modules under ``name``)."""
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader, path
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _quality():
    """The corpus + scoring module (``tests/quality_corpus.py``), by path."""
    return _load_module(ROOT / "tests" / "quality_corpus.py", "quality_corpus")


def _gold_gate():
    """The gold-gate script (``scripts/retrieval_gold_gate.py``), by path — the
    one definition of what counts as a gold file and where the snapshot and gold
    dir default, shared so the bench and the CI gate agree on the fixture set."""
    return _load_module(ROOT / "scripts" / "retrieval_gold_gate.py", "retrieval_gold_gate")


def _subsample(cases: list[dict], frac: float) -> list[dict]:
    """A deterministic ``frac``-sized slice of ``cases`` (fraction in (0, 1]) for
    fast iteration — the whole list when ``frac >= 1``, at least one case
    otherwise. Selection is by a content hash of each case's query, so the subset
    is stable across runs (the baseline-vs-experiment delta stays comparable run
    to run) and isn't just the file's head; the hash order also nests the slices,
    so a larger ``frac`` is a superset of a smaller one — widen it without losing
    the cases you already read. A subset scores a *direction* on grounded data,
    not the promotion delta: with fewer, differently-mixed cases the absolute
    numbers drift from the full bench, so read the delta, then confirm on the full
    bench (drop ``--sample``) before changing defaults."""
    if frac >= 1.0:
        return cases
    ordered = sorted(cases, key=lambda c: hashlib.sha1(c["query"].encode()).hexdigest())
    return ordered[: max(1, math.ceil(len(cases) * frac))]


def _params_search(params) -> Callable:
    """The production pipeline pinned to one configuration."""
    from thread_archive._retrieval import search as production

    def search(query, **kw):
        return production(query, params=params, **kw)

    return search


def discover(directory: Path = EXPERIMENTS_DIR) -> list[Experiment]:
    """Load every experiment module in ``directory`` and validate the contract:
    ``HYPOTHESIS`` plus exactly one of ``PARAMS`` / ``SEARCH``."""
    from thread_archive._retrieval import SearchParams

    experiments: list[Experiment] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod = _load_module(path, f"search_lab_experiment_{path.stem}")
        hypothesis = getattr(mod, "HYPOTHESIS", None)
        params = getattr(mod, "PARAMS", None)
        fn = getattr(mod, "SEARCH", None)
        if not isinstance(hypothesis, str) or not hypothesis.strip():
            raise ValueError(f"{path.name}: HYPOTHESIS (a non-empty str) is required")
        if (params is None) == (fn is None):
            raise ValueError(f"{path.name}: define exactly one of PARAMS or SEARCH")
        if params is not None:
            if not isinstance(params, SearchParams):
                raise ValueError(f"{path.name}: PARAMS must be a SearchParams instance")
            fn = _params_search(params)
        elif not callable(fn):
            raise ValueError(f"{path.name}: SEARCH must be callable")
        experiments.append(Experiment(path.stem, hypothesis.strip(), fn))
    return experiments


def _score_rows(score_one, experiments: list[Experiment]) -> list[dict]:
    """Score the baseline and every experiment through ``score_one(search)`` —
    which returns one ``evaluate``-shaped report per call — and build the
    leaderboard rows: baseline first, experiments MRR-descending, each carrying
    its ΔMRR against the baseline. ``score_one(None)`` scores the shipped
    default; a corpus is whatever ``score_one`` closes over."""
    def row(name: str, hypothesis: str, search=None) -> dict:
        rep = score_one(search)
        return {
            "name": name,
            "hypothesis": hypothesis,
            "mrr": rep["mrr"],
            "success": {str(k): v for k, v in rep["success"].items()},
            "recall": {str(k): v for k, v in rep["recall"].items()},
            "ndcg": {str(k): v for k, v in rep["ndcg"].items()},
            "per_shape": rep["per_shape"],
            "latency_p50_ms": rep["latency_p50_ms"],
        }

    baseline = row("baseline", "the shipped configuration (params.py defaults)")
    rows = [row(e.name, e.hypothesis, e.search) for e in experiments]
    rows.sort(key=lambda r: -r["mrr"])
    for r in [baseline, *rows]:
        r["delta_mrr"] = r["mrr"] - baseline["mrr"]
    return [baseline, *rows]


def run_lab(name_to_id: dict[str, str], experiments: list[Experiment], *,
            limit: int = 10, rerank=None) -> dict:
    """Score the baseline and every experiment on the synthetic corpus. Returns
    ``{"rows": [...]}`` — baseline first, then experiments in leaderboard
    (MRR-descending) order, each row carrying the metrics and its ΔMRR against
    the baseline."""
    quality = _quality()

    def score_one(search):
        return quality.run_cases(name_to_id, search=search, limit=limit, rerank=rerank)

    return {"rows": _score_rows(score_one, experiments)}


def run_gold_lab(cases: list[dict], experiments: list[Experiment], *,
                 limit: int = 20, rerank=None) -> dict:
    """Score the baseline and every experiment over one snapshot-bound gold case
    file — thread-id golds and graded pools already resolved (``load_case_file``).
    Same ``{"rows": [...]}`` leaderboard as :func:`run_lab`, but the corpus is the
    frozen snapshot the cases were mined against: the caller points
    ``THREAD_ARCHIVE_HOME`` at it and the production pipeline runs natively over
    its vectors, so a gold-file delta here is promotion-grade evidence."""
    from thread_archive._eval import evaluate

    def score_one(search):
        return evaluate(cases, limit=limit, rerank=rerank, content_type=None,
                        exclude_content_types=None, search=search)

    return {"rows": _score_rows(score_one, experiments)}


def _print_leaderboard(report: dict) -> None:
    rows = report["rows"]
    name_w = max(len(r["name"]) for r in rows) + 2
    print(f"{'config':<{name_w}} {'MRR':>6} {'ΔMRR':>7} "
          f"{'S@10':>5} {'R@10':>5} {'nDCG10':>7} {'p50ms':>7}")
    for r in rows:
        delta = f"{r['delta_mrr']:+.3f}" if r["name"] != "baseline" else "—"
        suc, rec, ndcg = r["success"], r["recall"], r["ndcg"]
        print(f"{r['name']:<{name_w}} {r['mrr']:>6.3f} {delta:>7} "
              f"{suc.get('10', 0.0):>5.2f} {rec.get('10', 0.0):>5.2f} "
              f"{ndcg.get('10', 0.0):>7.2f} "
              f"{r['latency_p50_ms']:>7.1f}")
    print()
    for r in rows[1:]:
        print(f"  {r['name']}: {r['hypothesis']}")


def _select_experiments(args) -> list[Experiment]:
    """Discover the experiment modules and apply ``--only``. Exits 2 on an
    unknown name so a typo fails loudly rather than silently scoring fewer arms."""
    experiments = discover(args.experiments)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = wanted - {e.name for e in experiments}
        if unknown:
            print(f"error: unknown experiment(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            raise SystemExit(2)
        experiments = [e for e in experiments if e.name in wanted]
    return experiments


def _run_synthetic(args) -> tuple[int, dict | None]:
    """The synthetic-corpus bench: build the checked-in corpus in a throwaway
    home and race every experiment against the baseline on it. Returns
    ``(exit_code, report)`` — the report is None on failure, else the leaderboard
    dict the caller folds into the combined ``--json`` output."""
    import os

    # A throwaway home: the lab must never rank against (or write into) the real
    # archive. Model arms off unless --models — read per call by the product's
    # own switches; coherence stays off either way (its background graph refresh
    # is nondeterministic across runs, and determinism is the lab's whole point).
    home = tempfile.mkdtemp(prefix="search-lab-")
    from thread_archive import _config

    os.environ[_config.ENV_HOME] = home
    os.environ["THREAD_ARCHIVE_COHERENCE"] = "off"
    if args.models:
        os.environ.pop("THREAD_ARCHIVE_EMBED", None)
        os.environ.pop("THREAD_ARCHIVE_RERANK", None)
    else:
        os.environ["THREAD_ARCHIVE_EMBED"] = "off"
        os.environ["THREAD_ARCHIVE_RERANK"] = "off"

    try:
        quality = _quality()
        print(f"building corpus ({len(quality.THREADS)} threads, {len(quality.CASES)} cases) …",
              file=sys.stderr)
        ids = quality.build_corpus(Path(home))
        if args.models:
            from thread_archive import _api as api

            print("embedding corpus (loads the real models) …", file=sys.stderr)
            res = api.embed()
            if not res.get("embedded"):
                print(f"error: no vectors built ({res}); is the [embeddings] extra installed?",
                      file=sys.stderr)
                return 2, None

        experiments = _select_experiments(args)
        report = run_lab(ids, experiments, limit=args.limit if args.limit is not None else 10)
        report["mode"] = "models" if args.models else "lexical"
        print(f"\n=== synthetic corpus ({report['mode']}) ===")
        _print_leaderboard(report)
        return 0, report
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run_gold(args, *, explicit: bool) -> tuple[int, dict | None]:
    """The gold bench: race every experiment against the baseline over the
    snapshot-bound gold case files — the fused production pipeline running
    natively over the frozen snapshot's vectors, one leaderboard per file.
    Returns ``(exit_code, report)``. A missing snapshot / gold set is a hard
    error (2) when gold was asked for explicitly (``--gold`` / ``--cases``), but
    a soft skip (0, None) when gold is only running as half of the default pair —
    a box without a snapshot still gets its synthetic leaderboard."""
    import os

    from thread_archive import _api as api
    from thread_archive import _config
    from thread_archive._eval import load_case_file
    from thread_archive._ops.snapshot import read_snapshot_id

    miss = 2 if explicit else 0

    gate = _gold_gate()
    snap = Path(args.snapshot or os.environ.get("THREAD_ARCHIVE_SNAP")
                or gate.DEFAULT_SNAP).expanduser()
    if not (snap / "snapshot.json").is_file():
        print(f"gold: no snapshot at {snap} — freeze one with "
              f"`thread_archive snapshot <dir>` and mine gold cases against it "
              f"(--snapshot to point elsewhere, --synthetic to skip gold).", file=sys.stderr)
        return miss, None

    # Route every arm at the frozen snapshot. Force the model arms back on — a
    # synthetic pass earlier in the same process may have switched them off — so
    # gold always measures the fused production stack over the snapshot's vector
    # pack (the stack the synthetic corpus can't exercise). Coherence stays off,
    # exactly as the synthetic bench: its graph refresh is nondeterministic across
    # runs and the leaderboard's job is a clean baseline-vs-experiment delta (the
    # coherence-on absolute number is the CI gold gate's).
    os.environ.pop("THREAD_ARCHIVE_EMBED", None)
    os.environ.pop("THREAD_ARCHIVE_RERANK", None)
    os.environ[_config.ENV_HOME] = str(snap)
    os.environ["THREAD_ARCHIVE_COHERENCE"] = "off"
    api.open_archive(str(snap))

    from thread_archive._retrieval import embed

    if not embed.is_available():
        print("warning: the semantic arm is unavailable — gold is scoring the "
              "lexical stack only, not the fused pipeline (install the [embeddings] "
              "extra to measure the shipped stack).", file=sys.stderr)

    current = read_snapshot_id(str(snap))
    if args.cases:
        files = [p.expanduser() for p in args.cases]
    else:
        gold_dir = Path(args.gold_dir or os.environ.get("THREAD_ARCHIVE_GOLD_DIR")
                        or gate.DEFAULT_GOLD_DIR).expanduser()
        files = gate.discover_gold_files(gold_dir)
        if not files:
            print(f"gold: no gold files in {gold_dir} — mine some with "
                  f"`thread_archive mine` first (--synthetic to skip gold).", file=sys.stderr)
            return miss, None

    experiments = _select_experiments(args)
    limit = args.limit if args.limit is not None else 20

    if args.sample is not None:
        print(f"gold: SAMPLED run (~{args.sample:.0%} of each file) — a fast direction on "
              f"grounded data, NOT the promotion delta; drop --sample and confirm on the "
              f"full bench before changing defaults.", file=sys.stderr)

    file_reports: list[dict] = []
    for path in files:
        if not path.is_file():
            print(f"  {path.name}: not found — skipping", file=sys.stderr)
            continue
        cases = load_case_file(path)
        sids = {c.get("snapshot_id") for c in cases}
        if sids != {current}:
            print(f"  {path.name}: SKIP — snapshot {sorted(str(s) for s in sids)} "
                  f"!= {current} (stale or mid-re-mine; re-mine against this snapshot)",
                  file=sys.stderr)
            continue
        scored = _subsample(cases, args.sample) if args.sample is not None else cases
        report = run_gold_lab(scored, experiments, limit=limit)
        tag = f" · SAMPLED {len(scored)}/{len(cases)}" if args.sample is not None else ""
        print(f"\n=== gold: {path.name}  ({len(scored)} cases{tag} · snapshot {current}) ===")
        _print_leaderboard(report)
        report.update({"file": path.name, "path": str(path), "snapshot_id": current,
                       "n": len(scored), "n_full": len(cases),
                       "sampled": args.sample})
        file_reports.append(report)

    if not file_reports:
        print("gold: no gold file matched the current snapshot — nothing scored "
              "(re-mine against it).", file=sys.stderr)
        return miss, None

    return 0, {"mode": "gold", "snapshot_id": current, "snapshot": str(snap),
               "files": file_reports}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gold", action="store_true",
                    help="run only the gold bench (default runs gold + synthetic)")
    ap.add_argument("--synthetic", action="store_true",
                    help="run only the synthetic bench (default runs gold + synthetic)")
    ap.add_argument("--models", action="store_true",
                    help="synthetic bench: embed the corpus and enable the model arms (minutes)")
    ap.add_argument("--cases", type=Path, action="append", metavar="FILE",
                    help="gold bench over specific case file(s) (repeatable; "
                    "overrides auto-discovery)")
    ap.add_argument("--snapshot", type=Path, metavar="DIR",
                    help="gold bench: the corpus snapshot home "
                    "(default $THREAD_ARCHIVE_SNAP or ~/.thread/archive-snap)")
    ap.add_argument("--gold-dir", type=Path, metavar="DIR",
                    help="gold bench: where to auto-discover gold files "
                    "(default $THREAD_ARCHIVE_GOLD_DIR or ~/.thread/archive)")
    ap.add_argument("--sample", type=float, metavar="FRAC",
                    help="gold bench: score a deterministic FRAC subset of each file's "
                    "cases (e.g. 0.15) for fast iteration — a direction on grounded data, "
                    "not the promotion delta (drop it to confirm before promoting)")
    ap.add_argument("--only", help="comma-separated experiment names (default: all in evals/experiments/)")
    ap.add_argument("--experiments", type=Path, default=EXPERIMENTS_DIR,
                    help="experiments directory (default: evals/experiments/)")
    ap.add_argument("--limit", type=int, default=None,
                    help="result depth per query (default: 20 gold, 10 synthetic)")
    ap.add_argument("--json", type=Path, help="also write the full report as JSON")
    args = ap.parse_args(argv)

    # Neither flag → run both. Each flag narrows to just that bench.
    run_gold = args.gold or not args.synthetic
    run_synth = args.synthetic or not args.gold
    if args.models and not run_synth:
        ap.error("--models embeds the synthetic corpus, but --gold runs gold only")
    if args.sample is not None and not 0.0 < args.sample <= 1.0:
        ap.error("--sample takes a fraction in (0, 1] (e.g. 0.15)")
    if (args.cases or args.snapshot or args.gold_dir or args.sample is not None) and not run_gold:
        ap.error("--cases/--snapshot/--gold-dir/--sample are gold-bench options, but "
                 "--synthetic runs synthetic only")

    out: dict = {}
    codes: list[int] = []
    # Synthetic first: it is the only bench that writes (into a throwaway home it
    # then deletes), and gold's api.open_archive repoints the engine off it before
    # gold reads — so the synthetic corpus can never leak into the snapshot.
    if run_synth:
        rc, rep = _run_synthetic(args)
        codes.append(rc)
        if rep is not None:
            out["synthetic"] = rep
    if run_gold:
        rc, rep = _run_gold(args, explicit=bool(args.gold or args.cases))
        codes.append(rc)
        if rep is not None:
            out["gold"] = rep

    if args.json and out:
        args.json.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}", file=sys.stderr)
    return max(codes) if codes else 0


if __name__ == "__main__":
    sys.exit(main())
