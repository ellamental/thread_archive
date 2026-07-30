"""The parts of the machine that were doing work nothing wrote down.

Four numbers this product produced and discarded, each the denominator or the
explanation for a number it did keep:

- what the *machine* was doing while a call ran (the contention sample recorded
  only this process's own facts, so a box under load and an idle one wrote the
  same row);
- what a **background rebuild** cost (a search beside one carried ``refreshing``,
  which names the rebuild and says nothing about it);
- what a **quiet poll pass** cost (rows are written only for passes that
  imported, so the loop's floor lived in counters a restart erases);
- what the **serving layer** costs *normally* (only the expensive calls crossed
  the floor, leaving a tail with no body under it, and the CLI door recorded
  nothing at all).

Every one is advisory telemetry, so each test also pins the property that makes
it safe to add: it must never be able to break the operation it describes.
"""

from __future__ import annotations

import json

import pytest

from thread_archive._retrieval import _contention, usage
from thread_archive._watcher import ingest_log


def _rows(home, kind):
    path = home / usage.LEDGER_FILE
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [r for r in rows if r.get("kind") == kind]


# --- the machine's own state -------------------------------------------------


def test_the_sample_carries_the_machine_not_just_the_process() -> None:
    """``load1`` and ``rss_mb`` ride every sample, like ``uptime_s``.

    They are the fields with no reading that means *nothing to report*: a quiet
    box is the single most useful thing a slow search can tell you, and omitting
    it would make "quiet" and "not measured" the same absence."""
    rec = _contention.sample()
    assert rec["uptime_s"] >= 0.0
    assert rec["load1"] >= 0.0
    assert rec["rss_mb"] > 0.0


def test_resident_memory_is_reported_in_megabytes() -> None:
    """The unit ``ru_maxrss`` reports is the one portable thing about it —
    kilobytes on Linux, bytes on the BSDs — and getting it wrong is a
    thousand-fold error in a number nobody would double-check.

    A live interpreter with the retrieval stack imported is tens to hundreds of
    MB. Either mis-scaling lands orders of magnitude outside that."""
    assert 1.0 < _contention.sample()["rss_mb"] < 100_000.0


# --- background rebuilds -----------------------------------------------------


def test_a_rebuild_records_what_it_cost_and_how_big_it_was(archive_home) -> None:
    usage.record_refresh("graph", duration_ms=1234.5,
                         detail={"threads": 40, "edges": 90},
                         context={"uptime_s": 12.0, "load1": 3.0})
    (row,) = _rows(archive_home, "refresh")
    assert row["what"] == "graph"
    assert row["duration_ms"] == 1234.5
    # The size travels with the duration: a rebuild that costs more may simply be
    # doing more, and only the size says which.
    assert row["threads"] == 40 and row["edges"] == 90
    # And the process identity, which is what lets the rebuild's window be laid
    # over the searches it overlapped.
    assert row["uptime_s"] == 12.0


def test_a_failed_rebuild_is_still_recorded(archive_home) -> None:
    """A rebuild that raised burned time before it did, and dropping it would
    flatter every percentile computed over these rows."""
    usage.record_refresh("matrix", duration_ms=90.0, failed=True)
    (row,) = _rows(archive_home, "refresh")
    assert row["failed"] is True and row["duration_ms"] == 90.0


def test_refresh_rows_are_disabled_with_the_rest_of_the_ledger(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_USAGE_LOG", "0")
    usage.record_refresh("graph", duration_ms=1.0)
    assert _rows(archive_home, "refresh") == []


# --- the poll loop's floor ---------------------------------------------------


def test_the_quiet_loop_is_rolled_up_not_dropped(archive_home) -> None:
    """One row per window, carrying what the passes cost between them."""
    ingest_log.record_idle(home=archive_home, passes=60, total_ms=900.0,
                           max_ms=45.0, window_s=300.0, checked=1200,
                           load1_avg=3.5)
    rows = [json.loads(ln) for ln in
            (archive_home / ingest_log.LEDGER_FILE).read_text(encoding="utf-8").splitlines()]
    (row,) = [r for r in rows if r.get("kind") == "idle"]
    assert row["passes"] == 60 and row["total_ms"] == 900.0
    # The worst pass, because a loop that is usually instant and occasionally
    # stalls averages out to healthy.
    assert row["max_ms"] == 45.0
    assert row["window_s"] == 300.0 and row["checked"] == 1200
    # And the machine, averaged over the window's passes — a pass costs what it
    # costs partly for how many files it stats and partly for what else the box
    # was doing, and those want opposite responses.
    assert row["load1_avg"] == 3.5


def test_an_empty_window_writes_nothing(archive_home) -> None:
    ingest_log.record_idle(home=archive_home, passes=0, total_ms=0.0,
                           max_ms=0.0, window_s=300.0, checked=0)
    assert not (archive_home / ingest_log.LEDGER_FILE).exists()


def test_idle_rows_never_land_in_a_source_total(archive_home) -> None:
    """The summary's source rows describe imports. An idle rollup is the absence
    of one, and folding it in would invent a source that polled for nothing."""
    ingest_log.record_idle(home=archive_home, passes=10, total_ms=100.0,
                           max_ms=20.0, window_s=60.0, checked=30)
    summary = ingest_log.summarize(archive_home, hours=24)
    assert summary["sources"] == {}
    assert summary["idle"]["passes"] == 10
    # Per pass is the number to read: totals grow with uptime, the per-pass cost
    # grows with how many files the loop has to look at.
    assert summary["idle"]["per_pass_ms"] == 10.0
    assert summary["idle"]["max_ms"] == 20.0


def test_the_daemon_flushes_a_partial_window_on_the_way_out(archive_home) -> None:
    """A daemon restarted more often than the rollup interval would otherwise
    report no idle cost at all — the restart-erases-everything failure this
    rollup exists to end."""
    from thread_archive._watcher.daemon import Watcher

    d = Watcher(watchers=[], home=str(archive_home))
    d._record_idle(12.0, 3)
    d._record_idle(8.0, 3)
    assert d._idle_passes == 2  # below the rollup interval — nothing written yet
    d._flush_idle()
    summary = ingest_log.summarize(archive_home, hours=24)
    assert summary["idle"]["passes"] == 2
    assert summary["idle"]["max_ms"] == 12.0
    assert d._idle_passes == 0  # and the window restarts empty
    assert d._idle_load_n == 0


def test_the_idle_window_carries_the_machine_it_ran_on(archive_home) -> None:
    """Sampled once per pass and averaged, so the field describes the window
    rather than the instant the row happened to be written."""
    from thread_archive._ops import machine
    from thread_archive._watcher.daemon import Watcher

    if machine.load1() is None:
        pytest.skip("no load average on this platform")

    d = Watcher(watchers=[], home=str(archive_home))
    d._record_idle(5.0, 1)
    d._record_idle(5.0, 1)
    assert d._idle_load_n == 2, "one sample per pass, not one per window"
    d._flush_idle()

    rows = [json.loads(ln) for ln in
            (archive_home / ingest_log.LEDGER_FILE).read_text(encoding="utf-8").splitlines()]
    (row,) = [r for r in rows if r.get("kind") == "idle"]
    assert row["load1_avg"] >= 0.0


def test_a_rollup_failure_does_not_reach_the_poll_loop(archive_home) -> None:
    """A real unwritable ledger, not a stubbed one: the point is that the append
    path's own failure is contained, and a fake writer would prove nothing about
    the writer that runs."""
    from thread_archive._watcher.daemon import Watcher

    (archive_home / ingest_log.LEDGER_FILE).mkdir()  # a directory where the file goes

    d = Watcher(watchers=[], home=str(archive_home))
    d._record_idle(5.0, 1)
    d._flush_idle()  # must not raise
    assert d._idle_passes == 0  # and the window is reset, not carried into the next


# --- the terminal front door -------------------------------------------------


def test_the_cli_records_what_its_own_door_cost(archive_home) -> None:
    """Every number this product publishes about a terminal call has described
    only the part that runs once the process is already up. That omission is not
    small here the way it is over MCP: a served MCP call reuses a warm process and
    a CLI call builds one, and until this row existed the ledger said the two
    doors were the same."""
    from thread_archive import cli
    from thread_archive._store import init_db

    init_db()
    assert cli.main(["search", "anything at all", "--limit", "1"]) == 0

    (row,) = _rows(archive_home, "serve")
    assert row["surface"] == "cli", "an absent surface means MCP — the doors must not merge"
    assert row["tool"] == "thread_search"
    # The whole point: what the door cost is more than what the tool did.
    assert row["served_ms"] >= row["tool_ms"]
    assert row["overhead_ms"] == round(row["served_ms"] - row["tool_ms"], 1)


def test_the_cli_read_door_is_recorded_too(archive_home) -> None:
    from thread_archive import cli
    from thread_archive._store import init_db

    init_db()
    cli.main(["read", "nonexistent-thread-id"])
    (row,) = _rows(archive_home, "serve")
    assert row["tool"] == "thread_read" and row["surface"] == "cli"


def test_the_cli_door_row_survives_a_disabled_ledger(archive_home, monkeypatch) -> None:
    """Telemetry is advisory everywhere else here; the terminal is no exception."""
    from thread_archive import cli
    from thread_archive._store import init_db

    monkeypatch.setenv("THREAD_ARCHIVE_USAGE_LOG", "0")
    init_db()
    assert cli.main(["search", "anything at all", "--limit", "1"]) == 0
    assert _rows(archive_home, "serve") == []
