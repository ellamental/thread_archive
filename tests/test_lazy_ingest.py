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


def test_mcp_maybe_catch_up_throttles_and_respects_env(archive_home, monkeypatch) -> None:
    from thread_archive._mcp import server
    from thread_archive import _watcher

    calls = []

    def fake_catch_up(*a, **k):
        calls.append(1)
        return WatchResult()

    monkeypatch.setattr(_watcher, "catch_up_once", fake_catch_up)
    monkeypatch.setattr(server, "_ingest_last", 0.0)

    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "0")
    server._maybe_catch_up()
    assert not calls

    monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST")
    server._maybe_catch_up()
    deadline = time.monotonic() + 5.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    # Within the throttle window: no second pass.
    server._maybe_catch_up()
    time.sleep(0.1)
    assert len(calls) == 1
