"""The token/cost rollup (_store._metrics) + the /api/stats survey it powers.

Cost and token counts live inside ``api_request_completed`` payloads. These tests seed
those events straight into the index — the live importer doesn't synthesize cost, and
a raw core insert also skips the JSONL truth drain, keeping the fixture to the one
concern under test — then assert the incremental fold, the aggregation, and the
reindex-shrink self-heal.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

# The /api/stats survey is read through the viewer's router, which is dev-only
# and ships in no wheel — see docs/public/web-viewer.md.
pytest.importorskip("thread_archive._web", reason="the viewer is dev-only (no wheel carries it)")

from thread_archive import _api as ta  # noqa: E402
from thread_archive._web import route  # noqa: E402


def _seed_events(archive_home, rows):
    """Insert conversation threads + ``api_request_completed`` events.

    ``rows`` is a list of ``(thread_id, source, model, input_tokens, output_tokens,
    cost[, cache_read_tokens])`` — ``cost=None`` writes a payload with no cost field
    (a subscription tool), a number writes it (a pay-per-token source).
    """
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    with get_engine().begin() as c:
        for tid, source, *_ in rows:
            c.execute(
                text(
                    "INSERT OR IGNORE INTO threads (id, name, thread_type, source, archived) "
                    "VALUES (:id, :name, 'conversation', :source, 0)"
                ),
                {"id": tid, "name": f"thread-{tid}", "source": source},
            )
        for j, row in enumerate(rows):
            tid, _source, model, itok, otok, cost, *extras = row
            payload = {"model": model, "input_tokens": itok, "output_tokens": otok, "thinking_tokens": 0}
            if extras:
                payload["cache_read_tokens"] = extras[0]
            if cost is not None:
                payload["cost"] = cost
            c.execute(
                text(
                    "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                    "VALUES (:tid, :sid, 'api_request_completed', :payload, :ts)"
                ),
                {"tid": tid, "sid": f"s{j}", "payload": json.dumps(payload), "ts": f"2026-05-{(j % 28) + 1:02d}T10:00:00Z"},
            )


def _get(path, **params):
    qp = {k: [str(v)] for k, v in params.items()}
    status, ctype, body, _ = route("GET", path, qp)
    return status, (json.loads(body) if ctype.startswith("application/json") else body)


def test_stats_empty_archive(archive_home):
    ta.open_archive(str(archive_home))
    status, payload = _get("/api/stats")
    assert status == 200
    assert payload["overview"]["conversations"] == 0
    assert payload["by_source"] == []
    assert payload["by_model"] == []


def test_stats_tokens_and_cost_by_source(archive_home):
    _seed_events(
        archive_home,
        [
            (1, "demo-harness", "deepseek/deepseek-v4-pro", 1000, 100, 0.05),
            (1, "demo-harness", "deepseek/deepseek-v4-pro", 2000, 200, 0.10),
            (2, "demo-harness", "x-ai/grok-4.5", 500, 50, 0.02),
            (3, "claude-code", "claude-opus-4-8", 3000, 300, None, 9000),  # cache, no cost
            (4, "demo-harness", "<synthetic>", 10, 1, None),  # placeholder model
        ],
    )
    status, payload = _get("/api/stats")
    assert status == 200

    o = payload["overview"]
    assert o["conversations"] == 4
    assert o["tokens"] == 1000 + 100 + 2000 + 200 + 500 + 50 + 3000 + 300 + 10 + 1
    assert abs(o["cost"] - 0.17) < 1e-9
    assert o["cost_conversations"] == 2  # threads 1 and 2 carried cost
    # The true distinct-model count (deepseek, grok, opus), NOT the length of a capped
    # by-model list, and not counting the '<synthetic>' placeholder.
    assert o["models"] == 3

    by_source = {r["source"]: r for r in payload["by_source"]}
    assert by_source["demo-harness"]["conversations"] == 3
    assert abs(by_source["demo-harness"]["cost"] - 0.17) < 1e-9
    assert abs(by_source["demo-harness"]["avg_cost"] - 0.085) < 1e-9  # over the 2 cost-bearing sessions
    # A subscription source: tokens present, cost absent (null, not a fabricated 0).
    assert by_source["claude-code"]["cost"] is None
    assert by_source["claude-code"]["tokens"] == 3300
    assert by_source["claude-code"]["cache_read_tokens"] == 9000
    assert o["cache_read_tokens"] == 9000

    by_model = {r["model"]: r for r in payload["by_model"]}
    assert by_model["deepseek/deepseek-v4-pro"]["requests"] == 2
    assert abs(by_model["deepseek/deepseek-v4-pro"]["cost"] - 0.15) < 1e-9
    assert by_model["claude-opus-4-8"]["cost"] is None
    assert by_model["claude-opus-4-8"]["cache_read_tokens"] == 9000
    assert "<synthetic>" not in by_model  # placeholder models are dropped from the model list
    # No default cap: even the lowest-volume model (grok, a single request) is listed.
    assert "x-ai/grok-4.5" in by_model


def test_by_model_lists_every_model_uncapped(archive_home):
    # 25 distinct models, each a single request — none may be silently dropped, and the
    # overview count must reflect all of them.
    _seed_events(archive_home, [(1, "demo-harness", f"m{i:02d}", 10, 1, None) for i in range(25)])
    _status, payload = _get("/api/stats")
    assert payload["overview"]["models"] == 25
    assert len(payload["by_model"]) == 25
    # An explicit ?models=N still caps for a caller that wants it.
    _status, capped = _get("/api/stats", models=5)
    assert len(capped["by_model"]) == 5
    assert capped["overview"]["models"] == 25  # the count stays true even when the list is capped


def test_refresh_is_incremental(archive_home):
    _seed_events(archive_home, [(1, "demo-harness", "m1", 100, 10, 0.01)])
    assert ta.stats()["overview"]["tokens"] == 110

    # More events on the same thread — the next survey folds only the delta and
    # accumulates onto the standing sums rather than recomputing from scratch.
    _seed_events(archive_home, [(1, "demo-harness", "m1", 200, 20, 0.02)])
    s2 = ta.stats()
    assert s2["overview"]["tokens"] == 330
    assert abs(s2["overview"]["cost"] - 0.03) < 1e-9


def test_claude_cache_alias_is_deduplicated_across_incremental_folds(archive_home):
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    tid = "01CLAUDECACHE0000000000001"
    with get_engine().begin() as c:
        c.execute(
            text(
                "INSERT INTO threads (id, name, thread_type, source, archived) "
                "VALUES (:id, 'claude-cache', 'conversation', 'claude-code', 0)"
            ),
            {"id": tid},
        )

    def add_response(event_suffix: str, message_id: str, cache: int) -> None:
        payload = {
            "model": "claude-opus-4-8",
            "input_tokens": 1,
            "output_tokens": 1,
            # Anthropic's native spelling, repeated on every content-block row.
            "cache_read_tokens": 0,
            "cache_read_input_tokens": cache,
            "annotations": {"message_id": message_id},
        }
        with get_engine().begin() as c:
            c.execute(
                text(
                    "INSERT INTO events "
                    "(thread_id, stream_id, event_type, payload, occurred_at) "
                    "VALUES (:tid, :sid, 'api_request_completed', :payload, :ts)"
                ),
                {
                    "tid": tid,
                    "sid": f"stream-{event_suffix}",
                    "payload": json.dumps(payload),
                    "ts": f"2026-06-01T10:00:0{event_suffix}Z",
                },
            )

    add_response("1", "msg-same-response", 9000)
    assert ta.stats()["overview"]["cache_read_tokens"] == 9000

    # The duplicate lands after the first stats fold, proving the request key
    # deduplicates across watcher polls rather than only within one SQL batch.
    add_response("2", "msg-same-response", 9000)
    assert ta.stats()["overview"]["cache_read_tokens"] == 9000

    add_response("3", "msg-next-response", 7000)
    assert ta.stats()["overview"]["cache_read_tokens"] == 16000

    # Every other figure is deduplicated by the same request identity, not just the
    # cached-read one: three events, two responses, two requests' worth of tokens.
    payload = ta.stats()
    by_model = {r["model"]: r for r in payload["by_model"]}
    assert by_model["claude-opus-4-8"]["requests"] == 2
    assert payload["overview"]["tokens"] == 4  # 2 responses x (1 in + 1 out)


def test_repeated_usage_object_does_not_multiply_any_column(archive_home):
    """Claude Code emits one response's usage across several transcript rows. Summing
    those rows counts the response once per row — the defect this ledger exists to
    stop, and it has to hold for every column, not only the cached-read one."""
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    tid = "01CLAUDEDUPE0000000000001"
    with get_engine().begin() as c:
        c.execute(
            text(
                "INSERT INTO threads (id, name, thread_type, source, archived) "
                "VALUES (:id, 'dupes', 'conversation', 'claude-code', 0)"
            ),
            {"id": tid},
        )

    def emit(suffix, message_id, *, inp, out, think, cache, cost):
        payload = {
            "model": "claude-opus-4-8",
            "input_tokens": inp,
            "output_tokens": out,
            "thinking_tokens": think,
            "cache_read_input_tokens": cache,
            "cost": cost,
            "annotations": {"message_id": message_id},
        }
        with get_engine().begin() as c:
            c.execute(
                text(
                    "INSERT INTO events "
                    "(thread_id, stream_id, event_type, payload, occurred_at) "
                    "VALUES (:tid, :sid, 'api_request_completed', :p, :ts)"
                ),
                {"tid": tid, "sid": f"s{suffix}", "p": json.dumps(payload),
                 "ts": f"2026-06-02T10:00:{int(suffix):02d}Z"},
            )

    # One response, three transcript rows repeating its usage verbatim.
    for i in range(3):
        emit(i, "msg-a", inp=5000, out=400, think=100, cache=20000, cost=0.25)
    # A second, genuinely distinct response.
    emit(3, "msg-b", inp=6000, out=500, think=0, cache=21000, cost=0.30)

    payload = ta.stats()
    row = {r["model"]: r for r in payload["by_model"]}["claude-opus-4-8"]
    assert row["requests"] == 2  # responses, not transcript rows
    assert row["input_tokens"] == 11000
    assert row["output_tokens"] == 900
    assert row["cache_read_tokens"] == 41000
    assert abs(row["cost"] - 0.55) < 1e-9
    assert payload["overview"]["tokens"] == 11900


def test_amended_events_survive_a_partial_usage_row(archive_home):
    """A response's counts can arrive partial and be completed by a later row. The
    canonical value is the largest seen, so the final total wins over the partial —
    and a duplicate that omits cost entirely must not erase the cost already held."""
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    tid = "01CLAUDEPARTIAL000000001"
    with get_engine().begin() as c:
        c.execute(
            text(
                "INSERT INTO threads (id, name, thread_type, source, archived) "
                "VALUES (:id, 'partial', 'conversation', 'claude-code', 0)"
            ),
            {"id": tid},
        )

    def emit(suffix, payload):
        payload = {"model": "m", "annotations": {"message_id": "msg-a"}, **payload}
        with get_engine().begin() as c:
            c.execute(
                text(
                    "INSERT INTO events "
                    "(thread_id, stream_id, event_type, payload, occurred_at) "
                    "VALUES (:tid, :sid, 'api_request_completed', :p, :ts)"
                ),
                {"tid": tid, "sid": f"s{suffix}", "p": json.dumps(payload),
                 "ts": f"2026-06-03T10:00:{suffix:02d}Z"},
            )

    emit(0, {"input_tokens": 100, "output_tokens": 5, "cost": 0.02})
    assert ta.stats()["overview"]["tokens"] == 105

    # A later poll carries the completed counts and no cost field at all.
    emit(1, {"input_tokens": 100, "output_tokens": 40})
    payload = ta.stats()
    assert payload["overview"]["tokens"] == 140  # the finished count, still one request
    row = {r["model"]: r for r in payload["by_model"]}["m"]
    assert row["requests"] == 1
    assert abs(row["cost"] - 0.02) < 1e-9  # not erased by the cost-less duplicate


def test_legacy_codex_input_is_split_from_cached_reads(archive_home):
    # Legacy Codex events carry provider-native input inclusive of cache and no
    # semantics marker. The rollup normalizes those without rewriting history.
    _seed_events(
        archive_home,
        [(1, "codex", "gpt-5.6-sol", 100, 10, None, 80)],
    )
    payload = ta.stats()
    codex = next(row for row in payload["by_source"] if row["source"] == "codex")
    assert codex["input_tokens"] == 20
    assert codex["cache_read_tokens"] == 80
    assert codex["output_tokens"] == 10
    assert codex["tokens"] == 30
    assert payload["overview"]["tokens"] == 30


# Fixed ULID thread ids for the per-model drill-down fixture (index 0 unused).
STATS_TIDS = [None] + [f"01STATSTEST0000000000000{i:02d}" for i in range(1, 4)]


def _seed_model_detail(archive_home):
    """Fixture for the per-model drill-down: two 'mx' sessions in different months
    (one mixed-model, one cost-bearing), a third session that never used 'mx', and
    ``context_summary`` (compaction) events — including one landing in a month after
    its session started, and one in the non-mx session that must not be attributed.
    """
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    with get_engine().begin() as c:
        for n, source, at in [
            (1, "demo-harness", "2026-01-05 10:00:00"),
            (2, "claude-code", "2026-01-20 09:00:00"),
            (3, "claude-code", "2026-02-02 08:00:00"),
        ]:
            c.execute(
                text(
                    "INSERT INTO threads (id, name, title, thread_type, source, archived, inserted_at) "
                    "VALUES (:id, :name, :title, 'conversation', :source, 0, :at)"
                ),
                {"id": STATS_TIDS[n], "name": f"thread-{n}", "title": f"session {n}",
                 "source": source, "at": at},
            )
        completions = [
            (STATS_TIDS[1], "mx", 100, 10, 0.01),
            (STATS_TIDS[1], "mx", 200, 20, None),
            (STATS_TIDS[2], "mx", 1000, 100, None, 9000),
            (STATS_TIDS[2], "my", 5000, 500, None),  # another model's share of the mixed session
            (STATS_TIDS[3], "my", 70, 7, None),  # a session mx never touched
        ]
        for j, completion in enumerate(completions):
            tid, model, itok, otok, cost, *extras = completion
            payload = {"model": model, "input_tokens": itok, "output_tokens": otok, "thinking_tokens": 0}
            if extras:
                payload["cache_read_tokens"] = extras[0]
            if cost is not None:
                payload["cost"] = cost
            c.execute(
                text(
                    "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                    "VALUES (:tid, :sid, 'api_request_completed', :payload, :ts)"
                ),
                {"tid": tid, "sid": f"s{j}", "payload": json.dumps(payload), "ts": "2026-01-05T10:00:00Z"},
            )
        for k, (tid, ts) in enumerate([
            (STATS_TIDS[1], "2026-01-05T12:00:00Z"),
            (STATS_TIDS[2], "2026-01-20T11:00:00Z"),
            (STATS_TIDS[2], "2026-02-01T09:00:00Z"),  # session started in Jan, compacted again in Feb
            (STATS_TIDS[3], "2026-02-02T09:00:00Z"),  # non-mx session: never attributed to mx
        ]):
            c.execute(
                text(
                    "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                    "VALUES (:tid, :sid, 'context_summary', '{}', :ts)"
                ),
                {"tid": tid, "sid": f"c{k}", "ts": ts},
            )


def test_model_stats_detail(archive_home):
    _seed_model_detail(archive_home)
    status, payload = _get("/api/stats/model/mx")
    assert status == 200
    assert payload["model"] == "mx"

    o = payload["overview"]
    assert o["conversations"] == 2  # threads 1 and 2 — thread 3 never used mx
    assert o["requests"] == 3
    assert o["tokens"] == 100 + 10 + 200 + 20 + 1000 + 100  # mx's share only, not my's
    assert o["cache_read_tokens"] == 9000
    assert abs(o["cost"] - 0.01) < 1e-9
    assert o["cost_conversations"] == 1
    assert o["compactions"] == 3  # thread 3's compaction is not mx's
    assert o["first_at"].startswith("2026-01-05")
    assert o["last_at"].startswith("2026-01-20")

    p = payload["per_session"]
    assert p["min_tokens"] == 330
    assert p["max_tokens"] == 1100
    assert p["avg_tokens"] == (330 + 1100) / 2
    assert p["median_tokens"] == (330 + 1100) / 2
    assert p["avg_requests"] == 1.5

    months = {m["month"]: m for m in payload["by_month"]}
    assert list(months) == ["2026-01", "2026-02"]  # sorted
    jan = months["2026-01"]
    assert jan["sessions"] == 2 and jan["tokens"] == 1430 and jan["compactions"] == 2
    assert jan["cache_read_tokens"] == 9000
    # February has no new mx session, but a January session compacted there — the
    # activity keeps a row rather than vanishing.
    feb = months["2026-02"]
    assert feb["sessions"] == 0 and feb["tokens"] == 0 and feb["compactions"] == 1
    assert feb["cost"] is None and feb["avg_tokens"] is None

    top = payload["top_sessions"]
    assert [t["thread_id"] for t in top] == [STATS_TIDS[2], STATS_TIDS[1]]  # heaviest mx share first
    assert top[0]["tokens"] == 1100 and top[0]["compactions"] == 2
    assert top[0]["cache_read_tokens"] == 9000
    assert top[0]["title"] == "session 2" and top[0]["source"] == "claude-code"


def test_model_stats_unknown_model_404s(archive_home):
    _seed_model_detail(archive_home)
    status, payload = _get("/api/stats/model/never-used")
    assert status == 404
    assert "never-used" in payload["error"]
    # A placeholder id has rollup rows but is not a real model page either way —
    # it simply has no thread_metrics rows under that name here.
    status, _ = _get("/api/stats/model/")
    assert status == 404


def test_model_stats_slash_in_model_name(archive_home):
    # Router models like 'deepseek/deepseek-v4-pro' contain a slash; the route takes
    # the whole tail, so both the SPA's percent-encoded form and a hand-typed literal
    # slash resolve.
    _seed_events(archive_home, [(1, "demo-harness", "deepseek/deepseek-v4-pro", 100, 10, 0.01)])
    for path in ("/api/stats/model/deepseek%2Fdeepseek-v4-pro", "/api/stats/model/deepseek/deepseek-v4-pro"):
        status, payload = _get(path)
        assert status == 200, path
        assert payload["model"] == "deepseek/deepseek-v4-pro"
        assert payload["overview"]["conversations"] == 1


def test_refresh_resets_when_log_shrinks(archive_home):
    _seed_events(archive_home, [(1, "demo-harness", "m1", 100, 10, 0.01)])
    ta.stats()

    # Simulate a reindex that rebuilt the log below the cursor, and corrupt the rollup
    # to prove the reset genuinely recomputes rather than trusting the stale sums.
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    with get_engine().begin() as c:
        c.execute(text("UPDATE metrics_cursor SET through_event_id = 999999"))
        c.execute(text("UPDATE thread_metrics SET input_tokens = 999999"))

    assert ta.stats()["overview"]["input_tokens"] == 100  # rebuilt from the real events


def test_median_of_empty_is_none():
    from thread_archive._store._metrics import _median

    assert _median([]) is None
    assert _median([3]) == 3.0  # odd → middle value
    assert _median([1, 3]) == 2.0  # even → mean of the middle pair


def test_model_stats_tolerates_blank_timestamps(archive_home):
    """A thread with a blank inserted_at (and a compaction event with a blank
    occurred_at — legacy/repair artifacts) still gets a session row — it just
    contributes to no month bucket instead of crashing the drill-down."""
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine
    from thread_archive._store._metrics import collect_model_stats

    tid = "01STATSNULL000000000000001"
    with get_engine().begin() as c:
        c.execute(
            text(
                "INSERT INTO threads (id, name, thread_type, source, archived, inserted_at) "
                "VALUES (:id, 'null-times', 'conversation', 'demo-harness', 0, '')"
            ),
            {"id": tid},
        )
        c.execute(
            text(
                "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                "VALUES (:tid, 's0', 'api_request_completed', :p, '')"
            ),
            {"tid": tid, "p": json.dumps({"model": "mz", "input_tokens": 10,
                                          "output_tokens": 1, "thinking_tokens": 0})},
        )
        c.execute(
            text(
                "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                "VALUES (:tid, 'c0', 'context_summary', '{}', '')"
            ),
            {"tid": tid},
        )

    d = collect_model_stats("mz")
    assert d is not None
    assert d["overview"]["conversations"] == 1
    assert d["overview"]["first_at"] is None  # no timestamps anywhere
    assert d["by_month"] == []  # month-less session and compaction bucket nowhere
    assert d["overview"]["compactions"] == 1  # still counted per-thread
    assert d["top_sessions"][0]["at"] is None


# ── the charts' sections: timeline, session sizes, rhythm ───────────────────────


def _seed_dated(archive_home, rows):
    """Insert threads + events at chosen timestamps.

    ``rows`` is ``(thread_id, source, event_type, occurred_at, payload | None)``. A
    payload of None writes a bare message event — the shape a source that records no
    token usage produces, and the case the timeline must still be able to date.
    """
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    with get_engine().begin() as c:
        for tid, source, *_ in rows:
            c.execute(
                text(
                    "INSERT OR IGNORE INTO threads (id, name, thread_type, source, archived) "
                    "VALUES (:id, :name, 'conversation', :source, 0)"
                ),
                {"id": tid, "name": f"thread-{tid}", "source": source},
            )
        for j, (tid, _source, etype, ts, payload) in enumerate(rows):
            c.execute(
                text(
                    "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                    "VALUES (:tid, :sid, :etype, :payload, :ts)"
                ),
                {"tid": tid, "sid": f"d{j}", "etype": etype,
                 "payload": json.dumps(payload or {}), "ts": ts},
            )


def _usage(model, itok, otok):
    return {"model": model, "input_tokens": itok, "output_tokens": otok, "thinking_tokens": 0}


def test_timeline_axis_is_dense_across_quiet_months(archive_home):
    """Months nobody used are drawn, not closed up: the gap is the data."""
    _seed_dated(
        archive_home,
        [
            (1, "chatgpt", "api_request_completed", "2026-01-04T10:00:00Z", _usage("m1", 100, 10)),
            (2, "chatgpt", "api_request_completed", "2026-04-09T10:00:00Z", _usage("m1", 200, 20)),
        ],
    )
    t = ta.stats()["timeline"]
    assert t["months"] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert t["conversations"] == [1, 0, 0, 1]
    assert t["tokens"] == [110, 0, 0, 220]


def test_timeline_dates_a_source_that_records_no_tokens(archive_home):
    """A web export carries messages and no usage at all. It has to appear on the
    conversation axis anyway — dropping it erases the archive's early history."""
    _seed_dated(
        archive_home,
        [
            (1, "chatgpt", "message", "2024-03-02T10:00:00Z", None),
            (2, "claude-code", "api_request_completed", "2024-04-02T10:00:00Z", _usage("m1", 500, 50)),
        ],
    )
    t = ta.stats()["timeline"]
    assert t["months"] == ["2024-03", "2024-04"]
    assert t["conversations"] == [1, 1]
    by_source = {s["key"]: s["values"] for s in t["conversations_by_source"]}
    assert by_source["chatgpt"] == [1, 0]
    assert by_source["claude-code"] == [0, 1]
    # …while the token series knows only the month that actually spent any.
    assert t["tokens"] == [0, 550]


def test_timeline_buckets_conversations_by_start_and_tokens_by_request(archive_home):
    """One session running across a month boundary starts once but spends in both."""
    _seed_dated(
        archive_home,
        [
            (1, "claude-code", "api_request_completed", "2026-01-30T10:00:00Z", _usage("m1", 100, 10)),
            (1, "claude-code", "api_request_completed", "2026-02-02T10:00:00Z", _usage("m1", 300, 30)),
        ],
    )
    t = ta.stats()["timeline"]
    assert t["conversations"] == [1, 0]  # counted where it began
    assert t["tokens"] == [110, 330]  # spent where each request happened


def test_timeline_folds_the_tail_into_other(archive_home):
    """Past the palette's slots the tail folds rather than minting more colors, and it
    folds by total — never by a month's own ranking, which would repaint a series."""
    from thread_archive._store._metrics import TIMELINE_SOURCES

    n_sources = TIMELINE_SOURCES + 3
    rows = []
    for i in range(n_sources):
        # Source i gets i+1 conversations, so the three smallest are the ones folded.
        for j in range(i + 1):
            rows.append(
                (f"{i}-{j}", f"src{i:02d}", "api_request_completed",
                 "2026-05-04T10:00:00Z", _usage("m1", 10, 1))
            )
    _seed_dated(archive_home, rows)
    series = ta.stats()["timeline"]["conversations_by_source"]
    biggest_first = [f"src{i:02d}" for i in range(n_sources - 1, n_sources - 1 - TIMELINE_SOURCES, -1)]
    assert [s["key"] for s in series] == biggest_first + ["other"]
    assert series[-1]["values"] == [1 + 2 + 3]  # the three smallest sources, summed


def test_timeline_reports_conversations_it_cannot_date(archive_home):
    """A thread with no events has no month. It stays in the overview count and is
    reported here, so the chart's total can be seen not to match rather than quietly
    disagreeing."""
    _seed_dated(
        archive_home,
        [(1, "claude-code", "api_request_completed", "2026-05-04T10:00:00Z", _usage("m1", 10, 1))],
    )
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    with get_engine().begin() as c:
        c.execute(
            text(
                "INSERT INTO threads (id, name, thread_type, source, archived) "
                "VALUES ('ghost', 'ghost', 'conversation', 'claude-code', 0)"
            )
        )
    stats = ta.stats()
    assert stats["overview"]["conversations"] == 2
    assert sum(stats["timeline"]["conversations"]) == 1
    assert stats["timeline"]["undated"] == 1


def test_session_sizes_bin_by_magnitude_and_exclude_usage_less_sessions(archive_home):
    _seed_dated(
        archive_home,
        [
            (1, "claude-code", "api_request_completed", "2026-05-04T10:00:00Z", _usage("m1", 400, 100)),  # 500
            (2, "claude-code", "api_request_completed", "2026-05-04T10:00:00Z", _usage("m1", 6000, 500)),  # 6.5k
            (3, "claude-code", "api_request_completed", "2026-05-04T10:00:00Z", _usage("m1", 2_000_000, 1)),
            (4, "chatgpt", "message", "2026-05-04T10:00:00Z", None),  # no usage at all
        ],
    )
    sizes = ta.stats()["session_sizes"]
    assert sizes["sessions"] == 3
    assert sizes["without_tokens"] == 1  # counted, never binned as a zero-token session
    counts = {(b["lo"], b["hi"]): b["count"] for b in sizes["buckets"]}
    assert counts[(0, 1_000)] == 1
    assert counts[(5_000, 10_000)] == 1
    assert counts[(1_000_000, None)] == 1  # the top bucket is open-ended
    assert sizes["median"] == 6500


def test_rhythm_is_a_monday_first_weekday_hour_grid(archive_home):
    """Local time, because 'when do I work' is only meaningful in the operator's own
    frame — so the grid is asserted by its totals, not by a cell a test machine's
    timezone would move."""
    _seed_dated(
        archive_home,
        [
            (1, "claude-code", "api_request_completed", "2026-05-04T15:00:00Z", _usage("m1", 10, 1)),
            (2, "claude-code", "api_request_completed", "2026-05-06T15:00:00Z", _usage("m1", 10, 1)),
            (3, "chatgpt", "message", "2026-05-07T15:00:00Z", None),
        ],
    )
    r = ta.stats()["rhythm"]
    assert len(r["grid"]) == 7 and all(len(row) == 24 for row in r["grid"])
    assert r["total"] == 3
    assert r["max"] == 1
    assert sum(sum(row) for row in r["grid"]) == r["total"]


def test_empty_archive_has_empty_chart_sections(archive_home):
    ta.open_archive(str(archive_home))
    _status, payload = _get("/api/stats")
    assert payload["timeline"]["months"] == []
    assert payload["timeline"]["conversations_by_source"] == []
    assert payload["session_sizes"]["sessions"] == 0
    assert payload["rhythm"]["total"] == 0
    assert payload["rhythm"]["max"] == 0


def test_thread_activity_accumulates_across_incremental_folds(archive_home):
    """The bounds are min/max against what is already stored, so a later poll widens the
    span rather than replacing it — and re-folding cannot move it."""
    _seed_dated(
        archive_home,
        [(1, "claude-code", "api_request_completed", "2026-03-10T10:00:00Z", _usage("m1", 10, 1))],
    )
    assert ta.stats()["timeline"]["months"] == ["2026-03"]

    _seed_dated(
        archive_home,
        [(1, "claude-code", "api_request_completed", "2026-05-10T10:00:00Z", _usage("m1", 10, 1))],
    )
    t = ta.stats()["timeline"]
    assert t["months"] == ["2026-03", "2026-04", "2026-05"]
    assert t["conversations"] == [1, 0, 0]  # still one session, still dated where it began

    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    with get_engine().begin() as c:
        first, last = c.execute(
            text("SELECT first_at, last_at FROM thread_activity WHERE thread_id = '1'")
        ).first()
    assert first.startswith("2026-03-10") and last.startswith("2026-05-10")
