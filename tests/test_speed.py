"""The latency measurement core.

The parts pinned here are the ones that must be right regardless of what the
machine's clock does: the aggregation, that the pool cache is actually suspended
while timing, and that the baseline round-trips. The wall-clock numbers
themselves aren't asserted —
they're a distribution, and the point of the module is to report one, not to hit a
target in a unit test.
"""

from __future__ import annotations

from search_lab import speed
from thread_archive._retrieval import pool_cache

# --- percentiles + aggregation ----------------------------------------------


def test_percentile_is_nearest_rank() -> None:
    xs = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert speed.percentile(xs, 0.5) == 30.0
    assert speed.percentile(xs, 0.95) == 50.0
    assert speed.percentile([], 0.5) == 0.0
    assert speed.percentile([7.0], 0.99) == 7.0


def _sample(total, *, fts=0.0, sem=0.0, pool=200,
            shape="natural", query="q") -> dict:
    return {"total_ms": total, "fts_ms": fts, "semantic_ms": sem,
            "pool_size": pool, "shape": shape, "query": query}


def test_summarize_builds_the_distribution() -> None:
    samples = [_sample(float(t), query=f"q{t}") for t in (100, 200, 300, 400, 500)]
    stats = speed._summarize(samples, n_queries=5, reps=1)
    assert stats.total["p50"] == 300.0
    assert stats.total["p99"] == 500.0
    assert stats.n_samples == 5


def test_summarize_reports_the_shape_split() -> None:
    samples = [
        _sample(100, shape="natural", query="a"),
        _sample(200, shape="natural", query="b"),
        _sample(50, shape="code", query="c"),
        _sample(60, shape="code", query="d"),
    ]
    stats = speed._summarize(samples, n_queries=4, reps=1)
    assert stats.by_shape["code"]["n"] == 2
    assert stats.by_shape["natural"]["p95"] == 200.0


def test_summarize_keeps_per_query_p50() -> None:
    samples = [
        _sample(100, query="fast"), _sample(120, query="fast"),
        _sample(900, query="slow"), _sample(1100, query="slow"),
    ]
    stats = speed._summarize(samples, n_queries=2, reps=2)
    assert stats.by_query["fast"] == 100.0
    assert stats.by_query["slow"] == 900.0


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
        return []

    stats = speed.measure(["q"], search=fake_search, reps=2, warmup=False)
    assert stats.stages["fts_ms"]["p50"] == 40.0
    assert stats.stages["semantic_ms"]["p50"] == 60.0


def test_measure_replays_the_arguments_a_call_was_made_with() -> None:
    """The parameters are part of the cost, not a detail of it — a recorded browse
    ask replayed as bare text at the default limit measures a workload nobody ran."""
    seen: list[dict] = []

    def fake_search(query, **kw):
        seen.append(kw)
        return []

    speed.measure([("q", {"group": "browse", "page": 4}), "plain"],
                  search=fake_search, reps=1, warmup=False, limit=10)
    assert seen[0] == {"limit": 10, "group": "browse", "page": 4}
    # …and a bare string still means "defaults", so existing callers are unchanged.
    assert seen[1] == {"limit": 10}


def test_a_replayed_call_may_override_the_benchs_own_limit() -> None:
    """A walk's later pages carry pools an order of magnitude deeper than page one;
    forcing the bench's limit over the recorded one would flatten exactly that."""
    seen: list[dict] = []

    def fake_search(query, **kw):
        seen.append(kw)
        return []

    speed.measure([("q", {"limit": 50})], search=fake_search, reps=1, warmup=False,
                  limit=10)
    assert seen[0]["limit"] == 50


# --- the timeseries + baseline ----------------------------------------------


def _stats() -> speed.LatencyStats:
    return speed._summarize(
        [_sample(700, query="q1"), _sample(1400, query="q2")], n_queries=2, reps=1)


def test_baseline_roundtrips_with_per_query(archive_home) -> None:
    speed.write_baseline(archive_home, snapshot_id="snap1", stats=_stats())
    blob = speed.read_baseline(archive_home, snapshot_id="snap1")
    assert blob is not None
    assert blob["by_query"] == {"q1": 700.0, "q2": 1400.0}


def test_baseline_from_another_snapshot_is_not_served(archive_home) -> None:
    speed.write_baseline(archive_home, snapshot_id="snap1", stats=_stats())
    assert speed.read_baseline(archive_home, snapshot_id="snap2") is None


def test_each_query_set_keeps_its_own_baseline(archive_home) -> None:
    """Two query sets are two populations — a p50 over one is not a reference for
    the other. One file would mean whichever set ran last defined the reference for
    both."""
    observed = speed._summarize([_sample(4000, query="o")], n_queries=1, reps=1)
    curated = speed._summarize([_sample(700, query="g")], n_queries=1, reps=1)
    speed.write_baseline(archive_home, snapshot_id=None, stats=observed)
    speed.write_baseline(archive_home, snapshot_id=None, stats=curated,
                         query_set="curated")
    assert speed.read_baseline(archive_home)["by_query"] == {"o": 4000.0}
    assert speed.read_baseline(
        archive_home, query_set="curated")["by_query"] == {"g": 700.0}


def test_a_baseline_from_another_query_set_reads_as_absent(archive_home) -> None:
    # Not a fallback: a reference over a different population would silently become
    # the thing a run is diffed against.
    speed.write_baseline(archive_home, snapshot_id=None, stats=_stats())
    assert speed.read_baseline(archive_home, query_set="curated") is None


def test_a_run_row_names_the_population_it_measured(archive_home) -> None:
    speed.record_run(archive_home, snapshot_id=None, stats=_stats())
    speed.record_run(archive_home, snapshot_id=None, stats=_stats(),
                     query_set="curated")
    rows = [__import__("json").loads(line) for line in
            (archive_home / speed.LATENCY_RUNS_FILE).read_text().splitlines()]
    assert [r["query_set"] for r in rows] == [speed.OBSERVED_SET, "curated"]


def test_record_run_appends_and_flags_a_tuning_run(archive_home) -> None:
    import json

    speed.record_run(archive_home, snapshot_id="s", stats=_stats(),
                     overrides={"pool_floor": 300})
    rows = [json.loads(line) for line in
            (archive_home / speed.LATENCY_RUNS_FILE).read_text().splitlines()]
    assert rows[0]["kind"] == "latency-run"
    assert rows[0]["overrides"] == {"pool_floor": 300}
    assert "by_query" not in rows[0]  # compact ledger row, not the baseline


def test_ledger_can_be_disabled(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_LATENCY_LOG", "0")
    speed.record_run(archive_home, snapshot_id="s", stats=_stats())
    assert not (archive_home / speed.LATENCY_RUNS_FILE).exists()
