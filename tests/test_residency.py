"""Warming loads the models; residency is whether the process still has them.

A warmed search server is a large, quiet process, which is what an OS evicts first
under memory pressure. Nothing else the ledger records can see that happen: the
model objects are still constructed, so ``cold`` and ``embed_cold`` stay false, and
``uptime_s`` only grows. The pages are simply gone, and the next query pays to
fault them back.

Two halves here, matching the two halves of the fix:

- the reading that makes it visible — current resident memory beside the peak,
  since a high-water mark never falls and so can never report a loss;
- the loop that prevents it — a touch when the process has gone idle, and
  *nothing at all* when it hasn't, because a server with work in flight is holding
  its own pages down and a touch would only compete with the request.
"""

from __future__ import annotations

import threading

from thread_archive import _retrieval
from thread_archive._ops import machine
from thread_archive._retrieval import _contention, usage

from .helpers import import_cc_session


class TestCurrentResident:
    """``rss_now_mb`` — the reading that can fall."""

    def test_reports_a_plausible_size(self):
        now = machine.rss_now_mb()
        assert now is not None, "no current-RSS reading on a platform the archive runs on"
        # A live CPython with the archive imported is megabytes, not kilobytes, and
        # not the terabytes a unit error would produce.
        assert 1.0 < now < 1_000_000.0

    def test_never_exceeds_the_peak(self):
        """The invariant that makes the pair readable: a high-water mark bounds it."""
        peak, now = machine.rss_mb(), machine.rss_now_mb()
        assert peak is not None and now is not None
        # Slack for the two readings being taken a moment apart, during which this
        # process may legitimately have grown.
        assert now <= peak + 32.0

    def test_tracks_an_allocation(self):
        """It moves with what the process actually holds — the whole point of it.

        A real allocation rather than a stubbed reading: the failure this guards
        against is a unit or field error in the platform struct, which only a live
        number can catch."""
        before = machine.rss_now_mb()
        ballast = bytearray(200 * 1024 * 1024)  # touched below, so genuinely resident
        ballast[::4096] = b"\x01" * len(ballast[::4096])
        after = machine.rss_now_mb()
        del ballast
        assert after - before > 100.0, f"200 MB allocated, reading moved {after - before} MB"


class TestSampleCarriesResidency:
    """The contention sample is where a search picks the pair up."""

    def test_both_readings_are_present(self):
        rec = _contention.sample()
        assert "rss_mb" in rec and "rss_now_mb" in rec
        assert "uptime_s" in rec

    def test_readings_are_numbers(self):
        rec = _contention.sample()
        assert isinstance(rec["rss_now_mb"], float)
        assert rec["rss_now_mb"] > 0


class TestIdleTracking:
    """What the keepalive reads to decide whether it is needed."""

    def test_inflight_counts_a_live_span(self):
        assert _contention.inflight_now() == 0
        with _contention.in_flight():
            assert _contention.inflight_now() == 1
            with _contention.in_flight():
                assert _contention.inflight_now() == 2
            assert _contention.inflight_now() == 1
        assert _contention.inflight_now() == 0

    def test_inflight_returns_to_zero_when_a_span_raises(self):
        """Bookkeeping survives a failing search, or the keepalive stops forever."""
        try:
            with _contention.in_flight():
                raise RuntimeError("search blew up")
        except RuntimeError:
            pass
        assert _contention.inflight_now() == 0

    def test_finishing_work_resets_idle(self):
        with _contention.in_flight():
            pass
        assert _contention.idle_s() < 1.0

    def test_idle_grows_without_work(self):
        with _contention.in_flight():
            pass
        first = _contention.idle_s()
        for _ in range(200000):  # a beat of real work, no sleep in the suite
            pass
        assert _contention.idle_s() >= first


class TestShouldTouch:
    """The keepalive's decision. Cheap on a busy server, active on an idle one."""

    def test_not_while_work_is_in_flight(self):
        """The case that matters most: never compete with the request being served."""
        with _contention.in_flight():
            assert _retrieval._should_touch(0.0) is False

    def test_not_when_retrieval_just_ran(self):
        with _contention.in_flight():
            pass
        assert _retrieval._should_touch(60.0) is False

    def test_yes_once_idle_past_the_interval(self):
        with _contention.in_flight():
            pass
        assert _retrieval._should_touch(0.0) is True

    def test_a_concurrent_search_suppresses_a_due_touch(self):
        """The two conditions compose: idle long enough, but busy right now."""
        started, release = threading.Event(), threading.Event()

        def hold():
            with _contention.in_flight():
                started.set()
                release.wait(30.0)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        assert started.wait(30.0)
        try:
            assert _retrieval._should_touch(0.0) is False
        finally:
            release.set()
            t.join(30.0)
        assert _retrieval._should_touch(0.0) is True


class TestTouchIsActivity:
    """A touch has to reset the idle clock, or the interval stops meaning anything.

    :func:`thread_archive._api.search` does not enter the in-flight span — the tool
    surface does — so a touch that called it bare would leave the process reading
    idle forever after. Every subsequent tick would then find the interval elapsed
    and touch again, collapsing a ninety-second keepalive into one per tick.

    Driven over a real archive and the real search: the suite runs model-free
    (``THREAD_ARCHIVE_EMBED=off``), so the touch takes the lexical path end to end
    without a torch load, which is the whole function under test rather than a
    stand-in for it.
    """

    def test_touch_marks_retrieval_as_having_run(self, tmp_path, archive_home):
        import_cc_session(tmp_path)
        # Idle by construction: nothing has run retrieval in this process yet.
        assert _retrieval._should_touch(0.0) is True
        _retrieval._keepalive_touch()
        # And now it is not due again until the interval has passed afresh. This is
        # the assertion that fails if the touch skips the in-flight span, since the
        # span's exit is the only thing that marks retrieval as having run.
        assert _retrieval._should_touch(60.0) is False

    def test_touch_leaves_nothing_in_flight(self, tmp_path, archive_home):
        """One touch cannot wedge the counter above zero and suppress every later
        one — nor make a real search beside it report a crowd that has gone."""
        import_cc_session(tmp_path)
        _retrieval._keepalive_touch()
        assert _contention.inflight_now() == 0

    def test_touch_writes_no_usage_row(self, tmp_path, archive_home):
        """Synthetic traffic must stay out of the population every retrieval report
        is computed over — the ledger is the query set the replay bench draws from,
        and a keepalive in it would be a query no agent ever asked."""
        import_cc_session(tmp_path)
        ledger = archive_home / usage.LEDGER_FILE
        before = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
        _retrieval._keepalive_touch()
        after = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
        assert after == before


class TestKeepaliveInterval:
    """Configuration, including the operator's off switch."""

    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv(_retrieval.KEEPALIVE_ENV, raising=False)
        assert _retrieval.keepalive_interval_s() == _retrieval.DEFAULT_KEEPALIVE_S

    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv(_retrieval.KEEPALIVE_ENV, "30")
        assert _retrieval.keepalive_interval_s() == 30.0

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv(_retrieval.KEEPALIVE_ENV, "0")
        assert _retrieval.keepalive_interval_s() == 0.0
        assert _retrieval.start_keepalive() is None

    def test_negative_disables(self, monkeypatch):
        """Clamped rather than honoured: a negative interval would touch every tick."""
        monkeypatch.setenv(_retrieval.KEEPALIVE_ENV, "-5")
        assert _retrieval.keepalive_interval_s() == 0.0
        assert _retrieval.start_keepalive() is None

    def test_unparseable_falls_back_to_the_default(self, monkeypatch):
        """A typo in a launchd plist must not silently disable residency."""
        monkeypatch.setenv(_retrieval.KEEPALIVE_ENV, "ninety")
        assert _retrieval.keepalive_interval_s() == _retrieval.DEFAULT_KEEPALIVE_S

    def test_started_thread_is_a_daemon(self, monkeypatch):
        """It must never hold up interpreter exit."""
        monkeypatch.setenv(_retrieval.KEEPALIVE_ENV, "3600")
        thread = _retrieval.start_keepalive()
        assert thread is not None
        assert thread.daemon and thread.is_alive()
