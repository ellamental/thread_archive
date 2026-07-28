"""The ingest ledger: one retained row per poll that did work.

The health record holds totals since process start and is throttled to one write
per five minutes, so it cannot answer "when did ingest get slow". This can, and
only if it stays quiet on the empty polls that make up most of the loop's life.
"""

from __future__ import annotations

import json

from thread_archive._importers import _probe
from thread_archive._ops import ledger
from thread_archive._watcher import ingest_log
from thread_archive._watcher.base import WatchResult


def _rows(home):
    return list(ledger.iter_rows(home / ingest_log.LEDGER_FILE))


def test_a_pass_that_did_nothing_writes_no_row(tmp_path) -> None:
    with _probe.install() as probe:
        pass
    ingest_log.record_pass("claude-code", home=tmp_path, probe=probe, pass_ms=3.2)
    assert _rows(tmp_path) == [], (
        "the loop spends most of its life finding nothing; rows for that would "
        "bury the ones that matter"
    )


def test_a_pass_that_imported_records_its_stage_split(tmp_path) -> None:
    with _probe.install() as probe:
        _probe.count("items", 1)
        _probe.count("events", 12)
        with _probe.timed("parse_ms"):
            pass
    ingest_log.record_pass("claude-code", home=tmp_path, probe=probe, pass_ms=41.5,
                           result=WatchResult(events_created=12))
    (row,) = _rows(tmp_path)
    assert row["kind"] == "ingest-pass"
    assert row["source"] == "claude-code"
    assert row["pass_ms"] == 41.5
    assert row["events"] == 12
    assert "parse_ms" in row
    assert "at" in row


def test_pass_ms_and_total_ms_are_both_kept(tmp_path) -> None:
    """Their difference is the loop looking for work rather than doing it — on a
    source with many files and few changes that difference is the whole cost."""
    with _probe.install() as probe:
        _probe.count("items", 1)
        with _probe.timed("read_ms"):
            pass
    ingest_log.record_pass("codex", home=tmp_path, probe=probe, pass_ms=900.0)
    (row,) = _rows(tmp_path)
    assert row["pass_ms"] == 900.0
    assert row["total_ms"] < row["pass_ms"]


def test_errors_ride_as_a_count_not_as_text(tmp_path) -> None:
    with _probe.install() as probe:
        _probe.count("items", 1)
    ingest_log.record_pass(
        "cursor", home=tmp_path, probe=probe, pass_ms=10.0,
        result=WatchResult(errors=["cursor: /secret/path blew up"]),
    )
    (row,) = _rows(tmp_path)
    assert row["errors"] == 1
    assert "secret" not in json.dumps(row)


def test_maintenance_and_embed_rows_land_in_the_same_ledger(tmp_path) -> None:
    ingest_log.record_maintenance(
        home=tmp_path, timings={"ms": 12.0, "lock_ms": 3.0}, counts={"threads_updated": 2},
    )
    ingest_log.record_embed(
        home=tmp_path, embedded=64, elapsed_ms=900.0,
        detail_ms={"encode_ms": 700.0}, pending=64,
    )
    kinds = [r["kind"] for r in _rows(tmp_path)]
    assert kinds == ["maintenance", "embed"]


def test_an_embed_pass_with_nothing_to_do_writes_no_row(tmp_path) -> None:
    ingest_log.record_embed(home=tmp_path, embedded=0, elapsed_ms=2.0,
                            detail_ms={}, pending=0)
    assert _rows(tmp_path) == []


def test_a_backlog_it_could_not_clear_is_recorded_even_at_zero_embedded(tmp_path) -> None:
    # A stopped drain and a caught-up one both embed nothing; only the pending
    # count tells them apart, so that row has to exist.
    ingest_log.record_embed(home=tmp_path, embedded=0, elapsed_ms=5.0,
                            detail_ms={}, pending=5000, capped=True)
    (row,) = _rows(tmp_path)
    assert row["pending"] == 5000 and row["capped"] is True


def test_disabled_by_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_INGEST_LOG", "0")
    with _probe.install() as probe:
        _probe.count("items", 1)
    ingest_log.record_pass("claude-code", home=tmp_path, probe=probe, pass_ms=1.0)
    ingest_log.record_maintenance(home=tmp_path, timings={"ms": 1.0}, counts={})
    assert not (tmp_path / ingest_log.LEDGER_FILE).exists()


def test_a_broken_probe_never_takes_the_poll_loop_down(tmp_path) -> None:
    class Exploding:
        ran = True

        def as_record(self):
            raise RuntimeError("boom")

    ingest_log.record_pass("x", home=tmp_path, probe=Exploding(), pass_ms=1.0)


def test_summarize_ranks_stages_by_where_the_window_actually_went(tmp_path) -> None:
    for _ in range(3):
        with _probe.install() as probe:
            _probe.count("items", 1)
            _probe.count("events", 10)
            probe.parse_ms = 100.0   # steady and unremarkable per pass...
            probe.commit_ms = 250.0  # ...but this is where the time is
        ingest_log.record_pass("claude-code", home=tmp_path, probe=probe, pass_ms=400.0)

    res = ingest_log.summarize(tmp_path, hours=24)
    src = res["sources"]["claude-code"]
    assert src["passes"] == 3 and src["events"] == 30
    assert src["pass_p50_ms"] == 400.0
    assert list(res["stages"])[0] == "commit_ms", "ranked by total time, not by peak"
    assert res["retained_bytes"] > 0


def test_summarize_keeps_upkeep_out_of_a_source_total(tmp_path) -> None:
    with _probe.install() as probe:
        _probe.count("items", 1)
    ingest_log.record_pass("codex", home=tmp_path, probe=probe, pass_ms=5.0)
    ingest_log.record_maintenance(home=tmp_path, timings={"ms": 900.0}, counts={})
    ingest_log.record_embed(home=tmp_path, embedded=32, elapsed_ms=700.0, detail_ms={},
                            pending=32)

    res = ingest_log.summarize(tmp_path, hours=24)
    assert res["sources"]["codex"]["total_s"] == 0.0  # 5ms, not the upkeep beside it
    assert res["maintenance"]["passes"] == 1
    assert res["embed"]["embedded"] == 32


def test_summarize_respects_the_window(tmp_path) -> None:
    with _probe.install() as probe:
        _probe.count("items", 1)
    ingest_log.record_pass("codex", home=tmp_path, probe=probe, pass_ms=5.0)
    assert ingest_log.summarize(tmp_path, hours=24)["sources"]
    # A zero-hour window ends in the future of every recorded row.
    assert ingest_log.summarize(tmp_path, hours=0)["sources"] == {}


def test_summarize_on_an_archive_that_has_ingested_nothing(tmp_path) -> None:
    res = ingest_log.summarize(tmp_path, hours=24)
    assert res["sources"] == {} and res["stages"] == {}
    assert res["retained_bytes"] == 0


def test_parse_errors_and_lag_ride_along_on_the_row(tmp_path) -> None:
    """Both mark the pass's timings as describing degraded work: lines the import
    dropped, and how far behind the loop was running when it started."""
    with _probe.install() as probe:
        _probe.count("items", 1)
    ingest_log.record_pass("claude-code", home=tmp_path, probe=probe, pass_ms=10.0,
                           result=WatchResult(parse_errors=3), lag_s=42.5)
    (row,) = _rows(tmp_path)
    assert row["parse_errors"] == 3
    assert row["lag_s"] == 42.5
    assert "errors" not in row, "zero clean-import errors is absence, not a 0"


def test_summarize_accumulates_maintenance_sub_timings_as_stages(tmp_path) -> None:
    ingest_log.record_maintenance(
        home=tmp_path,
        timings={"ms": 900.0, "lock_ms": 250.0, "snapshot_ms": 100.0},
        counts={"threads": 5, "note": "not-an-int"})
    ingest_log.record_maintenance(
        home=tmp_path, timings={"ms": 100.0, "lock_ms": 50.0}, counts={})
    res = ingest_log.summarize(tmp_path, hours=24)
    assert res["stages"]["lock_ms"] == 300.0
    assert res["stages"]["snapshot_ms"] == 100.0
    assert res["maintenance"]["passes"] == 2


def test_summarize_skips_rows_of_a_kind_it_does_not_know(tmp_path) -> None:
    """A future writer adding a row kind must not corrupt today's summary."""
    from datetime import datetime, timezone

    ledger.append(tmp_path / ingest_log.LEDGER_FILE,
                  {"at": datetime.now(timezone.utc).isoformat(),
                   "kind": "retrieval", "ms": 5000.0},
                  max_bytes=ingest_log.max_bytes())
    res = ingest_log.summarize(tmp_path, hours=24)
    assert res["sources"] == {} and res["stages"] == {}


def test_recorders_never_raise_when_the_ledger_cannot_be_written(tmp_path) -> None:
    """Advisory telemetry: a broken ledger must not break the pass it describes."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")  # home/<ledger> now cannot exist
    ingest_log.record_maintenance(home=blocked, timings={"ms": 1.0}, counts={})
    ingest_log.record_embed(home=blocked, embedded=1, elapsed_ms=1.0, detail_ms={})
    with _probe.install() as probe:
        _probe.count("items", 1)
    ingest_log.record_pass("codex", home=blocked, probe=probe, pass_ms=1.0)


def test_disabling_the_ledger_silences_every_recorder(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_INGEST_LOG", "0")
    with _probe.install() as probe:
        _probe.count("items", 1)
    ingest_log.record_pass("codex", home=tmp_path, probe=probe, pass_ms=1.0)
    ingest_log.record_maintenance(home=tmp_path, timings={"ms": 1.0}, counts={})
    ingest_log.record_embed(home=tmp_path, embedded=1, elapsed_ms=1.0, detail_ms={})
    assert _rows(tmp_path) == []


def test_an_embed_pass_that_did_and_found_nothing_writes_no_row(tmp_path) -> None:
    ingest_log.record_embed(home=tmp_path, embedded=0, elapsed_ms=3.0, detail_ms={})
    assert _rows(tmp_path) == []
