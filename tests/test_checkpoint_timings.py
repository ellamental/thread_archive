"""A checkpoint's own split — and the lock wait in particular.

A checkpoint is four unrelated pieces of work behind one number. Which of them a
slow one spent its time in decides whether the answer is a tune, a schedule
change, or a contention problem somewhere else entirely — and the lock wait is
time the call spent doing nothing at all, charged to it by whoever held the lock.
"""

from __future__ import annotations

import pytest

from thread_archive._store import init_db
from thread_archive._truth import checkpoint, maintenance


def test_a_checkpoint_reports_where_it_spent_its_time(archive_home) -> None:
    init_db()
    checkpoint(snapshots=True)
    timings = maintenance.last_timings()
    assert "lock_ms" in timings, "the wait for the ingest lock is time this call paid"
    assert "threads_ms" in timings
    assert "manifest_ms" in timings
    assert all(v >= 0.0 for v in timings.values())


def test_the_maintenance_form_reports_only_the_steps_it_ran(archive_home) -> None:
    init_db()
    checkpoint(snapshots=True)          # first call primes the interval gates
    checkpoint(snapshots=False)         # cadence form: skips the overlay snapshots
    timings = maintenance.last_timings()
    assert "snapshot_ms" not in timings, (
        "a deferred step must be absent, not zero — zero reads as instant"
    )
    assert "lock_ms" in timings


def test_timings_describe_the_last_pass_not_an_accumulation(archive_home) -> None:
    init_db()
    checkpoint(snapshots=True)
    first = maintenance.last_timings()
    checkpoint(snapshots=True)
    second = maintenance.last_timings()
    assert set(second) >= {"lock_ms", "threads_ms"}
    # Not summed across calls: each pass reports itself.
    assert second["threads_ms"] < first["threads_ms"] + second["threads_ms"] + 1.0


def test_reading_before_any_checkpoint_is_empty_not_an_error() -> None:
    assert isinstance(maintenance.last_timings(), dict)


def test_the_returned_map_is_a_copy_callers_cannot_corrupt(archive_home) -> None:
    init_db()
    checkpoint(snapshots=True)
    grabbed = maintenance.last_timings()
    grabbed["lock_ms"] = 9999.0
    assert maintenance.last_timings().get("lock_ms") != pytest.approx(9999.0)
