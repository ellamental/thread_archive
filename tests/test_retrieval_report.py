"""The retrieval page's data layer.

These pin the three rules that make the difference between a chart and a plausible
lie: probes excluded, cold and warm never averaged, query sets and pooled runs kept
apart.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from search_lab import gold_runs, speed
from search_lab import retrieval_report as rr
from thread_archive._retrieval.usage import LEDGER_FILE


def _at(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _write(home, records: list[dict]) -> None:
    (home / LEDGER_FILE).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _search(duration, *, uptime=None, query="a real question", minutes_ago=0.0,
            **extra) -> dict:
    rec = {"at": _at(minutes_ago), "kind": "search", "query": query,
           "duration_ms": duration, **extra}
    if uptime is not None:
        rec["uptime_s"] = uptime
    return rec


def _filled(section: dict) -> list[dict]:
    """The buckets that actually hold something. The span is dense, so most of a
    24-hour window is empty by design."""
    return [b for b in section["buckets"] if b["n"]]


def test_percentile_of_nothing_is_zero() -> None:
    assert rr.percentile([], 0.5) == 0.0
    assert rr.percentile([5.0], 0.99) == 5.0


def test_probe_queries_are_not_counted_as_searches(archive_home) -> None:
    """A bench leaves one-character queries behind; they return in ~1 ms and pull
    every percentile toward a number no agent experienced."""
    _write(archive_home, [
        _search(1.0, query="x"),
        _search(1.0, query="test"),
        _search(500.0),
    ])
    out = rr.served(archive_home)
    assert out["n"] == 1


def test_cold_and_warm_are_reported_apart(archive_home) -> None:
    """A process's first search runs an order of magnitude slower than its
    thousandth, and restarts are frequent — one median over both tracks the restart
    rate rather than the code."""
    _write(archive_home, [
        _search(9000.0, uptime=5.0),
        _search(200.0, uptime=900.0),
        _search(240.0, uptime=1200.0),
    ])
    out = rr.served(archive_home)
    assert out["cold"]["n"] == 1 and out["cold"]["p50"] == 9000.0
    assert out["warm"]["n"] == 2 and out["warm"]["p50"] == 240.0


def test_a_search_with_no_recorded_process_age_is_its_own_bucket(archive_home) -> None:
    """Neither regime, and counted — folding it into whichever is more flattering
    is the error that made the ledger unreadable."""
    _write(archive_home, [_search(700.0), _search(200.0, uptime=900.0)])
    out = rr.served(archive_home)
    assert out["n_unknown_regime"] == 1
    assert out["warm"]["n"] == 1 and out["cold"]["n"] == 0
    assert _filled(out)[0]["unknown"]["n"] == 1


def test_stages_exclude_the_known_cold_but_keep_the_unproven(archive_home) -> None:
    """Requiring proof of warmth would empty the chart for as long as the ledger's
    memory is older than the uptime field."""
    _write(archive_home, [
        _search(9000.0, uptime=5.0, embed_ms=5000.0, fts_ms=100.0),
        _search(300.0, uptime=900.0, embed_ms=20.0, fts_ms=250.0),
        _search(400.0, embed_ms=25.0, fts_ms=300.0),
    ])
    out = rr.stages(archive_home)
    assert out["n"] == 2 and out["n_unproven"] == 1
    embed = next(s for s in out["stages"] if s["stage"] == "embed_ms")
    assert embed["p50"] < 100.0  # the 5s cold load is not in here
    # Slowest first, so the chart's first row is the thing to look at.
    assert out["stages"][0]["p50"] >= out["stages"][-1]["p50"]


def test_a_failed_search_still_counts_toward_the_distribution(archive_home) -> None:
    """Dropping slow errors biases every percentile toward the searches that
    happened to succeed."""
    _write(archive_home, [_search(30000.0, uptime=900.0, failed=True),
                          _search(200.0, uptime=900.0)])
    assert rr.served(archive_home)["warm"]["n"] == 2


def test_bench_series_never_merge_two_query_sets(archive_home) -> None:
    stats = speed._summarize([{"total_ms": 900.0, "shape": "natural", "query": "q"}],
                             n_queries=1, reps=1)
    speed.record_run(archive_home, snapshot_id=None, stats=stats)
    speed.record_run(archive_home, snapshot_id=None, stats=stats,
                     query_set=speed.OBSERVED_SET)
    series = rr.bench(archive_home)
    assert set(series) == {speed.GOLD_SET, speed.OBSERVED_SET}
    assert len(series[speed.GOLD_SET]) == 1 and len(series[speed.OBSERVED_SET]) == 1


def test_quality_drops_the_runs_that_measure_something_else(archive_home) -> None:
    """A pooled run scores from persisted pools and a tuning run scores a candidate
    configuration; both are real numbers about something other than the shipped
    pipeline, and a line that mixes them in is worse than a shorter line."""
    files = {"judged-cases.jsonl": {"n": 10, "mrr": 0.5, "ndcg10": 0.4}}
    gold_runs.record_run(archive_home, snapshot_id="s", files=files, passed=True,
                         config={}, commit="a")
    gold_runs.record_run(archive_home, snapshot_id="s", files=files, passed=True,
                         config={}, commit="b", pool_cache=True)
    gold_runs.record_run(archive_home, snapshot_id="s", files=files, passed=True,
                         config={}, commit="c", overrides={"rrf_k": 10})
    out = rr.quality(archive_home)
    assert [p["commit"] for p in out["points"]] == ["a"]
    assert out["latest"]["mrr"] == 0.5


def test_a_missing_ledger_yields_an_empty_section_not_a_failure(archive_home) -> None:
    """An operator view that goes blank when one input is absent is the least
    useful thing it could do."""
    out = rr.report(archive_home)
    assert out["served"]["n"] == 0
    assert out["stages"]["stages"] == []
    assert out["quality"]["points"] == []
    assert out["bench"] == {}


def test_report_carries_every_section(archive_home) -> None:
    _write(archive_home, [_search(200.0, uptime=900.0)])
    out = rr.report(archive_home, hours=7 * 24)
    assert out["hours"] == 7 * 24
    for section in ("served", "stages", "restarts", "bench", "quality"):
        assert section in out


def test_a_short_window_is_bucketed_by_the_hour(archive_home) -> None:
    """A day's worth of searches at daily resolution is one point, which cannot say
    whether the slow patch was this morning or the restart at lunch."""
    _write(archive_home, [
        _search(200.0, uptime=900.0, minutes_ago=10),
        _search(9000.0, uptime=5.0, minutes_ago=200),
    ])
    out = rr.served(archive_home, hours=24)
    assert out["bucket"] == rr.HOUR
    assert len(_filled(out)) == 2, "two searches three hours apart are two buckets"
    assert rr.default_bucket(24) == rr.HOUR and rr.default_bucket(14 * 24) == rr.DAY


def test_the_span_keeps_its_empty_buckets(archive_home) -> None:
    """Emitting only the buckets that saw traffic compresses the axis onto the times
    something happened, and a line drawn over that joins 3am to noon as though the
    hours between were steady. A quiet stretch is a fact about the window."""
    _write(archive_home, [_search(200.0, uptime=900.0)])
    out = rr.served(archive_home, hours=6)
    assert len(out["buckets"]) >= 6
    assert sum(b["n"] for b in out["buckets"]) == 1
    # Ordered oldest-first, so the chart's x-axis is time.
    assert [b["at"] for b in out["buckets"]] == sorted(b["at"] for b in out["buckets"])


def test_restarts_follow_the_windows_resolution(archive_home) -> None:
    """Read as a table rather than a chart, so it stays sparse — but it has to key
    on the same bucket as the latency beside it, or a spike cannot be lined up with
    the restart that caused it."""
    _write(archive_home, [
        {"at": _at(10), "kind": "warm", "duration_ms": 22000.0},
        {"at": _at(200), "kind": "warm", "duration_ms": 21000.0},
    ])
    out = rr.restarts(archive_home, hours=24)
    assert out["bucket"] == rr.HOUR
    assert len(out["buckets"]) == 2 and all(len(b["at"]) == 13 for b in out["buckets"])
    wide = rr.restarts(archive_home, hours=14 * 24)
    assert wide["bucket"] == rr.DAY
    assert all(len(b["at"]) == 10 for b in wide["buckets"])
