"""The search stage-timing probe — opt-in context-local, fail-soft."""

from __future__ import annotations

from thread_archive._retrieval import _probe


def test_no_probe_installed_is_none() -> None:
    # The guard every timing point checks: nobody listening → nothing to record.
    assert _probe.current() is None


def test_install_yields_a_fresh_probe_and_restores_on_exit() -> None:
    assert _probe.current() is None
    with _probe.install() as probe:
        assert _probe.current() is probe
        probe.fts_ms += 5.0
    # Slot restored — the probe does not leak past its block.
    assert _probe.current() is None


def test_install_nests_without_leaking() -> None:
    with _probe.install() as outer:
        with _probe.install() as inner:
            assert _probe.current() is inner
        # Leaving the inner block restores the outer probe, not None.
        assert _probe.current() is outer


def test_as_record_shape_and_cold_only_when_true() -> None:
    probe = _probe.SearchProbe()
    probe.fts_ms = 12.34
    probe.semantic_ms = 5.0
    probe.pool_size = 198
    rec = probe.as_record()
    # Both arms ran, so both sub-splits ride along (all zero here — nothing
    # recorded into them).
    assert rec == {"fts_ms": 12.3, "semantic_ms": 5.0, "pool_size": 198,
                   "match_ms": 0.0, "scan_ms": 0.0, "rescan_ms": 0.0,
                   "build_ms": 0.0, "fts_passes": 0,
                   "embed_ms": 0.0, "scope_ms": 0.0, "matrix_ms": 0.0,
                   "knn_ms": 0.0, "hydrate_ms": 0.0}
    # cold is the exception (the cold-model tail), so it rides along only when set.
    assert "cold" not in rec
    probe.embed_cold = True
    rec = probe.as_record()
    assert rec["cold"] is True and rec["embed_cold"] is True


def test_substages_are_gated_per_arm() -> None:
    # A structural/tool-scoped search never reaches the vector arm. Explicit zeros
    # would read as "measured and instant" rather than "did not happen" — and the
    # gating is per arm, so the lexical split is still reported.
    probe = _probe.SearchProbe()
    probe.fts_ms = 40.0
    rec = probe.as_record()
    assert rec["semantic_ms"] == 0.0
    for name in _probe.SEMANTIC_SUBSTAGES:
        assert name not in rec
    for name in _probe.FTS_SUBSTAGES:
        assert name in rec
    assert rec["fts_passes"] == 0

    # A pool-cache hit sits both arms out: the record carries the pool it served
    # and no split at all.
    cached = _probe.SearchProbe()
    cached.pool_size = 200
    rec = cached.as_record()
    assert cached.ran is True
    for name in (*_probe.FTS_SUBSTAGES, *_probe.SEMANTIC_SUBSTAGES, "fts_passes"):
        assert name not in rec


def test_the_set_outcomes_ride_along_only_when_the_stage_ran() -> None:
    """``set_ms`` alone cannot see the memo working — the same duration is a cheap
    answer on a small corpus and a defeated memo on a large one. The outcome
    counters are what separate them, and like every other signal here their
    presence carries it: a search that never resolved a set says nothing."""
    probe = _probe.SearchProbe()
    probe.fts_ms = 10.0
    rec = probe.as_record()
    for name in _probe.SET_OUTCOMES:
        assert name not in rec

    probe.set_ms = 850.0
    probe.set_scans = 1
    rec = probe.as_record()
    assert rec["set_ms"] == 850.0 and rec["set_scans"] == 1
    assert "set_hits" not in rec


def test_set_outcomes_tally_because_one_search_can_resolve_two_sets() -> None:
    """One search can resolve more than one set query under a single probe, and they
    need not agree — one can be handed a memoized answer while the other scans.
    Counters, so the record says both happened."""
    with _probe.install() as probe:
        _probe.bump("set_hits")
        _probe.bump("set_scans")
    rec = probe.as_record()
    assert rec["set_hits"] == 1 and rec["set_scans"] == 1


def test_a_reused_query_vector_is_stated_not_inferred() -> None:
    """An ``embed_ms`` of ~0 on an arm that ran is ambiguous: the vector came from
    the cache, or the arm never embedded. The flag is what tells them apart, and it
    is meaningless on an arm that sat out."""
    probe = _probe.SearchProbe()
    probe.fts_ms = 10.0
    probe.embed_cached = True
    assert "embed_cached" not in probe.as_record(), "claimed a cache hit with no arm"

    probe.semantic_ms = 4.0
    rec = probe.as_record()
    assert rec["embed_cached"] is True and rec["embed_ms"] == 0.0


def test_bump_tallies_and_survives_an_unknown_counter() -> None:
    # The fail-soft contract the timing points rely on: a counter name that isn't a
    # slot is a bug in the caller, never a broken search.
    _probe.bump("fts_passes")  # no probe installed — a no-op, not an error
    with _probe.install() as probe:
        _probe.bump("fts_passes")
        _probe.bump("fts_passes")
        _probe.bump("no_such_counter")
    assert probe.fts_passes == 2


def test_record_and_flag_are_noops_without_a_probe() -> None:
    # Every record point sits on a hot path and must cost nothing when unmeasured.
    assert _probe.current() is None
    _probe.record("embed_ms", 0.0)  # must not raise
    _probe.flag("matrix_built")


def test_record_accumulates_and_flag_sets() -> None:
    from time import perf_counter

    with _probe.install() as probe:
        _probe.record("embed_ms", perf_counter())
        _probe.record("embed_ms", perf_counter())
        _probe.flag("matrix_built")
        # Accumulates rather than overwrites — a widen retry runs the arms twice
        # under one probe and the caller felt both.
        assert probe.embed_ms >= 0.0
        assert probe.matrix_built is True
    assert probe.as_record()["matrix_built"] is True


def test_unknown_stage_never_breaks_a_search() -> None:
    with _probe.install():
        _probe.record("no_such_ms", 0.0)
        _probe.flag("no_such_flag")



def test_shape_substages_ride_along_only_when_they_ran() -> None:
    # The post-pool stages are per-shape: a ranked search never extends, a browse
    # does. An explicit zero would read as "measured and instant" rather than "did
    # not happen", which is the distinction the whole gated-record convention keeps.
    probe = _probe.SearchProbe()
    probe.fts_ms = 1.0
    probe.rank_ms = 4.5
    probe.enrich_ms = 0.25
    rec = probe.as_record()
    assert rec["rank_ms"] == 4.5 and rec["enrich_ms"] == 0.2
    for name in ("coherence_ms", "group_ms", "extend_ms"):
        assert name not in rec


def test_shape_substages_are_a_disjoint_set_from_the_arms() -> None:
    # They measure what happens to a pool, not how it was found — so a stage name
    # landing in two groups would double-count it in any analysis over the ledger.
    arms = set(_probe.SEMANTIC_SUBSTAGES) | set(_probe.FTS_SUBSTAGES)
    assert not arms & set(_probe.SHAPE_SUBSTAGES)
    for name in _probe.SHAPE_SUBSTAGES:
        assert name in _probe.SearchProbe.__slots__
