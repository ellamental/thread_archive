"""Lazy catch-up ingest (``_watcher.lazy``) + its MCP-server wiring.

The concurrency contract under test: exactly one process ingests at a time
(the ingest-owner flock), the daemon owns the lock for its lifetime, and a
lazy pass skips — losing nothing — whenever someone else holds it.
"""

from __future__ import annotations

import threading
import time

from thread_archive._watcher import Watcher, catch_up_once, try_ingest_owner_lock
from thread_archive._watcher.base import SourceWatcher, WatchResult


class _StubSource(SourceWatcher):
    source_name = "stub"

    def __init__(self, events: int = 0) -> None:
        self.polls = 0
        self._events = events

    def is_available(self) -> bool:
        return True

    def poll(self) -> WatchResult:
        self.polls += 1
        return WatchResult(sources_checked=1, items_imported=0, events_created=self._events)


def test_catch_up_once_runs_a_pass(archive_home) -> None:
    src = _StubSource()
    result = catch_up_once(watchers=[src], embed=False)
    assert result is not None
    assert src.polls == 1


def test_catch_up_once_skips_while_owner_lock_held(archive_home) -> None:
    src = _StubSource()
    with try_ingest_owner_lock() as owned:
        assert owned
        assert catch_up_once(watchers=[src], embed=False) is None
    assert src.polls == 0
    # Lock released — the next pass runs.
    assert catch_up_once(watchers=[src], embed=False) is not None


def test_daemon_run_holds_owner_lock_for_its_lifetime(archive_home) -> None:
    watcher = Watcher([_StubSource()], interval=0.1, embed=False)
    t = threading.Thread(target=watcher.run, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5.0
        locked = False
        while time.monotonic() < deadline:
            with try_ingest_owner_lock() as owned:
                locked = not owned
            if locked:
                break
            time.sleep(0.05)
        assert locked, "daemon never took the ingest-owner lock"
    finally:
        watcher.stop()
        t.join(timeout=5.0)
    # Released on exit: a lazy pass owns ingest again.
    with try_ingest_owner_lock() as owned:
        assert owned


def _await_idle(throttle, what: str) -> None:
    """Block until the throttle's in-flight pass has finished."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if throttle.running.acquire(blocking=False):
            throttle.running.release()
            return
        time.sleep(0.01)
    raise AssertionError(f"the background pass never finished: {what}")


def test_mcp_throttle_drives_a_real_catch_up_pass(archive_home, tmp_path, monkeypatch) -> None:
    """The MCP server's ingest gate end to end: with a transcript sitting in a
    store the archive has never read, one kick imports it — and the kill-switch
    stops that happening at all."""
    import json

    from thread_archive import _api as ta
    from thread_archive._mcp.server import IngestThrottle

    # A machine whose only AI-tool store is one claude-code session.
    fake_home = tmp_path / "machine"
    sess = fake_home / ".claude" / "projects" / "proj"
    sess.mkdir(parents=True)
    (sess / "s1.jsonl").write_text("\n".join(json.dumps(ln) for ln in (
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
         "cwd": "/proj", "message": {"role": "user", "content": "lazy ingest please"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "done"}]}},
    )) + "\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    throttle = IngestThrottle()
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "0")
    throttle.maybe_catch_up()
    assert throttle.last == 0.0  # kill-switch: nothing claimed, nothing run
    assert not ta.search("lazy ingest")

    monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST")
    throttle.maybe_catch_up()
    first = throttle.last
    assert first > 0.0
    _await_idle(throttle, "the enabled kick")
    ta.close()  # the pass ran on its own thread; reopen to read what it wrote
    assert ta.search("lazy ingest")

    # Within the throttle window: no second pass, whatever else arrives.
    (sess / "s2.jsonl").write_text(json.dumps(
        {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T11:00:00Z",
         "cwd": "/proj", "message": {"role": "user", "content": "second session"}},
    ) + "\n", encoding="utf-8")
    throttle.maybe_catch_up()
    assert throttle.last == first
    time.sleep(0.1)
    assert not ta.search("second session")
