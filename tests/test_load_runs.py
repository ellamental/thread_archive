"""The load ledger + live-state file — loading an archive as a tracked event."""

from __future__ import annotations

from pathlib import Path

import pytest

from thread_archive._ops import load_runs

DUMMY = Path("/x")  # a run's home is irrelevant to the arithmetic tests


@pytest.fixture
def home(tmp_path):
    d = tmp_path / "home"
    d.mkdir()
    return d


# ── phase arithmetic (no sleeping — set the clock fields directly) ─────────────
def test_phase_reports_rate_and_eta_from_progress():
    run = load_runs.LoadRun("embed", DUMMY)
    ph = load_runs.Phase(run, "embed", total=100)
    ph.done = 20
    ph.elapsed = 10.0  # 2/s
    snap = ph.snapshot()
    assert snap["rate_per_s"] == 2.0
    # 80 remaining at 2/s → 40s.
    assert snap["eta_s"] == 40.0


def test_phase_eta_absent_without_total_or_progress():
    run = load_runs.LoadRun("embed", DUMMY)
    ph = load_runs.Phase(run, "embed", total=None)
    ph.done = 5
    ph.elapsed = 1.0
    assert "eta_s" not in ph.snapshot()  # a total is what makes progress an ETA


def test_phase_reports_throughput_decay_the_mean_rate_hides():
    """Work whose per-item cost grows with what it has already written (a
    full-table rewrite, a directory walk) keeps a healthy-looking mean while the
    tail crawls. The first/last window comparison is what makes that visible."""
    run = load_runs.LoadRun("import", DUMMY)
    ph = load_runs.Phase(run, "import", total=None)

    # One window's worth of fast progress, then one of slow — windows are closed
    # by rewinding the window clock rather than sleeping.
    ph.done = 1000
    ph._win_t -= load_runs._RATE_WINDOW_S + 1
    ph._sample()
    ph.done += 100
    ph._win_t -= load_runs._RATE_WINDOW_S + 1
    ph._sample()

    snap = ph.snapshot()
    assert snap["slowdown"] == pytest.approx(10.0, rel=0.05)
    assert snap["rate_first_s"] > snap["rate_last_s"]


def test_a_length_sorted_phase_trends_on_work_not_item_count():
    """The embed drain length-sorts on purpose, so its last window holds the
    longest documents in the corpus: items-per-second collapses at the tail for a
    reason that is not cost growth, and an item-count trend reads a healthy phase
    as catastrophically degrading. Trending the unit the encoder consumes —
    chunks — compares like with like."""
    run = load_runs.LoadRun("embed", DUMMY)
    ph = load_runs.Phase(run, "embed", total=None, work_unit="chunks")

    # Window one: 100 small events, one chunk each. Window two: 5 huge events,
    # 20 chunks each. Same chunks per window — the encoder did the same work.
    ph.advance(100, work=100)
    ph._win_t -= load_runs._RATE_WINDOW_S + 1
    ph._sample()
    ph.advance(5, work=100)
    ph._win_t -= load_runs._RATE_WINDOW_S + 1
    ph._sample()

    snap = ph.snapshot()
    assert snap["slowdown"] == pytest.approx(1.0, rel=0.05), "flat work, flat trend"
    assert snap["trend_unit"] == "chunks"  # the rates are not items per second
    assert snap["done"] == 105

    # The same phase measured on item count is the false alarm this replaces.
    naive = load_runs.Phase(run, "embed", total=None)
    naive.advance(100)
    naive._win_t -= load_runs._RATE_WINDOW_S + 1
    naive._sample()
    naive.advance(5)
    naive._win_t -= load_runs._RATE_WINDOW_S + 1
    naive._sample()
    assert naive.snapshot()["slowdown"] == pytest.approx(20.0, rel=0.05)


def test_a_phase_without_a_work_unit_still_trends_on_items():
    """Import has no sub-item unit and no deliberate ordering, so its item count
    *is* the work — the quadratic-ingest signal must keep firing."""
    run = load_runs.LoadRun("import", DUMMY)
    ph = load_runs.Phase(run, "import", total=None)
    ph.advance(1000)
    ph._win_t -= load_runs._RATE_WINDOW_S + 1
    ph._sample()
    ph.advance(100)
    ph._win_t -= load_runs._RATE_WINDOW_S + 1
    ph._sample()

    snap = ph.snapshot()
    assert snap["slowdown"] == pytest.approx(10.0, rel=0.05)
    assert "trend_unit" not in snap


def test_phase_reports_no_decay_before_two_windows():
    run = load_runs.LoadRun("import", DUMMY)
    ph = load_runs.Phase(run, "import", total=None)
    ph.done = 50
    ph._win_t -= load_runs._RATE_WINDOW_S + 1
    ph._sample()  # one window closed — a single sample is not a trend
    assert ph.slowdown() is None
    assert "slowdown" not in ph.snapshot()


def test_phase_detail_and_counts_accumulate():
    run = load_runs.LoadRun("embed", DUMMY)
    ph = load_runs.Phase(run, "embed", total=None)
    ph.mark("encode", 1.5)
    ph.mark("encode", 0.5)  # accumulates
    ph.mark("write", 0.25)
    ph.count("chunks", 10)
    ph.count("chunks", 5)
    snap = ph.snapshot()
    assert snap["detail_s"] == {"encode": 2.0, "write": 0.25}
    assert snap["counts"] == {"chunks": 15}


def test_timed_marks_the_block():
    run = load_runs.LoadRun("embed", DUMMY)
    ph = load_runs.Phase(run, "embed", total=None)
    with ph.timed("select"):
        pass
    assert "select" in ph.detail and ph.detail["select"] >= 0.0


# ── the run: live state + ledger row ──────────────────────────────────────────
def test_run_publishes_live_state_then_appends_a_ledger_row(home):
    with load_runs.load_run("embed", home=home) as run:
        with run.phase("embed", total=4) as ph:
            for _ in range(4):
                ph.advance()
            ph.count("chunks", 8)
        # While the run is live, the state file exists and reads back.
        live = load_runs.read_state(home)
        assert live["kind"] == "embed"
        assert live["phases"][0]["done"] == 4
    # After it ends, a ledger row lands and the state reads ok.
    runs = load_runs.read_runs(home=home)
    assert len(runs) == 1
    row = runs[0]
    assert row["kind"] == "embed" and row["status"] == "ok"
    assert row["phases"][0]["counts"] == {"chunks": 8}
    assert load_runs.read_state(home)["status"] == "ok"


def test_a_failed_run_is_recorded_not_swallowed(home):
    with pytest.raises(ValueError):
        with load_runs.load_run("reindex", home=home):
            raise ValueError("truth torn")
    row = load_runs.read_runs(home=home)[0]
    assert row["status"] == "failed"
    assert "ValueError" in row["error"]  # the failure is the case a count-at-the-end loses
    assert load_runs.read_state(home)["status"] == "failed"


def test_running_state_with_a_dead_writer_reads_as_stalled(home):
    # A load that died mid-phase leaves a 'running' state behind; a dead pid must
    # not read as still-running. Inject the liveness predicate (house style: a seam,
    # not a patched os.kill).
    load_runs._write_atomic(
        load_runs.state_path(home),
        {"kind": "embed", "status": "running", "pid": 4242, "phases": []},
    )
    assert load_runs.read_state(home, alive=lambda pid: True)["status"] == "running"
    assert load_runs.read_state(home, alive=lambda pid: False)["status"] == "stalled"


def test_reporter_is_called_with_snapshots(home):
    seen = []
    with load_runs.load_run("embed", home=home, reporter=seen.append) as run:
        with run.phase("embed", total=2) as ph:
            ph.advance()
            ph.advance()
    assert seen  # got at least the phase-enter/exit forced refreshes
    assert seen[-1]["kind"] == "embed"


def test_reporter_failure_never_breaks_the_run(home):
    def boom(_snap):
        raise RuntimeError("reporter down")

    # The reporter raising must not propagate — telemetry is advisory.
    with load_runs.load_run("embed", home=home, reporter=boom) as run:
        with run.phase("embed", total=1) as ph:
            ph.advance()
    assert load_runs.read_runs(home=home)[0]["status"] == "ok"


def test_disabled_writes_nothing(home, monkeypatch):
    monkeypatch.setenv("THREAD_ARCHIVE_LOAD_LOG", "0")
    with load_runs.load_run("embed", home=home) as run:
        with run.phase("embed", total=1) as ph:
            ph.advance()
    assert not load_runs.state_path(home).exists()
    assert not load_runs.ledger_path(home).exists()
    assert load_runs.read_runs(home=home) == []


def test_null_phase_is_a_silent_no_op():
    # The default a drain gets when nothing tracks it — same interface, records nothing.
    ph = load_runs.NullPhase()
    ph.advance(5)
    ph.mark("encode", 1.0)
    ph.count("chunks", 3)
    with ph.timed("write"):
        pass
    assert ph.done == 5  # advance still moves its own counter (the drain may read it)


def test_collecting_phase_keeps_the_split_without_a_run():
    # Steady-state work reports the same internal split a tracked load does, but a
    # ledger row per batch would be noise — so it accumulates in memory instead.
    ph = load_runs.CollectingPhase()
    ph.total = 64
    with ph.timed("select"):
        pass
    with ph.timed("encode"):
        pass
    ph.count("chunks_pending", 3)
    ph.count("chunks_pending", 4)
    ph.advance(2)

    assert ph.done == 2
    assert ph.counts["chunks_pending"] == 7  # counts accumulate
    assert set(ph.detail) == {"select", "encode"}
    ms = ph.detail_ms()
    assert set(ms) == {"select_ms", "encode_ms"}
    assert all(v >= 0.0 for v in ms.values())


def test_collecting_phase_charges_a_sub_step_that_raised():
    # Work that fails slowly is the case worth seeing, so the timing lands anyway.
    ph = load_runs.CollectingPhase()
    with pytest.raises(RuntimeError):
        with ph.timed("encode"):
            raise RuntimeError("model died")
    assert "encode" in ph.detail


def test_collecting_phase_accumulates_repeated_marks():
    # A drain calls timed('encode') once per batch; the phase reports their sum.
    ph = load_runs.CollectingPhase()
    ph.mark("encode", 1.0)
    ph.mark("encode", 0.5)
    assert ph.detail["encode"] == 1.5
    assert ph.detail_ms()["encode_ms"] == 1500.0


def test_read_runs_newest_first_and_bounded(home):
    for i in range(5):
        with load_runs.load_run(f"k{i}", home=home):
            pass
    runs = load_runs.read_runs(limit=3, home=home)
    assert len(runs) == 3
    assert [r["kind"] for r in runs] == ["k4", "k3", "k2"]  # newest first
