"""The retrieval-usage ledger records MCP searches and reads, ids only."""

from __future__ import annotations

import json

from thread_archive import _api as ta
from thread_archive._mcp.server import thread_read, thread_search
from thread_archive._retrieval import usage

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello ledger"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from ledger"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _records(home):
    """The call rows — what this file is about.

    ``serve`` rows are dropped: they describe the MCP layer *around* a call rather
    than the call, and they appear only when that layer crosses its reporting
    floor, which is a property of how loaded the machine is. Reading them here
    would make every row-count assertion depend on the box. They have their own
    file (``test_serve_overhead.py``). Filtering by kind is what every real
    consumer of this ledger does anyway — ``warm`` rows have always been mixed in
    the same way."""
    return [r for r in _all_records(home) if r.get("kind") != "serve"]


def _all_records(home):
    path = home / usage.LEDGER_FILE
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_record_search_writes_ids_and_omits_none_params(archive_home) -> None:
    hits = [{"event_id": 7, "thread_id": 3, "snippet": "SECRET CONTENT"},
            {"event_id": "8", "thread_id": "3"}]
    usage.record_search("what did we decide", params={"limit": 10, "source": None},
                        hits=hits)
    (rec,) = _records(archive_home)
    assert rec["kind"] == "search"
    assert rec["query"] == "what did we decide"
    assert rec["limit"] == 10 and "source" not in rec
    assert rec["n_hits"] == 2
    assert rec["results"] == [[7, "3"], [8, "3"]]  # event id int, thread id string
    # ids only — never content
    assert "SECRET CONTENT" not in json.dumps(rec)


def test_record_search_tolerates_non_hit_shapes(archive_home) -> None:
    usage.record_search("q", params={}, hits="a rendered string")
    usage.record_search("q2", params={}, hits=[{"count": 5}, {"event_id": "x", "thread_id": 1}])
    recs = _records(archive_home)
    assert "n_hits" not in recs[0] and "results" not in recs[0]
    assert recs[1]["n_hits"] == 2 and "results" not in recs[1]


def test_record_read_int_and_uuid(archive_home) -> None:
    usage.record_read(42, params={"mode": "chat", "offset": 0, "around_event": None})
    usage.record_read("abc-uuid", params=None)
    int_rec, uuid_rec = _records(archive_home)
    assert int_rec == {"at": int_rec["at"], "kind": "read", "thread_id": 42, "mode": "chat"}
    assert uuid_rec["thread_id"] == "abc-uuid"


def test_duration_ms_recorded_when_given_and_omitted_when_not(archive_home) -> None:
    usage.record_search("q", params={}, hits=[], duration_ms=12.3456)
    usage.record_read(1, duration_ms=0.74)
    usage.record_read(2)
    search, read, bare = _records(archive_home)
    assert search["duration_ms"] == 12.3
    assert read["duration_ms"] == 0.7
    assert "duration_ms" not in bare


def test_timings_merged_when_given_and_absent_when_not(archive_home) -> None:
    usage.record_search("q", params={}, hits=[],
                        timings={"fts_ms": 12.3, "semantic_ms": 4.5,
                                 "pool_size": 198, "cold": True})
    usage.record_search("q2", params={}, hits=[])
    with_t, without_t = _records(archive_home)
    assert with_t["fts_ms"] == 12.3 and with_t["semantic_ms"] == 4.5
    assert with_t["pool_size"] == 198 and with_t["cold"] is True
    # A search with no probe breakdown records no stage fields (backward-compatible).
    assert "fts_ms" not in without_t and "pool_size" not in without_t


def test_mcp_search_records_stage_timings(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello ledger", limit=5)
    (rec,) = _records(archive_home)
    # The probe's per-stage breakdown rides every real MCP search: the fields are
    # present (a lexical-only box still records fts, semantic sits at 0).
    for field in ("fts_ms", "semantic_ms", "pool_size"):
        assert field in rec, field
    assert rec["fts_ms"] >= 0.0 and isinstance(rec["pool_size"], int)
    assert rec["duration_ms"] >= rec["fts_ms"]  # total covers the stage it contains


def test_render_is_measured_beside_retrieval_not_inside_it(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello ledger", limit=5)
    (rec,) = _records(archive_home)
    # Formatting hits into the text the agent reads is part of the agent's wait,
    # so it is recorded — but kept out of duration_ms, which keeps meaning
    # retrieval alone.
    assert "render_ms" in rec and rec["render_ms"] >= 0.0
    assert "failed" not in rec


def test_a_raising_search_is_still_recorded_with_the_time_it_burned(archive_home) -> None:
    import pytest

    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    # A real engine rejection through the front door: 'newest' is not a sort the
    # engine offers, and it raises from inside the timed span.
    with pytest.raises(ValueError):
        thread_search("hello ledger", limit=5, sort="newest")
    (rec,) = [r for r in _records(archive_home) if r["kind"] == "search"]
    # The failure is the point: dropping slow errors biases every percentile
    # computed off this file toward the searches that happened to succeed.
    assert rec["failed"] is True
    assert rec["duration_ms"] >= 0.0
    assert "render_ms" not in rec  # it never reached the render


def test_read_records_response_size(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    threads = ta.search("hello ledger", limit=1)
    thread_read(threads[0]["thread_id"])
    rec = [r for r in _records(archive_home) if r["kind"] == "read"][-1]
    # Latency without size is a distribution missing its main explanatory
    # variable — a read's cost tracks how much it materialized.
    assert rec["chars"] > 0


def test_contention_context_rides_search_and_read(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello ledger", limit=5)
    thread_read(ta.search("hello ledger")[0]["thread_id"])
    search, read = _records(archive_home)
    # The import just wrote the index, so the WAL is fresh — that is the
    # cross-process signal that makes this ledger joinable to the ingest side.
    assert search["wal_age_s"] >= 0.0
    assert read["wal_age_s"] >= 0.0
    # A lone call has no in-flight peers and no rebuild running: the fields are
    # omitted rather than recorded as nothing, so their presence carries signal.
    assert "inflight" not in search and "refreshing" not in search


def test_read_calls_replays_the_arguments_not_just_the_words(archive_home) -> None:
    """The ledger is the query set the latency replay runs, and the parameters are
    part of the cost — a browse ask and a bare one are different workloads."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello ledger", limit=5, group="browse")
    thread_search("hello ledger", limit=5)
    calls = usage.read_calls(archive_home)
    # Deduped on the whole call, not the text: same words, two workloads.
    assert len(calls) == 2
    assert all(q == "hello ledger" for q, _ in calls)
    assert any(kw.get("group") == "browse" for _, kw in calls)
    # Newest first, so a limit takes the current distribution not an archaeological one.
    assert calls[0][1].get("group") is None


def test_read_calls_drops_the_probes_a_bench_leaves_behind(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello ledger", limit=5)
    thread_search("x", limit=1)
    assert [q for q, _ in usage.read_calls(archive_home, exclude=("x",))] == ["hello ledger"]


def test_read_calls_on_an_archive_nobody_has_searched_is_empty(archive_home) -> None:
    """Nothing to replay is a young archive, not an error."""
    assert usage.read_calls(archive_home) == []


def test_contention_sample_is_empty_on_an_idle_machine(archive_home) -> None:
    from thread_archive._retrieval import _contention

    # No archive touched yet — no WAL to stat, nothing in flight, no rebuilds.
    # Uptime is the exception: no reading of it means "nothing to report".
    assert set(_contention.sample()) == {"uptime_s"}


def test_every_search_says_how_old_the_process_serving_it_was(archive_home) -> None:
    """Every cache retrieval leans on is process-local, so the same query costs an
    order of magnitude more at second five than at second five hundred. A ledger
    that cannot tell those apart cannot answer whether a change helped — it compares
    cache states and calls the result a measurement."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello ledger", limit=5)
    thread_read(ta.search("hello ledger")[0]["thread_id"])
    search, read = _records(archive_home)
    assert search["uptime_s"] >= 0.0 and read["uptime_s"] >= 0.0
    # Recorded as a duration, not a cold/warm verdict: ``at - uptime_s`` is the
    # process's start, so it doubles as the identity that groups a process's
    # records — and the threshold for "cold" is chosen when the question is asked.
    assert read["uptime_s"] >= search["uptime_s"]


def test_in_flight_counts_the_caller_itself(archive_home) -> None:
    from thread_archive._retrieval import _contention

    with _contention.in_flight() as outer:
        # One call is not contention, so it stays out of the record...
        assert "inflight" not in _contention.peak_inflight(outer)
        with _contention.in_flight() as inner:
            # ...but a second concurrent call is exactly what the field is for.
            assert _contention.peak_inflight(inner)["inflight"] == 2
    # A span with no peers ever reports nothing, whenever it is read.
    assert _contention.peak_inflight(None) == {}


def test_a_peer_arriving_mid_call_still_counts_against_the_call_it_slowed(
    archive_home,
) -> None:
    """The defect this field had for its whole life: sampled once at the start, it
    could not see the peers that arrived during the seconds a search actually ran —
    which is every peer that matters. Recorded traffic showed it: plenty of provably
    overlapping calls, and the field had never fired once."""
    from thread_archive._retrieval import _contention

    with _contention.in_flight() as first:
        # Nothing yet — this call started alone, and a start-of-work sample is
        # exactly what would stop here and record an idle machine.
        assert _contention.peak_inflight(first) == {}
        with _contention.in_flight() as second:
            pass
        # The peer came and went entirely inside `first`. It is gone by the time
        # `first` is read, and the high-water mark is what remembers it.
        assert _contention.peak_inflight(first)["inflight"] == 2
        # The peer's own span saw the same crowd, from the other side.
        assert _contention.peak_inflight(second)["inflight"] == 2

    # The counter unwinds: a span opened after the crowd has cleared sees none of it.
    with _contention.in_flight() as later:
        assert _contention.peak_inflight(later) == {}


def test_concurrency_is_not_among_the_start_of_work_facts(archive_home) -> None:
    # The two are taken at opposite ends for opposite reasons — a rebuild that
    # finished mid-search still shaped it, while a peer that arrived mid-search is
    # only knowable at the end. Keeping them in one call would force one of the two
    # to be read at the wrong moment.
    from thread_archive._retrieval import _contention

    with _contention.in_flight(), _contention.in_flight():
        assert "inflight" not in _contention.sample()


def test_record_warm_names_the_startup_cost(archive_home) -> None:
    usage.record_warm(duration_ms=21000.0,
                      stages={"embed_ms": 20000.0, "search_ms": 600.0},
                      failed=["graph"])
    (rec,) = _records(archive_home)
    assert rec["kind"] == "warm"
    assert rec["duration_ms"] == 21000.0 and rec["embed_ms"] == 20000.0
    assert rec["failed_stages"] == ["graph"]


def test_usage_log_disabled_by_env(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_USAGE_LOG", "0")
    usage.record_search("q", params={}, hits=[])
    usage.record_read(1)
    assert _records(archive_home) == []


def test_usage_log_rotates_at_cap_without_losing_history(archive_home, monkeypatch) -> None:
    from thread_archive._ops import ledger

    monkeypatch.setenv("THREAD_ARCHIVE_USAGE_MAX_BYTES", "200")
    for i in range(20):
        usage.record_read(i)

    path = archive_home / usage.LEDGER_FILE
    segments = ledger.segments(path)
    assert len(segments) > 1, "the cap must actually have rotated"
    # The live file restarted, and nothing that was written is gone: the cap
    # bounds what one read walks, never what the archive remembers.
    live = _records(archive_home)
    everything = list(ledger.iter_rows(path))
    assert live and len(everything) == 20
    assert [r["thread_id"] for r in everything] == list(range(20))
    assert all(r["kind"] == "read" for r in everything)


def test_usage_write_failure_is_fail_soft(archive_home) -> None:
    # A real unwritable ledger path: the name the appender opens is a directory,
    # so the append raises for real (IsADirectoryError) inside the product.
    blocked = archive_home / usage.LEDGER_FILE
    blocked.mkdir()

    usage.record_search("q", params={}, hits=[])  # must not raise
    usage.record_read(1)  # nor the second time

    assert blocked.is_dir() and not any(blocked.iterdir()), "nothing was written"


def test_mcp_tools_feed_the_ledger(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    out = thread_search("hello ledger", limit=5)
    assert "hello ledger" in out
    thread_id = ta.search("hello ledger")[0]["thread_id"]
    thread_read(thread_id, mode="chat")

    recs = _records(archive_home)
    kinds = [r["kind"] for r in recs]
    assert kinds == ["search", "read"]
    search, read = recs
    assert search["query"] == "hello ledger"
    assert search["n_hits"] >= 1
    assert [search["results"][0][1]] == [thread_id]
    assert read["thread_id"] == thread_id and read["mode"] == "chat"
    # Both tools time their retrieval work into the record.
    assert search["duration_ms"] > 0
    assert read["duration_ms"] > 0


def test_count_output_records_params_without_ids(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello", output="count")
    (rec,) = _records(archive_home)
    assert rec["output"] == "count"
    assert "query" in rec
