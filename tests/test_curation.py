"""The curation drains (``_curation``): gate → launch → heartbeat.

No real ``claude`` is ever spawned — ``subprocess.Popen`` is faked — and the
gates are injected, so these tests drive the launch flow and the command
shape, not this machine's archive or Claude login.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from thread_archive import _curation
from thread_archive._config import resolve_paths


class FakeProc:
    """Stands in for the spawned claude instance."""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = 0

    def communicate(self, timeout=None):
        return "curated 3 threads\n", ""


@pytest.fixture
def spawned(archive_home, monkeypatch):
    """Capture the Popen call (there must be at most one per fire)."""
    calls: list[FakeProc] = []

    def popen(args, **kwargs):
        proc = FakeProc(args, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(_curation.subprocess, "Popen", popen)
    # Deterministic read-server choice: no probe of this machine's port 8788.
    monkeypatch.setattr(_curation, "shared_mcp_up", lambda port: False)
    return calls


def test_drain_runs_from_in_home_curation_cwd(archive_home, spawned) -> None:
    """The spawn's cwd is ``<home>/curation`` — the marker the claude-code
    importer types the drain's own session by ('system': archive machinery,
    never librarian work), and a CLAUDE.md-free directory besides."""
    rc = _curation.run("librarian", gate=lambda home: 3, claude="/fake/claude")
    assert rc == 0
    assert len(spawned) == 1
    cwd = spawned[0].kwargs["cwd"]
    assert cwd == str(resolve_paths(None).home / "curation")
    assert Path(cwd).is_dir()


def test_drained_queue_skips_launch_but_heartbeats(archive_home, spawned) -> None:
    rc = _curation.run("librarian", gate=lambda home: 0, claude="/fake/claude")
    assert rc == 0
    assert spawned == []
    assert (archive_home / "logs" / "librarian.heartbeat").exists()


def test_backlog_launches_bounded_strict_run(archive_home, spawned) -> None:
    rc = _curation.run("librarian", gate=lambda home: 7, claude="/fake/claude")
    assert rc == 0
    assert len(spawned) == 1
    args = spawned[0].args
    assert args[0] == "/fake/claude"
    assert "--print" in args
    # No config.json: the defaults — Opus at xhigh effort.
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--effort") + 1] == "xhigh"
    # Unattended run: the strict, dedicated MCP config is the containment.
    assert "--strict-mcp-config" in args
    config_path = args[args.index("--mcp-config") + 1]
    servers = json.loads(open(config_path).read())["mcpServers"]
    assert set(servers) == {"thread-archive", "thread-archive-librarian"}
    # Both stdio servers pin the gated home, whatever the launch env says.
    assert servers["thread-archive-librarian"]["env"]["THREAD_ARCHIVE_HOME"] == str(
        resolve_paths(None).home
    )
    # The prompt is the packaged drain text plus this run's cap.
    prompt = args[-1]
    assert "review_queue" in prompt
    assert "at most 25 threads" in prompt
    assert (archive_home / "logs" / "librarian.heartbeat").exists()


def test_failed_gate_fails_open(archive_home, spawned) -> None:
    # None = the count query failed: launch anyway rather than silently
    # parking the drain.
    _curation.run("gardener", gate=lambda home: None, claude="/fake/claude")
    assert len(spawned) == 1
    prompt = spawned[0].args[-1]
    assert "garden_status" in prompt
    assert "at most 30 write actions" in prompt


def test_missing_claude_heartbeats_without_launch(archive_home, spawned, monkeypatch) -> None:
    monkeypatch.setattr(_curation, "resolve_claude", lambda: None)
    rc = _curation.run("librarian", gate=lambda home: 5)
    assert rc == 0
    assert spawned == []
    assert (archive_home / "logs" / "librarian.heartbeat").exists()


def test_batch_override_reaches_prompt(archive_home, spawned) -> None:
    _curation.run("librarian", gate=lambda home: 5, claude="/fake/claude", batch=3)
    assert "at most 3 threads" in spawned[0].args[-1]


def test_shared_mcp_preferred_when_up(archive_home, spawned, monkeypatch) -> None:
    monkeypatch.setattr(_curation, "shared_mcp_up", lambda port: True)
    _curation.run("librarian", gate=lambda home: 1, claude="/fake/claude")
    args = spawned[0].args
    servers = json.loads(open(args[args.index("--mcp-config") + 1]).read())["mcpServers"]
    assert servers["thread-archive"] == {"type": "http", "url": "http://127.0.0.1:8788/mcp"}
    # The write server stays a stdio subprocess either way.
    assert "command" in servers["thread-archive-librarian"]


def test_family_heartbeat_mirrored_when_dir_exists(archive_home, spawned, monkeypatch, tmp_path) -> None:
    # Same contract as the nightly's family heartbeat: stamped into the
    # thread-family logs dir when it exists, skipped (not created) otherwise.
    family = tmp_path / "family_logs"
    family.mkdir()
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(family))
    _curation.run("librarian", gate=lambda home: 0, claude="/fake/claude")
    assert (family / "archive-librarian.heartbeat").exists()
    absent = tmp_path / "no_such_dir"
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(absent))
    _curation.run("gardener", gate=lambda home: 0, claude="/fake/claude")
    assert not absent.exists()


def test_config_sets_model_and_effort_per_drain(archive_home, spawned) -> None:
    from thread_archive._config import save_config

    save_config({"curation": {
        "librarian": {"model": "sonnet", "effort": "high"},
        # Empty effort is the escape hatch: pass no --effort flag at all.
        "gardener": {"model": "haiku", "effort": ""},
    }})
    _curation.run("librarian", gate=lambda home: 1, claude="/fake/claude")
    args = spawned[0].args
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--effort") + 1] == "high"
    _curation.run("gardener", gate=lambda home: 1, claude="/fake/claude")
    args = spawned[1].args
    assert args[args.index("--model") + 1] == "haiku"
    assert "--effort" not in args


def test_malformed_curation_config_falls_back_to_defaults(archive_home) -> None:
    from thread_archive._config import save_config

    assert _curation.curation_settings("librarian") == ("opus", "xhigh")
    save_config({"curation": {"librarian": {"model": 7, "effort": None}, "gardener": "nope"}})
    # Bad model type → default; explicit null effort → omit the flag.
    assert _curation.curation_settings("librarian") == ("opus", "")
    # A non-dict drain entry means defaults for that drain.
    assert _curation.curation_settings("gardener") == ("opus", "xhigh")


def test_cadence_config_read_and_validated(archive_home) -> None:
    from thread_archive._config import save_config

    # Unset: None means "installer default".
    assert _curation.librarian_interval() is None
    assert _curation.gardener_at() is None
    save_config({"curation": {
        "librarian": {"interval_minutes": 30},
        "gardener": {"at": "22:45"},
    }})
    assert _curation.librarian_interval() == 1800
    assert _curation.gardener_at() == (22, 45)
    # Malformed values fail soft to the default cadence, never crash a fire.
    save_config({"curation": {
        "librarian": {"interval_minutes": -5},
        "gardener": {"at": "25:99"},
    }})
    assert _curation.librarian_interval() is None
    assert _curation.gardener_at() is None
    save_config({"curation": {
        "librarian": {"interval_minutes": True},
        "gardener": {"at": "soonish"},
    }})
    assert _curation.librarian_interval() is None
    assert _curation.gardener_at() is None


def test_gates_read_missing_index_as_failed(archive_home) -> None:
    # No index.db yet: the count queries fail → None (fail open), never a crash.
    assert _curation.librarian_backlog() is None
    assert _curation.gardener_backlog() is None


def test_gates_count_real_index(archive_home) -> None:
    # A real (tiny) index: a conversation thread with an event and no
    # summary/citations is librarian backlog; a lone live topic is gardener
    # backlog (singleton, uncited, unparented all at once).
    from thread_archive._store import _base
    from thread_archive._store.models import Base

    engine = _base.get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text(
            "INSERT INTO threads (id, name, thread_type, archived, title) VALUES "
            "(1, 'chat-1', 'conversation', 0, 'a chat'), "
            "(2, 'topic-1', 'topic', 0, 'a topic')"
        ))
        conn.execute(text(
            "INSERT INTO events (id, thread_id, stream_id, event_type, payload, "
            "occurred_at, recorded_at) VALUES "
            "(1, 1, 's1', 'user_message_sent', '{}', "
            "datetime('now', '-2 hours'), datetime('now', '-2 hours'))"
        ))
    assert _curation.librarian_backlog() == 1
    assert _curation.gardener_backlog() == 1


def _seed_gate_index(rows: list[tuple[str, str]]) -> None:
    """A tiny index of curatable conversations: ``(thread_name, occurred_at)``."""
    from sqlalchemy import text

    from thread_archive._store import _base
    from thread_archive._store.models import Base

    engine = _base.get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for i, (name, occurred) in enumerate(rows, start=1):
            conn.execute(
                text("INSERT INTO threads (id, name, thread_type, archived, title) "
                     "VALUES (:i, :n, 'conversation', 0, :n)"),
                {"i": str(i), "n": name},
            )
            conn.execute(
                text("INSERT INTO events (id, thread_id, stream_id, event_type, "
                     "payload, occurred_at, recorded_at) VALUES "
                     "(:i, :t, 's', 'user_message_sent', '{}', :o, "
                     " datetime('now', '-2 hours'))"),
                {"i": i, "t": str(i), "o": occurred},
            )


def test_librarian_gate_counts_only_what_the_queue_would_hand_out(archive_home) -> None:
    # The gate decides whether a fire happens at all, so it has to mirror
    # review_queue's horizon split. Counting history the queue will never serve
    # would launch an Opus run every hour to discover there is nothing to do.
    from thread_archive import _curation

    _seed_gate_index([
        ("old-a", "2025-03-01 10:00:00"),
        ("old-b", "2025-04-01 10:00:00"),
        ("old-c", "2025-05-01 10:00:00"),
        ("new-a", "2026-06-02 10:00:00"),
    ])

    # No horizon: everything counts.
    assert _curation.librarian_backlog() == 4

    # Default policy: only what happened after the install point.
    _curation.set_curation_policy(horizon=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert _curation.librarian_backlog() == 1

    # A rate adds only as much history as a run would actually take — an archive
    # of thousands with a cap of 2 is 2 items of backlog, not thousands.
    _curation.set_curation_policy(catchup_per_run=2)
    assert _curation.librarian_backlog() == 3
    _curation.set_curation_policy(catchup_per_run=99)
    assert _curation.librarian_backlog() == 4

    # Opting back in to the whole archive.
    _curation.set_curation_policy(clear_horizon=True)
    assert _curation.librarian_backlog() == 4


def test_librarian_gate_ignores_threads_with_no_curatable_content(archive_home) -> None:
    # A thread carrying only bookkeeping events (an empty file_snapshot, a
    # queue_operation) has nothing to cite and nothing to summarize, so it is not
    # backlog. It would otherwise be permanently un-drainable — and since the
    # queue is newest-first, it would sit at the head and relaunch an instance
    # every fire to rediscover work it cannot do.
    from thread_archive._store import _base
    from thread_archive._store.models import Base

    engine = _base.get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text(
            "INSERT INTO threads (id, name, thread_type, archived, title) VALUES "
            "(1, 'shell-1', 'conversation', 0, 'Claude Code Session')"
        ))
        conn.execute(text(
            "INSERT INTO events (id, thread_id, stream_id, event_type, payload, "
            "occurred_at, recorded_at) VALUES "
            "(1, 1, 's1', 'file_snapshot', '{\"files\": []}', "
            "datetime('now', '-2 hours'), datetime('now', '-2 hours')), "
            "(2, 1, 's1', 'queue_operation', '{\"operation\": \"dequeue\"}', "
            "datetime('now', '-2 hours'), datetime('now', '-2 hours'))"
        ))
    assert _curation.librarian_backlog() == 0

    # One real message flips the same thread into backlog.
    with engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(text(
            "INSERT INTO events (id, thread_id, stream_id, event_type, payload, "
            "occurred_at, recorded_at) VALUES "
            "(3, 1, 's1', 'user_message_sent', '{\"content\": \"hi\"}', "
            "datetime('now', '-2 hours'), datetime('now', '-2 hours'))"
        ))
    assert _curation.librarian_backlog() == 1
