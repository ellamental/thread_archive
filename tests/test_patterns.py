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
            ("tool_execution_completed", {"tool_name": "unknown", "tool_call_id": "a2", "output": "ok"}, None),
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
                        "VALUES (:tid, 's', :api, :typ, :payload, :at)"
                    ),
                    {
                        "tid": tid,
                        "api": api_call_id,
                        "typ": event_type,
                        "payload": json.dumps(payload),
                        "at": {
                            "t1": "2026-07-20T10:00:00Z",
                            "t2": "2026-07-22T10:00:00Z",
                            "t3": "2026-07-21T10:00:00Z",
                        }[tid],
                    },
                )


def test_patterns_endpoint_before_first_run(archive_home):
    api.open_archive(str(archive_home))
    status, payload = _get("/api/experiments/patterns")
    assert status == 200
    assert payload["status"] == "not_run"
    assert payload["patterns"] == []
    assert payload["report_path"].endswith("experiments/patterns/report.json")


def test_mines_gapped_shape_and_tool_specific_sequences(archive_home):
    _seed(archive_home)
    report = api.mine_patterns(
        home=str(archive_home), min_support=2, max_length=3, max_gap=2,
        max_patterns=200, thread_types=("conversation",),
    )
    assert report["status"] == "ready"
    assert report["corpus"]["threads"] == 3
    assert report["through_event_id"] > 0
    experiment = archive_home / "experiments" / "patterns"
    assert (experiment / "report.json").exists()
    assert (experiment / "matches.db").exists()
    assert "patterns list" in (experiment / "README.md").read_text()

    patterns = {(p["abstraction"], tuple(p["activities"])): p for p in report["patterns"]}
    detailed = patterns[("detail", ("tool:error:Bash", "tool:call:Bash"))]
    assert detailed["support"] == 2
    assert detailed["occurrences"] == 2
    assert detailed["direct_occurrences"] == 0  # assistant text sits between them
    assert detailed["lift"] > 1
    assert detailed["examples"][0]["thread_id"] == "t2"
    assert "matched_at" in detailed["examples"][0]
    assert patterns[("shape", ("tool:error", "tool:call"))]["support"] == 2

    status, detail = _get(f"/api/experiments/patterns/{detailed['id']}/matches")
    assert status == 200
    assert detail["total"] == 2
    assert [match["thread_id"] for match in detail["matches"]] == ["t2", "t1"]
    assert detail["matches"][0]["event_ids"]
    assert detail["has_more"] is False

    status, loaded = _get("/api/experiments/patterns")
    assert status == 200 and loaded["status"] == "ready"
    assert loaded["stale"] is False and loaded["stale_events"] == 0

    status, _, body, _ = route(
        "GET", "/api/experiments/patterns/catalog",
        {"query": ["Bash"], "lens": ["detail"], "sort": ["lift"], "limit": ["1"]},
    )
    catalog = json.loads(body)
    assert status == 200 and catalog["returned"] == 1
    assert "Bash" in " ".join(catalog["patterns"][0]["activities"])


def test_pattern_matches_paginate_and_reject_unknown_pattern(archive_home):
    _seed(archive_home)
    report = api.mine_patterns(
        home=str(archive_home), min_support=2, max_patterns=40,
        thread_types=("conversation",),
    )
    pattern = next(item for item in report["patterns"] if item["support"] >= 2)
    status, ctype, body, _ = route(
        "GET", f"/api/experiments/patterns/{pattern['id']}/matches", {"limit": ["1"]},
    )
    page = json.loads(body)
    assert status == 200 and ctype.startswith("application/json")
    assert len(page["matches"]) == 1
    assert page["has_more"] is True

    status, payload = _get("/api/experiments/patterns/not-a-pattern/matches")
    assert status == 404
    assert payload["error"] == "pattern not found"


def test_report_marks_new_events_stale(archive_home):
    _seed(archive_home)
    report = api.mine_patterns(home=str(archive_home), min_support=2, max_patterns=40)
    from thread_archive._store import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at) "
                "VALUES ('t1', 's', 'user_message_sent', '{\"content\":\"new\"}', "
                "'2026-07-22T11:00:00Z')"
            )
        )
    _, loaded = _get("/api/experiments/patterns")
    assert loaded["stale"] is True
    assert loaded["stale_events"] == 1

    from thread_archive._patterns import _iter_traces

    snapshot_ids = [
        activity.event_id
        for trace in _iter_traces(
            ("conversation",), through_event_id=report["through_event_id"],
        )
        for activity in trace.activities
    ]
    assert max(snapshot_ids) <= report["through_event_id"]


def test_patterns_cli_publishes_report(archive_home, capsys):
    _seed(archive_home)
    rc = main([
        "patterns", "--home", str(archive_home), "--types", "conversation",
        "--min-support", "2", "--max-patterns", "20",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "behavioral events across 3 threads" in out
    assert "experiments/patterns/report.json" in out

    report = api.patterns(home=str(archive_home))
    pattern_id = report["patterns"][0]["id"]
    rc = main([
        "patterns", "list", "--home", str(archive_home),
        "--query", report["patterns"][0]["activities"][0], "--limit", "1",
    ])
    assert rc == 0
    catalog = json.loads(capsys.readouterr().out)
    assert catalog["status"] == "ready" and len(catalog["patterns"]) == 1

    rc = main([
        "patterns", "read", pattern_id, "--home", str(archive_home), "--limit", "1",
    ])
    assert rc == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["pattern"]["id"] == pattern_id
    assert len(detail["matches"]) == 1

    rc = main([
        "patterns", "--home", str(archive_home), "--types", "conversation",
        "--min-support", "2", "--max-patterns", "20", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["corpus"]["threads"] == 3

    rc = main([
        "patterns", "read", pattern_id, "--home", str(archive_home), "--limit", "100",
    ])
    assert rc == 0
    complete = json.loads(capsys.readouterr().out)
    assert complete["has_more"] is False and "next_offset" not in complete

    try:
        main(["patterns", "read", "--home", str(archive_home)])
    except SystemExit as exc:
        assert "pattern_id is required" in str(exc)
    else:  # pragma: no cover - argparse action requires the id at dispatch
        raise AssertionError("pattern read accepted no id")

    rc = main([
        "patterns", "read", "not-a-pattern", "--home", str(archive_home),
    ])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["status"] == "not_found"

    (archive_home / "experiments" / "patterns" / "matches.db").unlink()
    rc = main(["patterns", "read", pattern_id, "--home", str(archive_home)])
    assert rc == 0
    missing_index = json.loads(capsys.readouterr().out)
    assert missing_index["status"] == "not_indexed"
    assert "rebuild" in missing_index["next"]


def test_patterns_cli_rejects_invalid_bounds(archive_home):
    _seed(archive_home)
    try:
        main(["patterns", "--home", str(archive_home), "--min-support", "1"])
    except SystemExit as exc:
        assert "min_support must be at least 2" in str(exc)
    else:  # pragma: no cover - the CLI must reject it
        raise AssertionError("invalid support accepted")
