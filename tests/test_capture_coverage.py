"""Capture blind-spot detection: parse-error surfacing, the capture-skip ledger,
the watch-pass heartbeat, and the source↔archive coverage check.

These guard the *silent* capture failure modes — content consumed without a
trace (soft format drift), sources gone dark without an error, and a wedged
ingest loop indistinguishable from a quiet day. The loud failures (exceptions)
already ride ``watch_errors_last``; everything here exists for failures that
never throw.
"""

from __future__ import annotations

import json
import time

from thread_archive import _api as ta
from thread_archive._importers import import_session_incremental
from thread_archive._importers._skip_ledger import LEDGER_FILE, summarize_skips
from thread_archive._ops.coverage import check_coverage
from thread_archive._ops.health import pipeline_verdict, read_health, record_health
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult
from thread_archive._watcher.daemon import Watcher

from .helpers import cc_assistant, cc_user, import_cc_session, write_jsonl


# ── stubs ────────────────────────────────────────────────────────────────────


class StubWatcher(SourceWatcher):
    """A source watcher with fully scripted store state and poll result."""

    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        items: int | None = 1,
        size: int = 100,
        latest: float | None = None,
        tracks_content: bool = True,
        result: WatchResult | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._items = items
        self._size = size
        self._latest = latest
        self.store_mtime_tracks_content = tracks_content
        self._result = result or WatchResult()

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def poll(self) -> WatchResult:
        return self._result

    def discover(self) -> SourceDiscovery:
        return SourceDiscovery(
            name=self._name,
            available=self._available,
            items=self._items if self._available else 0,
            bytes=self._size if self._available else 0,
            latest=self._latest if self._available else None,
        )


# ── parse-error surfacing ────────────────────────────────────────────────────


def test_parse_errors_ride_the_import_result(archive_home, tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(
        json.dumps(cc_user("pe")) + "\n"
        + "{this line is not json\n"
        + json.dumps(cc_assistant("pe")) + "\n",
        encoding="utf-8",
    )
    ta.open_archive()
    result = import_session_incremental(f, "proj:pe")
    assert result.events_created > 0
    assert result.parse_errors == 1


# ── the capture-skip ledger ──────────────────────────────────────────────────


def test_zero_yield_cc_import_lands_on_the_skip_ledger(archive_home, tmp_path):
    # Unknown CC line *types* are preserved verbatim by the parser (so plain
    # type-drift never consumes content on this path) — but a known type whose
    # payload no longer parses to a message yields nothing, the thread is
    # discarded, and the watermark still advances. The ledger must hold the trace.
    f = tmp_path / "drift.jsonl"
    write_jsonl(f, [
        {"type": "user", "uuid": "d-1", "timestamp": "2026-01-01T10:00:00Z", "message": {}},
        {"type": "user", "uuid": "d-2", "timestamp": "2026-01-01T10:00:01Z", "message": {}},
    ])
    ta.open_archive()
    result = import_session_incremental(f, "proj:drift")
    assert result.events_created == 0

    ledger = archive_home / LEDGER_FILE
    records = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["source"] == "claude-code"
    assert rec["source_id"] == "proj:drift"
    assert rec["reason"] == "empty_import_discarded"
    assert rec["lines_skipped"] == 2

    summary = summarize_skips()
    assert summary["total"] == 1
    assert summary["recent"] == 1
    assert summary["recent_lines"] == 2


def test_line_stream_no_importable_content_lands_on_the_skip_ledger(archive_home, tmp_path):
    from thread_archive._importers import import_codex_session_incremental

    f = tmp_path / "codex.jsonl"
    write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "s1", "cwd": "/proj"}},
    ])
    ta.open_archive()
    result = import_codex_session_incremental(f, "s1")
    assert result.events_created == 0

    records = [
        json.loads(ln)
        for ln in (archive_home / LEDGER_FILE).read_text().splitlines()
        if ln.strip()
    ]
    assert [r["reason"] for r in records] == ["no_importable_content"]
    assert records[0]["source"] == "codex"


def test_normal_import_writes_no_skip_records(archive_home, tmp_path):
    import_cc_session(tmp_path, "clean")
    assert not (archive_home / LEDGER_FILE).exists()


# ── the watch-pass heartbeat ─────────────────────────────────────────────────


def test_poll_once_records_heartbeat_with_per_source_yield(archive_home):
    ta.open_archive()
    stub = StubWatcher("stub-src", result=WatchResult(
        sources_checked=3, items_imported=1, events_created=2,
        lines_processed=7, parse_errors=1,
    ))
    w = Watcher([stub], embed=False)
    w.poll_once()

    rec = read_health().get("watch_pass_last")
    assert rec is not None
    assert rec["passes"] == 1
    src = rec["sources"]["stub-src"]
    assert src == {
        "checked": 3, "items": 1, "events": 2,
        "lines": 7, "parse_errors": 1, "errors": 0,
    }


def test_heartbeat_accumulates_and_throttles(archive_home):
    ta.open_archive()
    stub = StubWatcher("stub-src", result=WatchResult(sources_checked=1, events_created=1))
    w = Watcher([stub], embed=False)
    w.poll_once()
    w.poll_once()  # within the throttle window: totals grow, record stays at pass 1
    assert read_health()["watch_pass_last"]["passes"] == 1
    assert w._source_totals["stub-src"]["events"] == 2

    w._heartbeat_recorded_at = None  # window elapsed
    w.poll_once()
    assert read_health()["watch_pass_last"]["passes"] == 3
    assert read_health()["watch_pass_last"]["sources"]["stub-src"]["events"] == 3


def test_poll_exception_counts_as_source_error(archive_home):
    ta.open_archive()

    class Exploding(StubWatcher):
        def poll(self):
            raise RuntimeError("boom")

    w = Watcher([Exploding("bad-src")], embed=False)
    result = w.poll_once()
    assert result.errors
    assert w._source_totals["bad-src"]["errors"] == 1


# ── the coverage check ───────────────────────────────────────────────────────


def test_coverage_flags_a_source_gone_dark(archive_home, tmp_path):
    import_cc_session(tmp_path, "dark")
    r = check_coverage(
        watchers=[StubWatcher("claude-code", available=False)], min_history=1
    )
    assert not r["ok"]
    assert any("went dark" in msg for msg in r["failed"])
    assert r["sources"]["claude-code"]["failed"] == "went_dark"
    rec = read_health()["coverage_last"]
    assert rec["ok"] is False


def test_coverage_ignores_a_missing_store_with_no_history(archive_home):
    r = check_coverage(watchers=[StubWatcher("claude-code", available=False)])
    assert r["ok"]


def test_coverage_flags_stale_ingest_against_events_not_watermarks(archive_home, tmp_path):
    # The archived events are from 2026-01-01 (helpers fixtures); the store
    # claims activity now. Watermark-advancing zero-yield imports would keep
    # last_import_at fresh — the check must compare *events*, which is what
    # makes it catch soft drift.
    import_cc_session(tmp_path, "stale")
    r = check_coverage(
        watchers=[StubWatcher("claude-code", latest=time.time())], min_history=1
    )
    assert not r["ok"]
    assert any("stale" in msg for msg in r["failed"])
    assert r["sources"]["claude-code"]["failed"] == "stale_ingest"


def test_coverage_green_within_grace(archive_home, tmp_path):
    import_cc_session(tmp_path, "fresh")
    r = check_coverage(
        watchers=[StubWatcher("claude-code", latest=time.time())],
        grace_hours=24 * 365 * 10,
    )
    assert r["ok"]
    rec = read_health()["coverage_last"]
    assert rec["ok"] is True


def test_coverage_exempts_churny_db_stores_from_staleness(archive_home, tmp_path):
    import_cc_session(tmp_path, "db")
    r = check_coverage(
        watchers=[
            StubWatcher("claude-code", latest=time.time(), tracks_content=False)
        ],
    )
    assert r["ok"]


def test_coverage_warns_on_content_never_ingested(archive_home):
    r = check_coverage(watchers=[StubWatcher("grok", items=4, size=4096)])
    assert r["ok"]  # a warning, not a failure
    assert any("never" in msg for msg in r["warnings"])
    assert r["sources"]["grok"]["warning"] == "never_ingested"


def test_coverage_reports_unwatched_sources(archive_home, tmp_path):
    f = tmp_path / "chatgpt-like.jsonl"
    write_jsonl(f, [cc_user("uw"), cc_assistant("uw")])
    ta.open_archive()
    import_session_incremental(f, "uw-1", source="chatgpt")
    r = check_coverage(watchers=[])
    assert "chatgpt" in r["unwatched"]
    assert r["unwatched"]["chatgpt"]["newest_event_at"] is not None


def test_out_of_band_coverage_run_retires_nightly_stage(archive_home):
    record_health("nightly_last", {"ok": False, "failed_stages": ["coverage"]})
    assert pipeline_verdict()["failed_stages"] == ["coverage"]
    check_coverage(watchers=[])  # green: nothing to check
    verdict = pipeline_verdict()
    assert verdict["ok"]
    assert verdict["recovered_stages"] == ["coverage"]
