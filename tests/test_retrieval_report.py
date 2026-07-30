"""The retrieval page's data layer.

These pin the rules that make the difference between a chart and a plausible lie:
probes excluded, cold and warm never averaged, the regime read off what a search
paid rather than off how young its process was, front doors counted apart,
workloads counted apart, query sets kept apart.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

# The report reads the viewer's own request ledger, and the viewer is dev-only —
# no wheel carries it (docs/web-viewer.md). search_lab is likewise repo-only.
pytest.importorskip("thread_archive._web", reason="the viewer is dev-only (no wheel carries it)")

from search_lab import retrieval_report as rr  # noqa: E402
from search_lab import speed  # noqa: E402
from thread_archive._retrieval.usage import LEDGER_FILE  # noqa: E402
from thread_archive._web.metrics import LEDGER_FILE as WEB_LEDGER_FILE  # noqa: E402


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


def _warm_search(duration, **extra) -> dict:
    """A search the probe measured and which paid nothing on the request thread —
    what the regime split calls warm. ``pool_size`` is the probe's fingerprint;
    without it a row is unclassifiable, which is a different claim."""
    return _search(duration, pool_size=120, **extra)


def _cold_search(duration, **extra) -> dict:
    """A search that loaded the model inside itself."""
    return _warm_search(duration, cold=True, embed_cold=True, **extra)


def _web_write(home, records: list[dict]) -> None:
    (home / WEB_LEDGER_FILE).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _web_search(duration, *, minutes_ago=0.0, status=200, **extra) -> dict:
    """One ``/api/search`` request as the viewer's ledger records it — no query
    text, and the probe's fields folded in flat, which is what lets this page read
    the two ledgers as one population."""
    return {"at": _at(minutes_ago), "path": "/api/search", "status": status,
            "duration_ms": duration, "pool_size": 120, **extra}


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
        _cold_search(9000.0),
        _warm_search(200.0),
        _warm_search(240.0),
    ])
    out = rr.served(archive_home)
    assert out["cold"]["n"] == 1 and out["cold"]["p50"] == 9000.0
    assert out["warm"]["n"] == 2 and out["warm"]["p50"] == 240.0


def test_the_typical_number_does_not_pool_bulk_sweeps(archive_home) -> None:
    """A limit=50 walk to page 40 is different work from a first-page question, and
    in a bulk-export week it is most of the warm pool. One median over both measures
    the window's workload mix, not what asking a question costs. A row recording
    neither a deep page nor a wide limit is interactive — bulk needs evidence."""
    _write(archive_home, [
        _warm_search(300.0, limit=10, page=1),
        _warm_search(280.0),                          # no limit/page recorded
        _warm_search(5000.0, limit=50, page=1),       # wide
        _warm_search(4000.0, limit=10, page=7),       # deep
    ])
    out = rr.served(archive_home)
    assert out["warm"]["n"] == 4
    assert out["warm_interactive"]["n"] == 2
    assert out["warm_interactive"]["p50"] == 300.0
    assert out["warm_bulk"]["n"] == 2
    assert out["warm_bulk"]["p50"] == 5000.0


def test_the_regime_is_what_a_search_paid_not_how_young_its_process_was(
        archive_home) -> None:
    """The proxy and the fact disagree in both directions, and the ledger carries
    the fact. A search on a five-second-old process that loaded nothing is warm — it
    had the caches, however it came by them; a search on a settled process that
    built the vector pack inline is cold, because it paid for it inside the request.
    Charging by process age instead gets both backwards, and sweeps in every search
    from the one-shot surfaces, whose processes are young by construction."""
    _write(archive_home, [
        _warm_search(200.0, uptime=5.0),
        _warm_search(9000.0, uptime=4000.0, matrix_built=True),
    ])
    out = rr.served(archive_home)
    assert out["warm"]["n"] == 1 and out["warm"]["p50"] == 200.0
    assert out["cold"]["n"] == 1 and out["cold"]["p50"] == 9000.0


def test_a_search_the_probe_never_measured_is_its_own_bucket(archive_home) -> None:
    """Neither regime, and counted — folding it into whichever is more flattering
    is the error that made the ledger unreadable. Process age is not evidence
    either way: what makes a row classifiable is the probe having measured it."""
    _write(archive_home, [_search(700.0, uptime=900.0), _warm_search(200.0)])
    out = rr.served(archive_home)
    assert out["n_unknown_regime"] == 1
    assert out["warm"]["n"] == 1 and out["cold"]["n"] == 0
    assert _filled(out)[0]["unknown"]["n"] == 1


def test_stages_exclude_the_known_cold_but_keep_the_unproven(archive_home) -> None:
    """Requiring proof of warmth would empty the chart for as long as the ledger's
    memory is older than the probe."""
    _write(archive_home, [
        _cold_search(9000.0, embed_ms=5000.0, fts_ms=100.0),
        _warm_search(300.0, embed_ms=20.0, fts_ms=250.0),
        _search(400.0, embed_ms=25.0, fts_ms=300.0),
    ])
    out = rr.stages(archive_home)
    assert out["n"] == 2 and out["n_unproven"] == 1
    embed = next(s for s in out["stages"] if s["stage"] == "embed_ms")
    assert embed["p50"] < 100.0  # the 5s cold load is not in here
    # Slowest first, so the chart's first row is the thing to look at.
    assert out["stages"][0]["p50"] >= out["stages"][-1]["p50"]


def test_each_front_door_is_counted_apart(archive_home) -> None:
    """The shared server warms once and serves every client off resident models; a
    stdio server and a CLI verb are one process per call and pay that load inside
    it. Pooled, the one-shot surfaces are the whole cold line and the page indicts a
    server that is behaving correctly."""
    _write(archive_home, [
        _warm_search(200.0, surface="mcp-http"),
        _warm_search(240.0, surface="mcp-http"),
        _cold_search(9000.0, surface="cli"),
    ])
    rows = {s["surface"]: s for s in rr.served(archive_home)["by_surface"]}
    assert rows["mcp-http"]["n"] == 2 and rows["mcp-http"]["n_cold"] == 0
    assert rows["cli"]["n"] == 1 and rows["cli"]["n_cold"] == 1
    # Volume first, so the door most searches came through leads the table.
    assert rr.served(archive_home)["by_surface"][0]["surface"] == "mcp-http"


def test_every_headline_number_is_available_per_door(archive_home) -> None:
    """The doors are the page's headline, so each one carries the same cuts the
    whole population does. A pooled median over doors this different is a mixture
    nobody waited on: here the warmed server's question costs 200 ms and the
    terminal's costs 9 s, and their average describes neither."""
    _write(archive_home, [
        _warm_search(200.0, surface="mcp-http"),
        _warm_search(5000.0, surface="mcp-http", limit=50),
        _cold_search(9000.0, surface="cli"),
    ])
    rows = {s["surface"]: s for s in rr.served(archive_home)["by_surface"]}
    http = rows["mcp-http"]
    assert http["warm_interactive"] == {"n": 1, "p50": 200.0, "p90": 200.0, "p99": 200.0}
    assert http["warm_bulk"]["n"] == 1 and http["warm_bulk"]["p50"] == 5000.0
    assert http["cold"]["n"] == 0
    # A door with nothing warm reports an empty band rather than borrowing the
    # pool's — its callers have never once had a warm process to ask.
    assert rows["cli"]["warm_interactive"]["n"] == 0
    assert rows["cli"]["cold"]["p50"] == 9000.0


def test_a_window_counts_only_the_window(archive_home) -> None:
    """The ledgers keep their whole history on purpose and the windows this page
    asks for are hours to weeks, so the read is bounded by the window rather than by
    the history behind it — walked backwards from now and stopped at the edge. A
    row older than the cutoff is neither counted nor read."""
    _write(archive_home, [
        _warm_search(9000.0, minutes_ago=60 * 24 * 9),
        _warm_search(200.0, minutes_ago=30),
    ])
    _web_write(archive_home, [
        _web_search(4000.0, minutes_ago=60 * 24 * 9),
        _web_search(500.0, minutes_ago=30),
    ])
    out = rr.served(archive_home, hours=24)
    assert out["n"] == 2
    assert out["warm"]["p50"] == 500.0 and out["warm"]["p90"] == 500.0


def test_the_viewer_is_a_door_like_any_other(archive_home) -> None:
    """Its searches live in their own ledger so that evals mined from observed
    traffic never learn from a human clicking around. Latency is not that question:
    the viewer drives the same engine, and a page about doors that read only one
    file would report the busiest surface on the box as silent."""
    _write(archive_home, [_warm_search(200.0, surface="mcp-http")])
    _web_write(archive_home, [
        _web_search(500.0, limit=40, page=1),
        _web_search(700.0, limit=40, page=1),
        {"at": _at(1), "path": "/api/status", "status": 200, "duration_ms": 3.0},
    ])
    rows = {s["surface"]: s for s in rr.served(archive_home)["by_surface"]}
    assert rows[rr.WEB]["n"] == 2
    assert rows[rr.WEB]["warm_interactive"]["p50"] == 700.0
    # A page load is not a search, however often it is served.
    assert rr.served(archive_home)["n"] == 3


def test_a_page_of_the_viewers_results_is_one_question_not_a_sweep(
        archive_home) -> None:
    """The boundary between a question and a sweep is a door's own first screen.
    The viewer cannot paint fewer than its page size, so asking for exactly that is
    the cheapest thing it ever does — while the same width from an agent that
    defaults to ten is a corpus walk. One number for both would file every search
    the viewer has ever run as bulk."""
    _write(archive_home, [_warm_search(5000.0, surface="mcp-http",
                                       limit=rr.WEB_INTERACTIVE_LIMIT)])
    _web_write(archive_home, [
        _web_search(500.0, limit=rr.WEB_INTERACTIVE_LIMIT, page=1),
        _web_search(4000.0, limit=rr.WEB_INTERACTIVE_LIMIT, page=6),
    ])
    rows = {s["surface"]: s for s in rr.served(archive_home)["by_surface"]}
    assert rows[rr.WEB]["warm_interactive"]["n"] == 1
    assert rows[rr.WEB]["warm_bulk"]["p50"] == 4000.0
    assert rows["mcp-http"]["warm_interactive"]["n"] == 0


def test_a_request_the_viewer_refused_never_reached_the_engine(archive_home) -> None:
    """A 4xx is a rejection, and its fast refusal would flatter every percentile it
    landed in. A 5xx is a search that failed slowly, which is the most interesting
    latency there is — it stays, for the same reason the tool ledger keeps its
    failures."""
    _web_write(archive_home, [
        _web_search(2.0, status=400),
        _web_search(30000.0, status=500),
    ])
    out = rr.served(archive_home)
    assert out["n"] == 1 and out["warm"]["p50"] == 30000.0


def test_a_row_from_before_the_doors_were_named_is_not_assigned_to_one(
        archive_home) -> None:
    """Every row predating the surface field looks like this, and resolving it to a
    door would invent an attribution — the exact error on the regime side, in the
    other column."""
    _write(archive_home, [_warm_search(200.0)])
    doors = rr.served(archive_home)["by_surface"]
    assert [(d["surface"], d["n"]) for d in doors] == [(rr.UNATTRIBUTED, 1)]


def test_restarts_are_attributed_to_the_daemon_that_paid_them(archive_home) -> None:
    """Several daemons warm independently and bounce for unrelated reasons, so the
    total answers how much warming the box did — not how often the service being
    read restarted, nor what a start costs *it*."""
    _write(archive_home, [
        {"at": _at(10), "kind": "warm", "duration_ms": 22000.0, "surface": "mcp-http"},
        {"at": _at(10), "kind": "warm", "duration_ms": 21000.0, "surface": "web"},
        {"at": _at(20), "kind": "warm", "duration_ms": 20000.0, "surface": "mcp-http"},
    ])
    out = rr.restarts(archive_home, hours=24)
    assert out["n"] == 3
    assert out["by_surface"] == [
        {"surface": "mcp-http", "n": 2, "p50_ms": 22000.0},
        {"surface": "web", "n": 1, "p50_ms": 21000.0},
    ]


def test_rebuilds_are_split_by_what_was_rebuilt(archive_home) -> None:
    """The matrix and the graph rebuild on unrelated triggers and unrelated
    schedules — one on ingest moving the store's validity token, the other on a
    stale partition — so a total over both tracks neither."""
    _write(archive_home, [
        {"at": _at(10), "kind": "refresh", "what": "matrix", "duration_ms": 4000.0},
        {"at": _at(12), "kind": "refresh", "what": "graph", "duration_ms": 9000.0},
        {"at": _at(20), "kind": "refresh", "what": "matrix", "duration_ms": 6000.0,
         "failed": True},
    ])
    out = rr.rebuilds(archive_home, hours=24)
    assert out["n"] == 3
    matrix, graph = out["by_what"]
    assert (matrix["what"], matrix["n"], matrix["failed"]) == ("matrix", 2, 1)
    assert matrix["max_ms"] == 6000.0 and matrix["total_s"] == 10.0
    assert (graph["what"], graph["n"]) == ("graph", 1)


def test_rebuilds_do_not_leak_into_the_served_distribution(archive_home) -> None:
    """A refresh is work between requests, not a request. Counting one as served
    latency would put a multi-second rebuild in a percentile no agent waited on."""
    _write(archive_home, [
        {"at": _at(10), "kind": "search", "query": "q", "duration_ms": 40.0},
        {"at": _at(11), "kind": "refresh", "what": "graph", "duration_ms": 9000.0},
    ])
    out = rr.served(archive_home, hours=24)
    assert out["n"] == 1


def test_a_failed_search_still_counts_toward_the_distribution(archive_home) -> None:
    """Dropping slow errors biases every percentile toward the searches that
    happened to succeed."""
    _write(archive_home, [_warm_search(30000.0, failed=True),
                          _warm_search(200.0)])
    assert rr.served(archive_home)["warm"]["n"] == 2


def test_bench_series_never_merge_two_query_sets(archive_home) -> None:
    stats = speed._summarize([{"total_ms": 900.0, "shape": "natural", "query": "q"}],
                             n_queries=1, reps=1)
    speed.record_run(archive_home, snapshot_id=None, stats=stats)
    speed.record_run(archive_home, snapshot_id=None, stats=stats,
                     query_set="curated")
    series = rr.bench(archive_home)
    assert set(series) == {speed.OBSERVED_SET, "curated"}
    assert len(series[speed.OBSERVED_SET]) == 1 and len(series["curated"]) == 1


def test_a_row_predating_the_query_set_field_is_not_folded_into_a_named_set(
        archive_home) -> None:
    # Guessing which population an unlabelled row measured is how two sets end up
    # averaged into one line; `unknown` is the honest bucket.
    (archive_home / speed.LATENCY_RUNS_FILE).write_text(json.dumps({
        "kind": "latency-run", "at": "2026-07-01T00:00:00Z",
        "total": {"p50": 900.0}}) + "\n")
    assert set(rr.bench(archive_home)) == {"unknown"}


def test_a_missing_ledger_yields_an_empty_section_not_a_failure(archive_home) -> None:
    """An operator view that goes blank when one input is absent is the least
    useful thing it could do."""
    out = rr.report(archive_home)
    assert out["served"]["n"] == 0
    assert out["stages"]["stages"] == []
    assert out["bench"] == {}


def test_report_carries_every_section(archive_home) -> None:
    _write(archive_home, [_warm_search(200.0)])
    out = rr.report(archive_home, hours=7 * 24)
    assert out["hours"] == 7 * 24
    for section in ("served", "stages", "restarts", "bench"):
        assert section in out


def test_a_short_window_is_bucketed_by_the_hour(archive_home) -> None:
    """A day's worth of searches at daily resolution is one point, which cannot say
    whether the slow patch was this morning or the restart at lunch."""
    _write(archive_home, [
        _warm_search(200.0, minutes_ago=10),
        _cold_search(9000.0, minutes_ago=200),
    ])
    out = rr.served(archive_home, hours=24)
    assert out["bucket"] == rr.HOUR
    assert len(_filled(out)) == 2, "two searches three hours apart are two buckets"
    assert rr.default_bucket(24) == rr.HOUR and rr.default_bucket(14 * 24) == rr.DAY


def test_the_span_keeps_its_empty_buckets(archive_home) -> None:
    """Emitting only the buckets that saw traffic compresses the axis onto the times
    something happened, and a line drawn over that joins 3am to noon as though the
    hours between were steady. A quiet stretch is a fact about the window."""
    _write(archive_home, [_warm_search(200.0)])
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
