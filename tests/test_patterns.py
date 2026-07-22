"""General behavioral sequence mining, its CLI batch, and viewer report endpoint."""

from __future__ import annotations

import json

from sqlalchemy import text

from thread_archive import _api as api
from thread_archive._web import route
from thread_archive.cli import main


def _get(path: str):
    status, ctype, body, _ = route("GET", path, {})
    return status, json.loads(body) if ctype.startswith("application/json") else body


def _seed(archive_home) -> None:
    api.open_archive(str(archive_home))
    from thread_archive._store import get_engine

    traces = {
        "t1": [
            ("user_message_sent", {"content": "one"}, None),
            ("tool_use_complete", {"tool_name": "Bash", "tool_call_id": "a1", "input": {}}, "r1"),
            ("tool_execution_error", {"tool_call_id": "a1", "error": "bad"}, None),
            ("text_complete", {"text": "retrying"}, "r2"),
            ("tool_use_complete", {"tool_name": "Bash", "tool_call_id": "a2", "input": {}}, "r2"),
            ("tool_execution_completed", {"tool_call_id": "a2", "output": "ok"}, None),
        ],
        "t2": [
            ("user_message_sent", {"content": "two"}, None),
            ("tool_use_complete", {"tool_name": "Bash", "tool_call_id": "b1", "input": {}}, "r3"),
            ("tool_execution_error", {"tool_call_id": "b1", "error": "bad"}, None),
            ("text_complete", {"text": "trying another way"}, "r4"),
            ("tool_use_complete", {"tool_name": "Bash", "tool_call_id": "b2", "input": {}}, "r4"),
            ("tool_execution_completed", {"tool_call_id": "b2", "output": "ok"}, None),
        ],
        "t3": [
            ("user_message_sent", {"content": "three"}, None),
            ("tool_use_complete", {"tool_name": "Read", "tool_call_id": "c1", "input": {}}, "r5"),
            ("tool_execution_completed", {"tool_name": "Read", "tool_call_id": "c1", "output": "ok"}, None),
        ],
    }
    with get_engine().begin() as conn:
        for tid, events in traces.items():
            conn.execute(
                text(
                    "INSERT INTO threads (id, name, title, thread_type, source, archived) "
                    "VALUES (:id, :name, :title, 'conversation', 'fixture', 0)"
                ),
                {"id": tid, "name": f"fixture:{tid}", "title": f"Trace {tid}"},
            )
            for event_type, payload, api_call_id in events:
                conn.execute(
                    text(
                        "INSERT INTO events (thread_id, stream_id, api_call_id, event_type, payload, occurred_at) "
                        "VALUES (:tid, 's', :api, :typ, :payload, '2026-07-22T10:00:00Z')"
                    ),
                    {
                        "tid": tid,
                        "api": api_call_id,
                        "typ": event_type,
                        "payload": json.dumps(payload),
                    },
                )


def test_patterns_endpoint_before_first_run(archive_home):
    api.open_archive(str(archive_home))
    status, payload = _get("/api/patterns")
    assert status == 200
    assert payload["status"] == "not_run"
    assert payload["patterns"] == []
    assert payload["report_path"].endswith("patterns.json")


def test_mines_gapped_shape_and_tool_specific_sequences(archive_home):
    _seed(archive_home)
    report = api.mine_patterns(
        home=str(archive_home), min_support=2, max_length=3, max_gap=2,
        max_patterns=200, thread_types=("conversation",),
    )
    assert report["status"] == "ready"
    assert report["corpus"]["threads"] == 3
    assert report["through_event_id"] > 0
    assert (archive_home / "patterns.json").exists()

    patterns = {(p["abstraction"], tuple(p["activities"])): p for p in report["patterns"]}
    detailed = patterns[("detail", ("tool:error:Bash", "tool:call:Bash"))]
    assert detailed["support"] == 2
    assert detailed["occurrences"] == 2
    assert detailed["direct_occurrences"] == 0  # assistant text sits between them
    assert detailed["lift"] > 1
    assert detailed["examples"][0]["thread_id"] == "t2"
    assert patterns[("shape", ("tool:error", "tool:call"))]["support"] == 2

    status, loaded = _get("/api/patterns")
    assert status == 200 and loaded["status"] == "ready"
    assert loaded["stale"] is False and loaded["stale_events"] == 0


def test_report_marks_new_events_stale(archive_home):
    _seed(archive_home)
    api.mine_patterns(home=str(archive_home), min_support=2, max_patterns=40)
    from thread_archive._store import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                "VALUES ('t1', 's', 'user_message_sent', '{\"content\":\"new\"}', "
                "'2026-07-22T11:00:00Z')"
            )
        )
    _, loaded = _get("/api/patterns")
    assert loaded["stale"] is True
    assert loaded["stale_events"] == 1


def test_patterns_cli_publishes_report(archive_home, capsys):
    _seed(archive_home)
    rc = main([
        "patterns", "--home", str(archive_home), "--types", "conversation",
        "--min-support", "2", "--max-patterns", "20",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "behavioral events across 3 threads" in out
    assert "patterns.json" in out


def test_patterns_cli_rejects_invalid_bounds(archive_home):
    _seed(archive_home)
    try:
        main(["patterns", "--home", str(archive_home), "--min-support", "1"])
    except SystemExit as exc:
        assert "min_support must be at least 2" in str(exc)
    else:  # pragma: no cover - the CLI must reject it
        raise AssertionError("invalid support accepted")
