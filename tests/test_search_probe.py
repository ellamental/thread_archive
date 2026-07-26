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
        probe.did_rerank = True
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
    probe.rerank_ms = 100.06
    probe.did_rerank = True
    probe.pool_size = 198
    rec = probe.as_record()
    # Both arms ran, so both sub-splits ride along (all zero here — nothing
    # recorded into them).
    assert rec == {"fts_ms": 12.3, "semantic_ms": 5.0, "rerank_ms": 100.1,
                   "did_rerank": True, "pool_size": 198,
                   "match_ms": 0.0, "scan_ms": 0.0, "rescan_ms": 0.0,
                   "build_ms": 0.0, "fts_passes": 0,
                   "embed_ms": 0.0, "scope_ms": 0.0, "matrix_ms": 0.0,
                   "knn_ms": 0.0, "hydrate_ms": 0.0}
    # cold is the exception (the cold-model tail), so it rides along only when set.
    assert "cold" not in rec
    probe.embed_cold = True
    rec = probe.as_record()
    assert rec["cold"] is True and rec["embed_cold"] is True
    assert "rerank_cold" not in rec


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


def test_bump_tallies_and_survives_an_unknown_counter() -> None:
    # The fail-soft contract the timing points rely on: a counter name that isn't a
    # slot is a bug in the caller, never a broken search.
    _probe.bump("fts_passes")  # no probe installed — a no-op, not an error
    with _probe.install() as probe:
        _probe.bump("fts_passes")
        _probe.bump("fts_passes")
        _probe.bump("no_such_counter")
    assert probe.fts_passes == 2


def test_cold_is_not_pinned_by_an_installed_but_idle_rerank_arm() -> None:
    # The whole reason the flag is per-arm: a cross-encoder that is installed and
    # never invoked must contribute no cold signal, or every search reads cold.
    probe = _probe.SearchProbe()
    assert probe.cold is False
    probe.rerank_cold = True
    assert probe.cold is True and probe.as_record()["rerank_cold"] is True


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


def test_the_vector_arm_records_into_the_probe_from_its_own_thread(archive_home, monkeypatch) -> None:
    """The two pool arms run concurrently, and the probe is context-local. A worker
    started without a copied context would find no probe and drop every stage it
    measured — the vector arm's whole sub-split, silently, in production only."""
    import json
    import threading

    from thread_archive import _retrieval
    from thread_archive._importers import import_session_incremental
    from thread_archive._store import init_db

    init_db()
    f = archive_home / "p.jsonl"
    f.write_text(json.dumps({
        "type": "user", "uuid": "u1", "timestamp": "2026-01-02T10:00:00Z", "sessionId": "s",
        "message": {"role": "user", "content": "the widget report"},
    }) + "\n", encoding="utf-8")
    import_session_incremental(f, "proj:p")

    caller = threading.current_thread()
    ran_on: list = []

    def _arm(query, **kw):
        ran_on.append(threading.current_thread())
        _probe.flag("matrix_built")  # only lands if the probe crossed into this thread
        return None

    monkeypatch.setattr(_retrieval, "_semantic_hits", _arm)
    with _probe.install() as probe:
        _retrieval.search("widget", limit=3)

    assert ran_on and ran_on[0] is not caller, "the vector arm did not leave the calling thread"
    assert probe.matrix_built is True, "the arm thread could not see the installed probe"
