"""search_lab — score N retrieval configurations against the shipped one.

The experiment bench of the quality ladder (docs/search-quality.md): every
module in ``evals/experiments/`` is one configuration of the search
stack — a :class:`thread_archive._retrieval.SearchParams` value or a full
``SEARCH`` callable (contract in ``evals/experiments/README.md``). This runner builds
the checked-in synthetic corpus (``tests/quality_corpus.py``) in a throwaway
archive home, scores the baseline and every configuration on the identical
cases with the same MRR/recall loop as the live-archive harness, and prints a
leaderboard with deltas against the baseline.

Fast by default (lexical stack, seconds); ``--models`` embeds the corpus and
runs the fused pipeline (real torch models — minutes on a cold cache), which is
the mode where fusion/rerank experiments actually move.

    .venv/bin/python evals/search_lab.py
    .venv/bin/python evals/search_lab.py --models
    .venv/bin/python evals/search_lab.py --only no_recency,pool_order --json out.json

The corpus is synthetic and lexically easy: a delta here is a direction, not a
shipping verdict — promote winners by re-measuring on the live tiers
(``retrieval_eval.py --from-log``, the judge) before touching the defaults in
``_retrieval/params.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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


def run_lab(name_to_id: dict[str, str], experiments: list[Experiment], *,
            limit: int = 10, rerank=None) -> dict:
    """Score the baseline and every experiment on the corpus cases. Returns
    ``{"rows": [...]}`` — baseline first, then experiments in leaderboard
    (MRR-descending) order, each row carrying the metrics and its MRR delta
    against the baseline."""
    quality = _quality()

    def score(name: str, hypothesis: str, search=None) -> dict:
        rep = quality.run_cases(name_to_id, search=search, limit=limit, rerank=rerank)
        return {
            "name": name,
            "hypothesis": hypothesis,
            "mrr": rep["mrr"],
            "recall": {str(k): v for k, v in rep["recall"].items()},
            "per_shape": rep["per_shape"],
            "latency_p50_ms": rep["latency_p50_ms"],
        }

    baseline = score("baseline", "the shipped configuration (params.py defaults)")
    rows = [score(e.name, e.hypothesis, e.search) for e in experiments]
    rows.sort(key=lambda r: -r["mrr"])
    for row in [baseline, *rows]:
        row["delta_mrr"] = row["mrr"] - baseline["mrr"]
    return {"rows": [baseline, *rows]}


def _print_leaderboard(report: dict) -> None:
    rows = report["rows"]
    name_w = max(len(r["name"]) for r in rows) + 2
    print(f"{'config':<{name_w}} {'MRR':>6} {'ΔMRR':>7} {'R@1':>5} {'R@5':>5} {'R@10':>5} {'p50ms':>7}")
    for r in rows:
        delta = f"{r['delta_mrr']:+.3f}" if r["name"] != "baseline" else "—"
        rec = r["recall"]
        print(f"{r['name']:<{name_w}} {r['mrr']:>6.3f} {delta:>7} "
              f"{rec.get('1', 0.0):>5.2f} {rec.get('5', 0.0):>5.2f} {rec.get('10', 0.0):>5.2f} "
              f"{r['latency_p50_ms']:>7.1f}")
    print()
    for r in rows[1:]:
        print(f"  {r['name']}: {r['hypothesis']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", action="store_true",
                    help="run the fused pipeline: embed the corpus and enable the model arms (minutes)")
    ap.add_argument("--only", help="comma-separated experiment names (default: all in evals/experiments/)")
    ap.add_argument("--experiments", type=Path, default=EXPERIMENTS_DIR,
                    help="experiments directory (default: evals/experiments/)")
    ap.add_argument("--limit", type=int, default=10, help="result depth per query (default 10)")
    ap.add_argument("--json", type=Path, help="also write the full report as JSON")
    args = ap.parse_args(argv)

    # A throwaway home: the lab must never rank against (or write into) the real
    # archive. Model arms off unless --models — read per call by the product's
    # own switches; coherence stays off either way (its background graph refresh
    # is nondeterministic across runs, and determinism is the lab's whole point).
    import os

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
                return 2

        experiments = discover(args.experiments)
        if args.only:
            wanted = {n.strip() for n in args.only.split(",") if n.strip()}
            unknown = wanted - {e.name for e in experiments}
            if unknown:
                print(f"error: unknown experiment(s): {', '.join(sorted(unknown))}", file=sys.stderr)
                return 2
            experiments = [e for e in experiments if e.name in wanted]

        report = run_lab(ids, experiments, limit=args.limit)
        report["mode"] = "models" if args.models else "lexical"
        _print_leaderboard(report)
        if args.json:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"\nwrote {args.json}", file=sys.stderr)
        return 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
