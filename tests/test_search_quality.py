"""Tier-0 search-quality eval: relevance floors + ranking invariants, offline.

The fast tier of the search-quality ladder (the tiers are mapped in
docs/search-quality.md): every pytest run scores the
production lexical pipeline against the checked-in synthetic corpus and case
set in ``quality_corpus.py`` — deterministic, model-free, seconds. The metric
floors are a ratchet against the corpus's known relevance structure; the
invariant tests pin the individual ranking behaviours (density, phrase
contiguity, recency, decoy resistance) so a floor breach comes with a named
cause. ``-m quality_models`` runs the same corpus under the real model arms;
the live-archive tiers (snapshot-bound golds, judge, BEIR) measure real usage.
"""

from __future__ import annotations

import pytest

from .quality_corpus import CASES, build_corpus, run_cases, top_threads

# Floors, not targets: the corpus is built to be near-perfectly solvable by the
# lexical stack (measured MRR ≈ 1.0), so the floors sit with headroom below
# that. A breach means a ranking change reshuffled known-relevance cases.
MIN_MRR = 0.85
MIN_RECALL_5 = 0.90


@pytest.fixture
def corpus(archive_home) -> dict[str, str]:
    return build_corpus(archive_home)


def test_lexical_quality_floors(corpus) -> None:
    report = run_cases(corpus)
    assert report["n"] == len(CASES)
    assert report["mrr"] >= MIN_MRR, (
        f"quality floor breach: MRR {report['mrr']:.3f} < {MIN_MRR} "
        f"(per-shape: {report['per_shape']})")
    assert report["recall"][5] >= MIN_RECALL_5, (
        f"quality floor breach: recall@5 {report['recall'][5]:.3f} < {MIN_RECALL_5}")


def test_every_gold_reachable_at_limit(corpus) -> None:
    """No case may lose its gold entirely: recall@10 stays perfect. Separate
    from the floors so a single dropped-to-nowhere thread names itself."""
    report = run_cases(corpus)
    assert report["recall"][10] == 1.0, report


def test_focused_thread_beats_passing_mentions(corpus) -> None:
    """A thread about authentication outranks a long dump and an unrelated
    thread that each mention the term once, AND a paste that spams the term
    dozens of times (bm25's favourite — per-length density normalization is
    what keeps it down)."""
    ranked = top_threads("authentication")
    assert ranked[0] == corpus["auth"]
    for decoy in ("dump", "css-decoy", "auth-spam"):
        if corpus[decoy] in ranked:
            assert ranked.index(corpus[decoy]) > 0


def test_contiguous_phrase_beats_scattered_words(corpus) -> None:
    """Unquoted multi-word query: the thread carrying the contiguous phrase
    outranks the one with the same words scattered across sentences."""
    ranked = top_threads("graceful shutdown handler")
    assert corpus["phrase"] in ranked and corpus["phrase-scatter"] in ranked
    assert ranked.index(corpus["phrase"]) < ranked.index(corpus["phrase-scatter"])


def test_quoted_phrase_excludes_scattered_words(corpus) -> None:
    ranked = top_threads('"graceful shutdown handler"')
    assert ranked == [corpus["phrase"]]


def test_recency_breaks_a_density_tie(corpus) -> None:
    """Near-equal term density, months apart: the recent thread wins."""
    ranked = top_threads("ingest pipeline metrics")
    assert corpus["recency-new"] in ranked and corpus["recency-old"] in ranked
    assert ranked.index(corpus["recency-new"]) < ranked.index(corpus["recency-old"])


def test_recency_outranks_a_lexically_stronger_twin(corpus) -> None:
    """The old twin repeats the query terms (bm25 prefers it); the recency
    tiebreaker still resolves the pair toward the thread from this week."""
    ranked = top_threads("metrics review")
    assert ranked[0] == corpus["recency-new"]
    assert corpus["recency-old"] in ranked


def test_identifier_query_hits_its_thread_first(corpus) -> None:
    assert top_threads("save_vectors_sidecar")[0] == corpus["identifier"]


def test_candidate_ranker_measured_on_same_cases(corpus) -> None:
    """The science hook: ``run_cases(search=...)`` scores any candidate ranker
    on identical cases. A deliberately broken ranker (reversed production
    order) must measure clearly worse than the incumbent — if it doesn't, the
    harness can't tell rankers apart and every A/B number is noise."""
    from thread_archive._retrieval import search as production

    incumbent = run_cases(corpus)

    def reversed_ranker(query, **kw):
        return list(reversed(production(query, **kw)))

    degraded = run_cases(corpus, search=reversed_ranker)
    # Many cases match only a thread or two, where reversal costs little —
    # the measurable gap is what matters, not its size.
    assert degraded["mrr"] <= incumbent["mrr"] - 0.1
