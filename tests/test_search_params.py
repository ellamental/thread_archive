"""The ``SearchParams`` seam: an alternative configuration reaches the ranking.

Every numeric knob of the retrieval pipeline lives on a :class:`SearchParams`,
and ``search(params=...)`` is the seam a candidate configuration rides — the
one the gold gate's tuning loop and ``quality_corpus.run_cases(params=...)``
both go through. Two contracts hold it open: an alternative instance actually
changes the live ranking (a seam that silently ignored ``params`` would make
every measured configuration a re-measurement of the incumbent), and the
default instance reproduces the shipped ranking bit-for-bit (``SearchParams()``
IS production, so a baseline row scored through the seam is comparable to one
scored without it).

All model-free — under the suite's pins this exercises the lexical stack.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from thread_archive._retrieval import SearchParams
from thread_archive._retrieval import rank as _rank

from .quality_corpus import build_corpus, run_cases, top_threads


@pytest.fixture
def corpus(archive_home):
    return build_corpus(archive_home)


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
