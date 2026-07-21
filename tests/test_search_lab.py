"""The search lab stays runnable: params seam, experiment contract, leaderboard.

Guards the experiment bench (``scripts/search_lab.py`` + ``experiments/``) in
the fast tier: the ``SearchParams`` seam actually reaches the production
pipeline, the default params reproduce the shipped ranking bit-for-bit, every
checked-in experiment satisfies the contract, and a full lexical lab run
produces a scored leaderboard. All model-free — under the suite's pins this
exercises the lexical stack, the same mode as the script's default run.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from thread_archive._retrieval import SearchParams
from thread_archive._retrieval import rank as _rank

from .quality_corpus import build_corpus, run_cases, top_threads

EXPERIMENTS_DIR = Path(__file__).resolve().parent.parent / "experiments"


def _lab():
    import importlib.util
    import sys

    mod = sys.modules.get("search_lab")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "search_lab",
            Path(__file__).resolve().parent.parent / "scripts" / "search_lab.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["search_lab"] = mod
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def corpus(archive_home):
    return build_corpus(archive_home)


# ── the params seam ──────────────────────────────────────────────────────────

def test_ranker_honors_params() -> None:
    """The unit seam: rank_search_results takes its weights from SearchParams."""
    now = datetime(2026, 7, 1, 12, 0)
    dense_old = {"event_id": 1, "thread_id": "t-old", "content_type": "user",
                 "full_content": "alpha beta gamma", "occurred_at": "2026-01-01T12:00:00"}
    sparse_new = {"event_id": 2, "thread_id": "t-new", "content_type": "user",
                  "full_content": "alpha " + "filler " * 200,
                  "occurred_at": "2026-07-01T11:00:00"}
    terms = ["alpha", "beta", "gamma"]

    default = _rank.rank_search_results([sparse_new, dense_old], terms, 2, now=now)
    assert default[0]["thread_id"] == "t-old"  # density dominates at shipped weights

    recency_only = SearchParams(density_weight=0.0, phrase_weight=0.0, recency_weight=1.0)
    flipped = _rank.rank_search_results([sparse_new, dense_old], terms, 2,
                                        params=recency_only, now=now)
    assert flipped[0]["thread_id"] == "t-new"


def test_params_reach_the_production_pipeline(corpus) -> None:
    """The integration seam: search(params=...) changes the live ranking. At
    shipped weights the focused auth thread wins 'authentication'; a config
    that discounts user text and inflates assistant text hands the query to
    the decoy that mentions it in passing."""
    assert top_threads("authentication")[0] == corpus["auth"]
    skewed = SearchParams(content_type_weights={"user": 0.5, "text": 5.0})
    assert top_threads("authentication", params=skewed)[0] == corpus["css-decoy"]


def test_default_params_reproduce_the_shipped_ranking(corpus) -> None:
    """SearchParams() IS the production configuration — same report, whole case set."""
    incumbent = run_cases(corpus)
    explicit = run_cases(corpus, params=SearchParams())
    assert {k: v for k, v in explicit.items() if k != "latency_p50_ms"} \
        == {k: v for k, v in incumbent.items() if k != "latency_p50_ms"}


# ── the experiment bench ─────────────────────────────────────────────────────

def test_every_experiment_satisfies_the_contract() -> None:
    experiments = _lab().discover(EXPERIMENTS_DIR)
    assert len(experiments) >= 5, "the bench ships with a real spread of configurations"
    names = [e.name for e in experiments]
    assert len(set(names)) == len(names)
    for e in experiments:
        assert e.hypothesis and callable(e.search), e.name


def test_lab_run_scores_every_experiment_against_baseline(corpus) -> None:
    """A full lexical lab pass: every checked-in configuration runs end to end
    and lands on the leaderboard with metrics and a delta vs the baseline."""
    lab = _lab()
    experiments = lab.discover(EXPERIMENTS_DIR)
    report = lab.run_lab(corpus, experiments)
    rows = report["rows"]
    assert rows[0]["name"] == "baseline" and rows[0]["delta_mrr"] == 0.0
    assert {r["name"] for r in rows[1:]} == {e.name for e in experiments}
    mrrs = [r["mrr"] for r in rows[1:]]
    assert mrrs == sorted(mrrs, reverse=True)  # leaderboard order
    for r in rows:
        assert 0.0 <= r["mrr"] <= 1.0
        assert set(r["recall"]) >= {"1", "5", "10"}
    # The bench can distinguish configurations: stripping the whole weighted
    # ranker (pool_order) must not beat the shipped weights on this corpus.
    by_name = {r["name"]: r for r in rows}
    assert by_name["pool_order"]["mrr"] <= by_name["baseline"]["mrr"]
