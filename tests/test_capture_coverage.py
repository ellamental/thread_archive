"""Capture blind-spot detection: parse-error surfacing, the capture-skip ledger,
the watch-pass heartbeat, and the source↔thread-archive source coverage check.

These guard the *silent* capture failure modes — content consumed without a
trace (soft format drift), sources gone dark without an error, and a wedged
ingest loop indistinguishable from a quiet day. The loud failures (exceptions)
already ride ``watch_errors_last``; everything here exists for failures that
never throw.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from thread_archive import _api as ta
from thread_archive._importers import import_session_incremental
from thread_archive._importers._skip_ledger import LEDGER_FILE, summarize_skips
from thread_archive._ops.coverage import check_coverage
from thread_archive._ops.health import pipeline_verdict, read_health, record_health
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult
from thread_archive._watcher.daemon import Watcher

from .helpers import (
    append_jsonl,
    cc_assistant,
    cc_user,
    import_cc_session,
    write_jsonl,
)

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
    # ``ms`` is this source's cumulative poll time and ``import_ms`` the part of it
    # an import actually ran (the rest being the loop looking for work), beside its
    # yield counters — a stub returns instantly, so only their presence is
    # meaningful here.
    assert src == {
        "checked": 3, "items": 1, "events": 2,
        "lines": 7, "parse_errors": 1, "errors": 0,
        "ms": src["ms"], "import_ms": src["import_ms"],
    }
    assert src["ms"] >= 0
    # A stub imports through no helper, so it is charged no import time — which is
    # the honest reading of a source that did no importing.
    assert src["import_ms"] == 0


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


# ── settled empty sessions: store activity the archive has accounted for ─────

CODEX_REAL = [
    {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"id": "real", "cwd": "/proj"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:01Z",
     "payload": {"type": "user_message", "message": "hello", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
     "payload": {"type": "agent_message", "message": "hi"}},
]
#: What a CLI session opened and never used leaves behind: metadata, no turns.
CODEX_ABANDONED = [
    {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"id": "abandoned", "cwd": "/proj"}},
]


def _codex_store_with_abandoned_session(tmp_path, *, extra_lines=()):
    """A codex store holding one real (imported) session and one abandoned
    session whose store mtime is *now* — the shape that used to read as stale
    ingest. Returns ``(watcher, abandoned_path)``; the abandoned session is
    imported once per call to :func:`append_jsonl`-driven growth in ``extra_lines``,
    so a caller can script repeat consumption."""
    from thread_archive._importers import import_codex_session_incremental
    from thread_archive._watcher.sources import codex_watcher

    sessions = tmp_path / "codex-sessions"
    sessions.mkdir()
    ta.open_archive()

    real = sessions / "real.jsonl"
    write_jsonl(real, CODEX_REAL)
    import_codex_session_incremental(real, "real")
    # A real session's file stopped moving when its last turn landed.
    written = datetime(2026, 1, 1, 10, 0, 2, tzinfo=timezone.utc).timestamp()
    os.utime(real, (written, written))

    abandoned = sessions / "abandoned.jsonl"
    write_jsonl(abandoned, CODEX_ABANDONED)
    import_codex_session_incremental(abandoned, "abandoned")
    for line in extra_lines:
        append_jsonl(abandoned, [line])
        import_codex_session_incremental(abandoned, "abandoned")

    now = time.time()
    os.utime(abandoned, (now, now))
    return codex_watcher(sessions), abandoned


def test_coverage_excuses_a_settled_empty_session(archive_home, tmp_path):
    # The newest thing in the store is a session the importer consumed whole and
    # judged contentless. Nothing was lost, so nothing is stale — and without
    # this, one abandoned session pins the source red until the next real
    # conversation lands.
    watcher, _ = _codex_store_with_abandoned_session(tmp_path)
    r = check_coverage(watchers=[watcher], min_history=1)
    assert r["ok"]
    assert "failed" not in r["sources"]["codex"]
    assert r["sources"]["codex"]["unaccounted_store_latest"] is None


def test_coverage_flags_a_session_consumed_over_and_over(archive_home, tmp_path):
    # A parser gone blind leaves the same trace as an abandoned session — except
    # its session keeps growing, so it is consumed again and again. Repetition is
    # what disqualifies the id, and the drift stays caught.
    watcher, _ = _codex_store_with_abandoned_session(
        tmp_path,
        extra_lines=[
            {"type": "event_msg", "timestamp": "2026-07-01T10:00:00Z",
             "payload": {"type": "unknown_to_this_parser", "message": "real content"}},
        ],
    )
    r = check_coverage(watchers=[watcher], min_history=1)
    assert not r["ok"]
    assert r["sources"]["codex"]["failed"] == "stale_ingest"


def test_coverage_flags_a_store_file_grown_past_its_watermark(archive_home, tmp_path):
    # Settled once, then appended to with no import behind it — a wedged ingest
    # loop. The watermark no longer covers the file's bytes, so it counts as
    # activity again.
    watcher, abandoned = _codex_store_with_abandoned_session(tmp_path)
    append_jsonl(abandoned, [
        {"type": "event_msg", "timestamp": "2026-07-01T10:00:00Z",
         "payload": {"type": "user_message", "message": "never imported", "turn_id": "t9"}},
    ])
    now = time.time()
    os.utime(abandoned, (now, now))
    r = check_coverage(watchers=[watcher], min_history=1)
    assert not r["ok"]
    assert r["sources"]["codex"]["failed"] == "stale_ingest"


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


def test_coverage_warns_on_stale_export_fed_source(archive_home, tmp_path):
    # chatgpt/claude reach the archive only via manual account exports; aging
    # past the window is a coverage hole with no other surface — warn, never red.
    f = tmp_path / "chatgpt-like.jsonl"
    write_jsonl(f, [cc_user("st"), cc_assistant("st")])  # fixture events are old
    ta.open_archive()
    import_session_incremental(f, "st-1", source="chatgpt")
    r = check_coverage(watchers=[])
    assert r["ok"]
    assert r["unwatched"]["chatgpt"]["warning"] == "export_stale"
    assert any("account export is stale" in msg for msg in r["warnings"])

    # A generous-enough window keeps it quiet.
    r = check_coverage(watchers=[], export_stale_days=365 * 50)
    assert "warning" not in r["unwatched"]["chatgpt"]
    assert not r["warnings"]


def test_coverage_ages_export_channel_of_watched_source(archive_home):
    # grok is fed by both the CLI watcher and manual xAI account exports (same
    # source name). Fresh CLI events must not mask an aging export channel: the
    # staleness check keys on export-imported threads (source_metadata.surface
    # = 'web') alone.
    from datetime import datetime, timedelta, timezone

    from thread_archive._store import Event, Thread, get_session, init_db

    ta.open_archive()
    init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as s:
        s.add(Thread(id=1, name="cli-session", source="grok"))
        s.add(Thread(id=2, name="web-export", source="grok",
                     source_metadata={"provider": "grok", "surface": "web"}))
        s.flush()
        s.add(Event(id=1, thread_id=1, stream_id="st", event_type="user_message",
                    payload={"content_text": "fresh cli"}, occurred_at=now))
        s.add(Event(id=2, thread_id=2, stream_id="st", event_type="user_message",
                    payload={"content_text": "old export"},
                    occurred_at=now - timedelta(days=100)))
        s.commit()

    r = check_coverage(watchers=[StubWatcher("grok")])
    assert r["unwatched"]["grok"]["channel"] == "export"
    assert r["unwatched"]["grok"]["warning"] == "export_stale"
    assert any("grok account export is stale" in msg for msg in r["warnings"])

    # The CLI channel's freshness is judged where it always was — per-source
    # newest_event — so the split adds the export view without touching it.
    r = check_coverage(watchers=[StubWatcher("grok")], export_stale_days=365 * 50)
    assert "warning" not in r["unwatched"]["grok"]


def test_coverage_skips_export_channel_with_no_export_history(archive_home, tmp_path):
    # A watched source with no export-imported thread has no export channel to
    # age: CLI-only grok use must not nag for an account export it never had.
    f = tmp_path / "grok-like.jsonl"
    write_jsonl(f, [cc_user("g"), cc_assistant("g")])
    ta.open_archive()
    import_session_incremental(f, "g-1", source="grok")
    r = check_coverage(watchers=[StubWatcher("grok")])
    assert "grok" not in r["unwatched"]
    assert not any("account export" in msg for msg in r["warnings"])


def test_coverage_warns_on_recent_ledger_records(archive_home):
    # Skip/drift ledgers record capture loss that never throws; recent records
    # must surface as coverage warnings (warn, never red).
    import datetime as dt

    from thread_archive._importers._validation_ledger import (
        LEDGER_FILE as DRIFT_FILE,
    )

    ta.open_archive()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    (archive_home / DRIFT_FILE).write_text(
        json.dumps({"at": now_iso, "count": 3}) + "\n")
    (archive_home / LEDGER_FILE).write_text(
        json.dumps({"at": now_iso, "lines_skipped": 2}) + "\n")

    r = check_coverage(watchers=[])
    assert r["ok"]
    assert any("format drift" in msg for msg in r["warnings"])
    assert any("capture skips" in msg for msg in r["warnings"])


def test_coverage_skip_warning_ignores_routine_empty_sessions(archive_home):
    # A steady trickle of empty-session skips (no_importable_content) is routine
    # — it must stay in the ledger and its recent tally but not trip the warning,
    # or the signal drowns. A substantive skip (any other reason) still warns.
    import datetime as dt

    ta.open_archive()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    (archive_home / LEDGER_FILE).write_text(
        json.dumps({"at": now_iso, "reason": "no_importable_content", "lines_skipped": 2}) + "\n"
        + json.dumps({"at": now_iso, "reason": "no_importable_content", "lines_skipped": 2}) + "\n")

    summary = summarize_skips()
    assert summary["recent"] == 2  # still counted for the audit trail
    assert summary["recent_substantive"] == 0

    r = check_coverage(watchers=[])
    assert not any("capture skips" in msg for msg in r["warnings"])

    # A drift-adjacent skip (thread built then discarded) is substantive → warns.
    with open(archive_home / LEDGER_FILE, "a") as fh:
        fh.write(json.dumps(
            {"at": now_iso, "reason": "empty_import_discarded", "lines_skipped": 4}) + "\n")
    assert summarize_skips()["recent_substantive"] == 1
    r = check_coverage(watchers=[])
    assert any("capture skips" in msg for msg in r["warnings"])


def test_coverage_export_staleness_respects_config_opt_out(archive_home, tmp_path):
    import json as _json

    from thread_archive._config import resolve_paths

    f = tmp_path / "chatgpt-like.jsonl"
    write_jsonl(f, [cc_user("od"), cc_assistant("od")])
    ta.open_archive()
    import_session_incremental(f, "od-1", source="chatgpt")
    cfg_path = resolve_paths().home / "config.json"
    cfg_path.write_text(_json.dumps({"sources": {"chatgpt": {"enabled": False}}}))
    r = check_coverage(watchers=[])
    assert "warning" not in r["unwatched"]["chatgpt"]
    assert not r["warnings"]


def test_out_of_band_coverage_run_retires_nightly_stage(archive_home):
    record_health("nightly_last", {"ok": False, "failed_stages": ["coverage"]})
    assert pipeline_verdict()["failed_stages"] == ["coverage"]
    check_coverage(watchers=[])  # green: nothing to check
    verdict = pipeline_verdict()
    assert verdict["ok"]
    assert verdict["recovered_stages"] == ["coverage"]


def test_coverage_failed_sources_carry_degraded_verdicts(archive_home, tmp_path):
    """A coverage FAIL is a degradation verdict outright, persisted (with its
    reason and onset) into health.json's compact record — the state the MCP
    search notice and `thread-archive source fix` key on."""
    import_cc_session(tmp_path, "degv")
    r = check_coverage(
        watchers=[StubWatcher("claude-code", latest=time.time())], min_history=1
    )
    verdict = r["degraded"]["claude-code"]
    assert verdict["reason"] == "stale_ingest"
    assert verdict["since"] == r["sources"]["claude-code"]["newest_event_at"]
    assert read_health()["coverage_last"]["degraded"]["claude-code"]["reason"] == (
        "stale_ingest"
    )


def test_coverage_degrades_on_sustained_ledger_volume_only(archive_home):
    """Per-source ledger volume below the threshold warns but does not degrade;
    at the threshold it degrades with the oldest recent record as the onset.
    One benign record must not put a repair prompt in every search result."""
    import datetime as dt

    from thread_archive._importers._validation_ledger import (
        LEDGER_FILE as DRIFT_FILE,
    )

    ta.open_archive()
    now = dt.datetime.now(dt.timezone.utc)
    stamps = [(now - dt.timedelta(hours=3 - i)).isoformat() for i in range(3)]
    rec = lambda at: json.dumps(  # noqa: E731
        {"at": at, "provider": "codex", "source_id": "s", "count": 1}) + "\n"

    (archive_home / DRIFT_FILE).write_text(rec(stamps[0]) + rec(stamps[1]))
    r = check_coverage(watchers=[])
    assert "codex" not in r["degraded"]
    assert any("format drift" in msg for msg in r["warnings"])  # still warns

    with open(archive_home / DRIFT_FILE, "a") as fh:
        fh.write(rec(stamps[2]))
    r = check_coverage(watchers=[])
    assert r["degraded"]["codex"] == {
        "reason": "validation_drift", "since": stamps[0]}

    # substantive skips degrade the same way, without stealing a stronger verdict
    skip = lambda at: json.dumps(  # noqa: E731
        {"at": at, "source": "grok", "source_id": "g",
         "reason": "empty_import_discarded", "lines_skipped": 1}) + "\n"
    (archive_home / LEDGER_FILE).write_text("".join(skip(s) for s in stamps))
    r = check_coverage(watchers=[])
    assert r["degraded"]["grok"]["reason"] == "capture_skips"
    assert r["degraded"]["codex"]["reason"] == "validation_drift"


def test_degraded_source_gets_quarantine_snapshot(archive_home, tmp_path):
    """Coverage's degradation verdict triggers the preservation snapshot: the
    raw store lands under dumps/drift/<source>/ before the provider can prune
    it. StubWatcher can't enumerate files, so a store-backed watcher stands in."""
    from thread_archive._watcher.sources import RglobWatcher

    store = tmp_path / "cc-store"
    store.mkdir()
    (store / "sess.jsonl").write_text('{"drifted": true}\n')

    import_cc_session(tmp_path, "snap")

    def _no_import(path, source_id):
        raise AssertionError("coverage must never import")

    w = RglobWatcher(store, _no_import, lambda f: f.stem, name="claude-code")
    r = check_coverage(watchers=[w], min_history=1)
    assert r["degraded"]["claude-code"]["reason"] == "stale_ingest"
    gen = r["drift_snapshots"]["claude-code"]
    manifest = json.loads((archive_home / "dumps" / "drift" / "claude-code" /
                           gen.rsplit("/", 1)[-1] / "manifest.json").read_text())
    assert manifest["reason"] == "stale_ingest"
    assert manifest["files"][0]["path"].endswith("sess.jsonl")

    # snapshot=False (and a healthy pass) never touches the quarantine
    r = check_coverage(watchers=[w], min_history=1, snapshot=False)
    assert r["drift_snapshots"] == {}


def test_disabled_source_reports_store_activity(archive_home):
    """A source disabled in config stays report-only (never red), but its
    store's current activity must be visible in the report: the sanctioned
    off switch is also the one path by which a config bug could silently
    stop capture, and this block is the only surface where that shows."""
    now = time.time()
    enabled = StubWatcher("live-source", latest=now)
    off = StubWatcher("opted-out", items=7, latest=now)
    report = check_coverage(watchers=[enabled], all_watchers=[enabled, off])
    assert report["ok"]
    assert "opted-out" in report["disabled"]
    entry = report["disabled"]["opted-out"]
    assert entry["store_items"] == 7
    assert entry["store_latest"] is not None
