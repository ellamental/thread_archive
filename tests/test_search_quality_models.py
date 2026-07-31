"""Model-tier search-quality eval: the synthetic corpus under the real model arms.

The middle rung of the quality ladder (see docs/public/search-quality.md): the same
checked-in corpus and cases as the tier-0 lexical eval in
``test_search_quality.py``, but with the embeddings arm live — the fused
pipeline a full install runs. Deterministic corpus, real models: catches a
model-arm change that reshuffles known-relevance cases, offline, without touching
the live archive. Costs the torch model load (minutes, downloads on first run),
so it's an explicit lane: ``-m quality_models``, no CI row — run it by hand when
touching the semantic stack.
"""

from __future__ import annotations

import importlib.util

import pytest

from thread_archive import _api as api

from .quality_corpus import build_corpus, run_cases, top_threads

pytestmark = [
    pytest.mark.quality_models,
    pytest.mark.timeout(1200),  # two cold model loads dwarf the suite default
]

# The fused floors match the lexical tier's: the corpus is near-perfectly
# solvable lexically, and fusion must not un-solve it. A breach here with the
# lexical tier green points squarely at the model arms.
MIN_MRR = 0.85
MIN_RECALL_5 = 0.90


@pytest.fixture
def model_corpus(archive_home, monkeypatch):
    """The corpus with model arms enabled and vectors built."""
    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip("[embeddings] extra not installed")
    # Undo the suite-wide model-free pin; coherence stays off (its background
    # refresh thread outlives the test — see conftest).
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED", raising=False)
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    ids = build_corpus(archive_home)
    res = api.embed()
    assert res["embedded"] > 0, f"no vectors built: {res}"
    return ids


def test_fused_pipeline_holds_the_lexical_floors(model_corpus) -> None:
    report = run_cases(model_corpus)
    assert report["mrr"] >= MIN_MRR, (
        f"fused-pipeline floor breach: MRR {report['mrr']:.3f} < {MIN_MRR} "
        f"(per-shape: {report['per_shape']})")
    assert report["recall"][5] >= MIN_RECALL_5, report
    assert report["recall"][10] == 1.0, report


def test_semantic_arm_bridges_a_vocabulary_gap(model_corpus) -> None:
    """A query sharing no content words with its answer thread: lexical search
    can't reach it, so surfacing it is the semantic arm's whole job."""
    ranked = top_threads("signing in securely to an account")
    assert model_corpus["auth"] in ranked, (
        "semantic arm failed to surface the authentication thread for a "
        "zero-lexical-overlap paraphrase")


def test_the_fused_stack_keeps_a_solved_case_solved(model_corpus) -> None:
    """The vector arm, live, must not displace an unambiguous lexical answer."""
    ranked = top_threads("jwt authentication login flow")
    assert ranked[0] == model_corpus["auth"]
