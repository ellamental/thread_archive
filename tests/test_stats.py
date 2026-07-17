"""The token/cost rollup (_store._metrics) + the /api/stats survey it powers.

Cost and token counts live inside ``api_request_completed`` payloads. These tests seed
those events straight into the index — the live importer doesn't synthesize cost, and
a raw core insert also skips the JSONL truth drain, keeping the fixture to the one
concern under test — then assert the incremental fold, the aggregation, and the
reindex-shrink self-heal.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from thread_archive import _api as ta
from thread_archive._web import route


def _seed_events(archive_home, rows):
    """Insert conversation threads + ``api_request_completed`` events.

    ``rows`` is a list of ``(thread_id, source, model, input_tokens, output_tokens,
    cost)`` — ``cost=None`` writes a payload with no cost field (a subscription tool),
    a number writes it (a pay-per-token source).
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
        for j, (tid, _source, model, itok, otok, cost) in enumerate(rows):
            payload = {"model": model, "input_tokens": itok, "output_tokens": otok, "thinking_tokens": 0}
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
            (1, "cloth", "deepseek/deepseek-v4-pro", 1000, 100, 0.05),
            (1, "cloth", "deepseek/deepseek-v4-pro", 2000, 200, 0.10),
            (2, "cloth", "x-ai/grok-4.5", 500, 50, 0.02),
            (3, "claude-code", "claude-opus-4-8", 3000, 300, None),  # tokens, no cost
            (4, "cloth", "<synthetic>", 10, 1, None),  # placeholder model
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
    assert by_source["cloth"]["conversations"] == 3
    assert abs(by_source["cloth"]["cost"] - 0.17) < 1e-9
    assert abs(by_source["cloth"]["avg_cost"] - 0.085) < 1e-9  # over the 2 cost-bearing sessions
    # A subscription source: tokens present, cost absent (null, not a fabricated 0).
    assert by_source["claude-code"]["cost"] is None
    assert by_source["claude-code"]["tokens"] == 3300

    by_model = {r["model"]: r for r in payload["by_model"]}
    assert by_model["deepseek/deepseek-v4-pro"]["requests"] == 2
    assert abs(by_model["deepseek/deepseek-v4-pro"]["cost"] - 0.15) < 1e-9
    assert by_model["claude-opus-4-8"]["cost"] is None
    assert "<synthetic>" not in by_model  # placeholder models are dropped from the model list
    # No default cap: even the lowest-volume model (grok, a single request) is listed.
    assert "x-ai/grok-4.5" in by_model


def test_by_model_lists_every_model_uncapped(archive_home):
    # 25 distinct models, each a single request — none may be silently dropped, and the
    # overview count must reflect all of them.
    _seed_events(archive_home, [(1, "cloth", f"m{i:02d}", 10, 1, None) for i in range(25)])
    _status, payload = _get("/api/stats")
    assert payload["overview"]["models"] == 25
    assert len(payload["by_model"]) == 25
    # An explicit ?models=N still caps for a caller that wants it.
    _status, capped = _get("/api/stats", models=5)
    assert len(capped["by_model"]) == 5
    assert capped["overview"]["models"] == 25  # the count stays true even when the list is capped


def test_refresh_is_incremental(archive_home):
    _seed_events(archive_home, [(1, "cloth", "m1", 100, 10, 0.01)])
    assert ta.stats()["overview"]["tokens"] == 110

    # More events on the same thread — the next survey folds only the delta and
    # accumulates onto the standing sums rather than recomputing from scratch.
    _seed_events(archive_home, [(1, "cloth", "m1", 200, 20, 0.02)])
    s2 = ta.stats()
    assert s2["overview"]["tokens"] == 330
    assert abs(s2["overview"]["cost"] - 0.03) < 1e-9


def test_refresh_resets_when_log_shrinks(archive_home):
    _seed_events(archive_home, [(1, "cloth", "m1", 100, 10, 0.01)])
    ta.stats()

    # Simulate a reindex that rebuilt the log below the cursor, and corrupt the rollup
    # to prove the reset genuinely recomputes rather than trusting the stale sums.
    ta.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    with get_engine().begin() as c:
        c.execute(text("UPDATE metrics_cursor SET through_event_id = 999999"))
        c.execute(text("UPDATE thread_metrics SET input_tokens = 999999"))

    assert ta.stats()["overview"]["input_tokens"] == 100  # rebuilt from the real events
