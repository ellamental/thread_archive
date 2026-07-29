"""The candidate-pool cache and the ranker's feature/weight split.

Both exist so a scoring loop can re-rank a pool it already has. The properties
that make that safe are what's pinned here: the key sees every pool-affecting
knob (a key that doesn't makes a sweep silently measure nothing), hits are
insulated from the pipeline's downstream mutation of the hits it ranks, and the
split scorer is arithmetically identical to the one it replaced.
"""

from __future__ import annotations

from datetime import datetime

from thread_archive._retrieval import pool_cache, rank
from thread_archive._retrieval.params import SearchParams

NOW = datetime(2026, 7, 1, 12, 0, 0)


def _hit(event_id: int, content: str, *, rrf: float = 0.0, ct: str = "user",
         lex: float | None = None) -> dict:
    hit = {
        "event_id": event_id, "thread_id": f"t{event_id}", "content_type": ct,
        "full_content": content, "occurred_at": "2026-06-01T00:00:00", "_rrf": rrf,
    }
    if lex is not None:
        hit["_lex"] = lex
    return hit


# --- the key ----------------------------------------------------------------


def test_key_separates_pool_shaping_params() -> None:
    # rrf_k and the pool depth reach the arms and the fusion, so two configs that
    # differ in them must not share a pool. Getting this wrong doesn't slow a
    # sweep, it makes the knob read as having no effect.
    base = dict(over=200, rrf_k=60, structural=False)
    assert pool_cache.key_for("q", **base) == pool_cache.key_for("q", **base)
    assert pool_cache.key_for("q", **{**base, "rrf_k": 30}) != pool_cache.key_for("q", **base)
    assert pool_cache.key_for("q", **{**base, "over": 400}) != pool_cache.key_for("q", **base)


def test_key_separates_every_structural_scope() -> None:
    base = dict(over=200, rrf_k=60, structural=False)
    ref = pool_cache.key_for("q", **base)
    for field, value in (
        ("thread_id", "t1"), ("thread_ids", ["a"]), ("content_types", ["user"]),
        ("exclude_content_types", ["tool"]), ("since", "2026-01-01"),
        ("until", "2026-01-01"), ("tool_name", "Bash"), ("source", ["claude"]),
        ("types", ["system"]), ("agents", "include"), ("startswith", "x"),
        ("oldest_first", True), ("or_fallback", False), ("structural", True),
    ):
        assert pool_cache.key_for("q", **{**base, field: value}) != ref, field


def test_key_ignores_list_ordering() -> None:
    base = dict(over=200, rrf_k=60, structural=False)
    assert (pool_cache.key_for("q", content_types=["user", "text"], **base)
            == pool_cache.key_for("q", content_types=["text", "user"], **base))


def test_key_covers_every_pool_reaching_search_param() -> None:
    # A guard against the failure mode the key exists to prevent: a new
    # SearchParams field that reaches the arms or the fusion, added without being
    # keyed, would make every config read back the first one's pool. Fields that
    # only reach the *ranking* half are correctly invisible here.
    ranking_only = {
        "density_weight", "phrase_weight", "recency_weight", "fusion_weight",
        "bm25_weight", "bm25_score_weight", "semantic_weight",
        "thread_evidence_weight",
        "content_type_weights", "recency_half_life_hours",
        "density_norm_chars", "coherence_gamma",
    }
    # pool_floor reaches the key folded into `over`, which the caller resolves.
    keyed = {"rrf_k", "pool_floor"}
    assert set(vars(SearchParams())) == ranking_only | keyed


# --- the cache ---------------------------------------------------------------


def test_hits_are_insulated_from_caller_mutation() -> None:
    # The pipeline writes onto the hits it ranks (thread_title, context).
    # Handing out the stored dicts would leak one config's enrichment into the
    # next one's scoring.
    cache = pool_cache.PoolCache()
    cache.put(("k",), [_hit(1, "alpha")])

    first = cache.get(("k",))
    first[0]["context"] = "  1 | mutated"
    first[0]["thread_title"] = "mutated"

    second = cache.get(("k",))
    assert "context" not in second[0]
    assert "thread_title" not in second[0]


def test_put_copies_so_later_mutation_does_not_reach_the_store() -> None:
    cache = pool_cache.PoolCache()
    pool = [_hit(1, "alpha")]
    cache.put(("k",), pool)
    pool[0]["full_content"] = "clobbered"
    assert cache.get(("k",))[0]["full_content"] == "alpha"


def test_miss_reports_none_and_counts() -> None:
    cache = pool_cache.PoolCache()
    assert cache.get(("absent",)) is None
    cache.put(("k",), [_hit(1, "a")])
    cache.get(("k",))
    assert (cache.hits, cache.misses) == (1, 1)
    assert cache.hit_rate == 0.5


def test_roundtrips_through_a_file(tmp_path) -> None:
    path = tmp_path / "pools.pkl"
    cache = pool_cache.PoolCache(namespace="snap1", path=path)
    cache.put(("k",), [_hit(1, "alpha")])
    cache.save()

    reloaded = pool_cache.PoolCache(namespace="snap1", path=path)
    assert len(reloaded) == 1
    assert reloaded.get(("k",))[0]["full_content"] == "alpha"


def test_a_different_namespace_is_not_served(tmp_path) -> None:
    # Namespace is the corpus: pools mined from one snapshot describe documents
    # another one may not have.
    path = tmp_path / "pools.pkl"
    written = pool_cache.PoolCache(namespace="snap1", path=path)
    written.put(("k",), [_hit(1, "alpha")])
    written.save()

    assert len(pool_cache.PoolCache(namespace="snap2", path=path)) == 0


def test_an_unreadable_cache_file_costs_a_refetch_not_a_crash(tmp_path) -> None:
    path = tmp_path / "pools.pkl"
    path.write_bytes(b"not a pickle")
    cache = pool_cache.PoolCache(namespace="snap1", path=path)
    assert len(cache) == 0
    assert cache.get(("k",)) is None


def test_install_is_scoped_and_nests() -> None:
    assert pool_cache.current() is None
    outer = pool_cache.PoolCache()
    with pool_cache.install(outer):
        assert pool_cache.current() is outer
        with pool_cache.install(pool_cache.PoolCache()) as inner:
            assert pool_cache.current() is inner
        assert pool_cache.current() is outer
        with pool_cache.suspend():
            assert pool_cache.current() is None
        assert pool_cache.current() is outer
    assert pool_cache.current() is None


# --- the feature/weight split ------------------------------------------------


def test_split_scoring_matches_the_combined_ranker() -> None:
    pool = [
        _hit(1, "alpha beta gamma", rrf=0.9),
        _hit(2, "beta only, and a good deal of unrelated padding text", rrf=0.1),
        _hit(3, "alpha beta", rrf=0.5, ct="thinking"),
    ]
    terms = ["alpha", "beta"]
    params = SearchParams()
    scores = rank.score_from_features(
        rank.score_features(pool, terms, params=params, now=NOW), params)
    ranked = rank.rank_search_results(pool, terms, 3, params=params, now=NOW)
    expected = [pool[i] for i in sorted(range(3), key=lambda i: (-scores[i], i))]
    assert ranked == expected


def test_features_do_not_move_with_the_weights() -> None:
    # The property the whole cache rests on: a weight sweep re-reads one set of
    # feature rows.
    pool = [_hit(1, "alpha beta", rrf=0.4), _hit(2, "alpha", rrf=0.9)]
    terms = ["alpha", "beta"]
    a = rank.score_features(pool, terms, params=SearchParams(), now=NOW)
    b = rank.score_features(
        pool, terms, params=SearchParams(density_weight=1.0, fusion_weight=7.0), now=NOW)
    assert a == b


def test_scoring_is_scale_invariant() -> None:
    # All five weights times a constant is the same order — the reason
    # density_weight reads as the anchor the others are calibrated against.
    pool = [_hit(1, "alpha beta", rrf=0.4, lex=0.3), _hit(2, "alpha", rrf=0.9, lex=0.1),
            _hit(3, "beta gamma delta epsilon", rrf=0.2, lex=1.0)]
    terms = ["alpha", "beta"]
    base = SearchParams(bm25_weight=250.0)
    scaled = SearchParams(
        density_weight=base.density_weight * 3, phrase_weight=base.phrase_weight * 3,
        recency_weight=base.recency_weight * 3, fusion_weight=base.fusion_weight * 3,
        bm25_weight=base.bm25_weight * 3)
    assert (rank.rank_search_results(pool, terms, 3, params=base, now=NOW)
            == rank.rank_search_results(pool, terms, 3, params=scaled, now=NOW))


# --- the bm25 term -----------------------------------------------------------


def test_bm25_term_is_scored_at_the_shipped_default() -> None:
    # The shipped weight is live, so a hit's bm25 standing changes where it lands.
    # A pool with no _lex at all still scores — the term degrades to 0, which is
    # what a semantic-only pool and every pre-stamp cached pool look like.
    terms = ["alpha", "beta"]
    head = [_hit(1, "alpha beta padding", lex=1.0), _hit(2, "alpha beta padding", lex=0.02)]
    assert [h["event_id"] for h in rank.rank_search_results(head, terms, 2, now=NOW)] == [1, 2]
    flipped = [_hit(1, "alpha beta padding", lex=0.02), _hit(2, "alpha beta padding", lex=1.0)]
    assert [h["event_id"] for h in rank.rank_search_results(flipped, terms, 2, now=NOW)] == [2, 1]
    bare = [_hit(1, "alpha beta padding"), _hit(2, "alpha beta padding")]
    assert len(rank.rank_search_results(bare, terms, 2, now=NOW)) == 2


def test_bm25_term_can_outvote_density() -> None:
    # The failure the term exists for: density is IDF-blind and length-normalized,
    # so the short doc matching common terms outranks the long doc the lexical arm
    # put first. Weighted, the arm's own verdict wins the head back.
    terms = ["alpha", "beta"]
    short_confound = _hit(1, "alpha beta", lex=0.02)
    long_target = _hit(2, "alpha beta " + "unrelated padding " * 40, lex=1.0)
    pool = [short_confound, long_target]
    without = rank.rank_search_results(
        pool, terms, 2, params=SearchParams(bm25_weight=0.0), now=NOW)
    assert without[0]["event_id"] == 1
    ranked = rank.rank_search_results(
        pool, terms, 2, params=SearchParams(bm25_weight=500.0), now=NOW)
    assert ranked[0]["event_id"] == 2


def test_lex_is_absent_for_a_hit_no_lexical_arm_returned() -> None:
    # A semantic-only hit never had a bm25 standing; it must score 0 on the term
    # rather than inherit a neighbour's.
    feats = rank.score_features([_hit(1, "alpha", rrf=0.9)], ["alpha"], now=NOW)
    assert feats[0][4] == 0.0


# --- against a real corpus ---------------------------------------------------


def test_cached_search_returns_exactly_the_uncached_result(archive_home) -> None:
    """The transparency property: installing a cache must not move a ranking.

    Scored end to end over the synthetic corpus, because the interesting failures
    are downstream of the pool — a hit mutated by one query's grouping, an arm's
    provenance dropped in the copy — and none of them show in a unit test of the
    cache itself.
    """
    from tests import quality_corpus
    from thread_archive._retrieval import search

    quality_corpus.build_corpus(archive_home)
    queries = [q for q, _ in quality_corpus.CASES]

    cold = [search(q, limit=10) for q in queries]

    cache = pool_cache.PoolCache()
    with pool_cache.install(cache):
        warm = [search(q, limit=10) for q in queries]   # fills
        again = [search(q, limit=10) for q in queries]  # serves

    assert cache.hits > 0, "the second pass should have hit the cache"
    for q, a, b, c in zip(queries, cold, warm, again):
        assert [h["event_id"] for h in a] == [h["event_id"] for h in b], q
        assert [h["event_id"] for h in a] == [h["event_id"] for h in c], q


def test_a_pool_shaping_param_is_not_served_a_stale_pool(archive_home) -> None:
    # The footgun the key exists to close: sweeping rrf_k against a cache that
    # ignored it would read back the first value's pool for every value, and the
    # knob would measure as inert.
    from tests import quality_corpus
    from thread_archive._retrieval import SearchParams, search

    quality_corpus.build_corpus(archive_home)
    query = quality_corpus.CASES[0][0]

    with pool_cache.install(pool_cache.PoolCache()) as cache:
        search(query, limit=10, params=SearchParams(rrf_k=60))
        before = cache.misses
        search(query, limit=10, params=SearchParams(rrf_k=5))
        assert cache.misses > before, "rrf_k change must miss, not serve a stale pool"

        deeper = cache.misses
        search(query, limit=10, params=SearchParams(pool_floor=400))
        assert cache.misses > deeper, "pool_floor change must miss"
