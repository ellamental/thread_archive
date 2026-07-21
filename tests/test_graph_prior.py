"""The graph-authority ranking prior (_retrieval.graph_prior).

- weight parsing: default / off / explicit float / garbage
- normalization: pool-relative, boost-only, bounded by the weight
- ranker plumbing: a prior reorders comparable hits, absence penalizes nothing
- fail-soft: no curation (or no librarian) yields {} and search still runs
"""

from __future__ import annotations

from thread_archive._retrieval import graph_prior, rank


def test_prior_weight_parsing():
    assert graph_prior.prior_weight("") == 0.0  # off by default — eval scored it negative
    assert graph_prior.prior_weight("off") == 0.0
    assert graph_prior.prior_weight("0") == 0.0
    assert graph_prior.prior_weight("0.4") == 0.4  # explicit opt-in
    assert graph_prior.prior_weight("-1") == 0.0  # never a penalty
    assert graph_prior.prior_weight("banana") == 0.0  # garbage stays off, loudly


def test_normalize_pagerank_bounded_and_boost_only():
    pr = {"a": 0.02, "b": 0.01, "c": 0.0}
    out = graph_prior.normalize_pagerank(pr, 0.25)
    assert out["a"] == 0.25  # pool max hits the ceiling exactly
    assert abs(out["b"] - 0.125) < 1e-9  # proportional below it
    assert "c" not in out  # zero PageRank carries no boost entry
    assert graph_prior.normalize_pagerank(pr, 0.0) == {}
    assert graph_prior.normalize_pagerank({}, 0.25) == {}
    assert graph_prior.normalize_pagerank({"a": 0.0}, 0.25) == {}


def _hit(thread_id, content, event_id):
    return {
        "thread_id": thread_id,
        "event_id": event_id,
        "full_content": content,
        "content_type": "text",
        "occurred_at": "2026-01-01T00:00:00",
    }


def test_thread_prior_breaks_ties_without_penalizing():
    # Two hits identical in every ranking signal; only the prior differs.
    hits = [_hit("t-plain", "the auth flow decision", 1),
            _hit("t-cited", "the auth flow decision", 2)]
    terms = ["auth", "flow"]

    baseline = rank.rank_search_results(list(hits), terms, 10)
    assert baseline[0]["thread_id"] == "t-plain"  # input order wins a true tie

    boosted = rank.rank_search_results(list(hits), terms, 10,
                                       thread_prior={"t-cited": 0.25})
    assert boosted[0]["thread_id"] == "t-cited"

    # A prior on an absent thread changes nothing for the ones present.
    unrelated = rank.rank_search_results(list(hits), terms, 10,
                                         thread_prior={"t-elsewhere": 0.25})
    assert [h["thread_id"] for h in unrelated] == [h["thread_id"] for h in baseline]


def test_prior_is_tiebreaker_scale_not_an_override():
    # A strong lexical match must beat a weakly-matching but boosted thread.
    hits = [_hit("t-strong", "the auth flow decision in march", 1),
            _hit("t-boosted", "auth mentioned once in a long unrelated message " + "x" * 2000, 2)]
    ranked = rank.rank_search_results(list(hits), ["auth", "flow", "decision"], 10,
                                      thread_prior={"t-boosted": 0.25})
    assert ranked[0]["thread_id"] == "t-strong"


def test_thread_graph_prior_fail_soft_paths(archive_home):
    # weight 0 short-circuits before touching the librarian at all
    assert graph_prior.thread_graph_prior(["x"], weight=0.0) == {}
    assert graph_prior.thread_graph_prior([], weight=0.25) == {}
    # Against a fresh archive with no curation, the graph has no nodes: {} —
    # whether or not thread-librarian is importable in this venv. This is the
    # production fail-soft path a base install exercises on every search.
    assert graph_prior.thread_graph_prior(["nonexistent-thread"], weight=0.25) == {}
