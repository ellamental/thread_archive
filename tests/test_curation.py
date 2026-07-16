"""The curation drains (``_curation``): gate → launch → heartbeat.

No real ``claude`` is ever spawned — ``subprocess.Popen`` is faked — and the
gates are injected, so these tests drive the launch flow and the command
shape, not this machine's archive or Claude login.
"""

from __future__ import annotations

import json

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
            "(1, 1, 's1', 'user_query', '{}', "
            "datetime('now', '-2 hours'), datetime('now', '-2 hours'))"
        ))
    assert _curation.librarian_backlog() == 1
    assert _curation.gardener_backlog() == 1
