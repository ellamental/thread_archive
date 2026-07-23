"""The latency measurement core and its smoke test.

The parts pinned here are the ones that must be right regardless of what the
machine's clock does: the aggregation, that the pool cache is actually suspended
while timing, that the smoke test cherry-picks the pathological queries, and that
the baseline round-trips. The wall-clock numbers themselves aren't asserted —
they're a distribution, and the point of the module is to report one, not to hit a
target in a unit test.
"""

from __future__ import annotations

from thread_archive._ops import speed
from thread_archive._retrieval import pool_cache

# --- percentiles + aggregation ----------------------------------------------


def test_percentile_is_nearest_rank() -> None:
    xs = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert speed.percentile(xs, 0.5) == 30.0
    assert speed.percentile(xs, 0.95) == 50.0
    assert speed.percentile([], 0.5) == 0.0
    assert speed.percentile([7.0], 0.99) == 7.0


def _sample(total, *, fts=0.0, sem=0.0, rr=0.0, did=False, pool=200,
            shape="natural", query="q") -> dict:
    return {"total_ms": total, "fts_ms": fts, "semantic_ms": sem, "rerank_ms": rr,
            "did_rerank": did, "pool_size": pool, "shape": shape, "query": query}


def test_summarize_builds_the_distribution() -> None:
    samples = [_sample(float(t), query=f"q{t}") for t in (100, 200, 300, 400, 500)]
    stats = speed._summarize(samples, n_queries=5, reps=1)
    assert stats.total["p50"] == 300.0
    assert stats.total["p99"] == 500.0
    assert stats.n_samples == 5


def test_summarize_reports_rerank_rate_and_shapes() -> None:
    samples = [
        _sample(100, did=True, shape="natural", query="a"),
        _sample(200, did=False, shape="natural", query="b"),
        _sample(50, did=False, shape="code", query="c"),
        _sample(60, did=False, shape="code", query="d"),
    ]
    stats = speed._summarize(samples, n_queries=4, reps=1)
    assert stats.rerank_rate == 0.25
    assert stats.by_shape["code"]["n"] == 2
    assert stats.by_shape["natural"]["p95"] == 200.0


def test_summarize_keeps_per_query_p50_for_the_smoke_set() -> None:
    samples = [
        _sample(100, query="fast"), _sample(120, query="fast"),
        _sample(900, query="slow"), _sample(1100, query="slow"),
    ]
    stats = speed._summarize(samples, n_queries=2, reps=2)
    assert stats.by_query["fast"] == 100.0
    assert stats.by_query["slow"] == 900.0


# --- the smoke set + ceiling -------------------------------------------------


def test_smoke_set_picks_the_slowest_at_baseline() -> None:
    baseline = {"by_query": {"a": 100.0, "b": 900.0, "c": 400.0, "d": 1500.0}}
    assert speed.smoke_set(baseline, 2) == ["d", "b"]


def test_smoke_set_is_empty_without_a_baseline() -> None:
    assert speed.smoke_set(None, 8) == []
    assert speed.smoke_set({}, 8) == []


def test_ceiling_prefers_an_explicit_budget() -> None:
    baseline = {"total": {"p95": 1000.0}}
    assert speed.ceiling_ms(baseline, budget_ms=1500.0, factor=1.5) == 1500.0


def test_ceiling_falls_back_to_a_multiple_of_baseline() -> None:
    baseline = {"total": {"p95": 1000.0}}
    assert speed.ceiling_ms(baseline, budget_ms=None, factor=1.5) == 1500.0


def test_ceiling_is_none_without_a_budget_or_baseline() -> None:
    assert speed.ceiling_ms(None, budget_ms=None, factor=1.5) is None
    assert speed.ceiling_ms({}, budget_ms=None, factor=1.5) is None


# --- measure() ---------------------------------------------------------------


def test_measure_suspends_the_pool_cache_while_timing() -> None:
    # The whole point of a latency number is that it includes the pool build; a
    # cache in an outer scope must NOT be consulted, or it would time lookups.
    seen: list = []

    def fake_search(query, **kw):
        seen.append(pool_cache.current())
        probe = __import__("thread_archive._retrieval._probe", fromlist=["current"]).current()
        if probe is not None:
            probe.fts_ms += 5.0
        return []

    outer = pool_cache.PoolCache()
    with pool_cache.install(outer):
        speed.measure(["q"], search=fake_search, reps=2)
    assert seen, "search should have run"
    assert all(c is None for c in seen), "cache must be suspended during measurement"


def test_measure_discards_the_warmup_and_times_the_reps() -> None:
    calls: list[str] = []

    def fake_search(query, **kw):
        calls.append(query)
        return []

    stats = speed.measure(["q"], search=fake_search, reps=3, warmup=True)
    assert len(calls) == 4  # 1 warmup + 3 timed
    assert stats.n_samples == 3


def test_measure_attributes_stage_times_from_the_probe() -> None:
    from thread_archive._retrieval import _probe

    def fake_search(query, **kw):
        probe = _probe.current()
        probe.fts_ms += 40.0
        probe.semantic_ms += 60.0
        probe.did_rerank = True
        return []

    stats = speed.measure(["q"], search=fake_search, reps=2, warmup=False)
    assert stats.stages["fts_ms"]["p50"] == 40.0
    assert stats.stages["semantic_ms"]["p50"] == 60.0
    assert stats.rerank_rate == 1.0


# --- the timeseries + baseline ----------------------------------------------


def _stats() -> speed.LatencyStats:
    return speed._summarize(
        [_sample(700, query="q1"), _sample(1400, query="q2")], n_queries=2, reps=1)


def test_baseline_roundtrips_with_per_query(archive_home) -> None:
    speed.write_baseline(archive_home, snapshot_id="snap1", stats=_stats())
    blob = speed.read_baseline(archive_home, snapshot_id="snap1")
    assert blob is not None
    assert blob["by_query"] == {"q1": 700.0, "q2": 1400.0}
    # and it drives the smoke set
    assert speed.smoke_set(blob, 1) == ["q2"]


def test_baseline_from_another_snapshot_is_not_served(archive_home) -> None:
    speed.write_baseline(archive_home, snapshot_id="snap1", stats=_stats())
    assert speed.read_baseline(archive_home, snapshot_id="snap2") is None


def test_record_run_appends_and_flags_a_tuning_run(archive_home) -> None:
    import json

    speed.record_run(archive_home, snapshot_id="s", stats=_stats(),
                     overrides={"rerank_auto": True})
    rows = [json.loads(line) for line in
            (archive_home / speed.LATENCY_RUNS_FILE).read_text().splitlines()]
    assert rows[0]["kind"] == "latency-run"
    assert rows[0]["overrides"] == {"rerank_auto": True}
    assert "by_query" not in rows[0]  # compact ledger row, not the baseline


def test_ledger_can_be_disabled(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_LATENCY_LOG", "0")
    speed.record_run(archive_home, snapshot_id="s", stats=_stats())
    assert not (archive_home / speed.LATENCY_RUNS_FILE).exists()
