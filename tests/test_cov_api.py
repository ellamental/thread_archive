"""Dispatch coverage for the thin ``_api`` wrappers.

The public library functions are one-line adapters over the private machinery —
``import_path`` (line-stream vs DB-scanner vs unknown-provider), ``embed``,
``watch`` (one-shot vs looping), and ``redactions``. Their real work is covered
by the machinery's own suites; these tests pin the adapter arms — the provider
routing and the return-shape wrapping — with the inner call stubbed on the
module attribute each adapter resolves at call time.
"""

from __future__ import annotations

import pytest

from thread_archive import _api as ta


def test_import_path_unknown_provider_raises(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        ta.import_path(f, provider="nope")


def test_import_path_routes_to_db_scanner(archive_home, monkeypatch) -> None:
    """A ``db-scan`` provider is handed the whole store path, not a session path,
    and its result comes back verbatim — the dispatch difference between the two
    importer shapes is the only thing distinguishing them here."""
    from thread_archive import _importers

    seen: dict = {}
    sentinel = object()

    def fake_scan(path):
        seen["path"] = path
        return sentinel

    monkeypatch.setattr(_importers, "db_scanners", lambda home=None: {"fakedb": fake_scan})
    f = archive_home / "store.db"
    f.write_text("", encoding="utf-8")

    result = ta.import_path(f, provider="fakedb")
    assert result is sentinel
    assert seen["path"] == f


def test_embed_wraps_indexed_count(archive_home, monkeypatch) -> None:
    from thread_archive._retrieval import vectors

    captured: dict = {}

    def fake_index(*, rebuild, max_events, newest_first):
        captured.update(rebuild=rebuild, max_events=max_events, newest_first=newest_first)
        return 7

    monkeypatch.setattr(vectors, "index_events_local", fake_index)
    out = ta.embed(rebuild=True, max_events=10, newest_first=True)
    assert out == {"embedded": 7}
    assert captured == {"rebuild": True, "max_events": 10, "newest_first": True}


def test_watch_once_polls_under_lock(archive_home, monkeypatch) -> None:
    from thread_archive import _watcher

    poll_result = object()

    class FakeWatcher:
        def __init__(self, *, home, interval):
            self.home, self.interval = home, interval

        def poll_once(self):
            return poll_result

        def run(self):  # pragma: no cover — not exercised on the once path
            raise AssertionError("run() must not be called when once=True")

    monkeypatch.setattr(_watcher, "Watcher", FakeWatcher)
    assert ta.watch(once=True) is poll_result


def test_watch_loop_calls_run(archive_home, monkeypatch) -> None:
    from thread_archive import _watcher

    ran: dict = {}

    class FakeWatcher:
        def __init__(self, *, home, interval):
            ran["interval"] = interval

        def poll_once(self):  # pragma: no cover — not exercised on the loop path
            raise AssertionError("poll_once() must not be called when once=False")

        def run(self):
            ran["ran"] = True

    monkeypatch.setattr(_watcher, "Watcher", FakeWatcher)
    assert ta.watch(once=False, interval=1.5) is None
    assert ran == {"interval": 1.5, "ran": True}


def test_redactions_delegates(archive_home, monkeypatch) -> None:
    from thread_archive._ops import redact

    rows = [{"key_id": "k1", "state": "active"}]
    monkeypatch.setattr(redact, "redaction_statuses", lambda: rows)
    assert ta.redactions() == rows
