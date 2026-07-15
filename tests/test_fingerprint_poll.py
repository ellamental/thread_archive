"""The shared poll scaffold behind the file- and db-scan watchers.

``fingerprint_poll`` is what every ``FileSessionWatcher`` / ``_DbScanWatcher``
subclass polls through, so its invariants — skip-unchanged, advance-the-
fingerprint-only-after-success, prune-vanished — are pinned here once rather
than re-proved per watcher.
"""

from __future__ import annotations

from thread_archive._watcher.base import WatchResult, fingerprint_poll


def _probe_ok(target):
    # target is (key, fingerprint, ...) — the first two fields drive the scaffold.
    return target[0], target[1]


def test_unchanged_target_is_counted_and_work_is_skipped() -> None:
    seen = {"a": 1}
    calls: list[str] = []

    def work(t):
        calls.append(t[0])
        return WatchResult(events_created=99)

    out = fingerprint_poll(
        [("a", 1)], seen, probe=_probe_ok, work=work,
        on_error=lambda t, e: WatchResult(errors=["x"]),
    )
    assert calls == []  # fingerprint matched → work never ran
    assert out.sources_checked == 1 and out.events_created == 0
    assert seen == {"a": 1}


def test_changed_target_runs_work_and_advances_fingerprint() -> None:
    seen = {"a": 1}

    out = fingerprint_poll(
        [("a", 2)], seen, probe=_probe_ok,
        work=lambda t: WatchResult(sources_checked=1, events_created=3),
        on_error=lambda t, e: WatchResult(errors=["x"]),
    )
    assert out.events_created == 3
    assert seen == {"a": 2}  # advanced only after work returned


def test_work_failure_routes_to_on_error_and_leaves_fingerprint_stale() -> None:
    # The retry invariant: a raising target must NOT advance its fingerprint, so
    # the next poll sees it as still-changed and tries again.
    seen = {"a": 1}

    def work(t):
        raise RuntimeError("boom")

    out = fingerprint_poll(
        [("a", 2)], seen, probe=_probe_ok, work=work,
        on_error=lambda t, e: WatchResult(sources_checked=1, errors=[str(e)]),
    )
    assert out.errors == ["boom"] and out.sources_checked == 1
    assert seen == {"a": 1}  # stale — unchanged, so it retries next poll


def test_vanished_fingerprints_are_pruned() -> None:
    # "b" was seen on a prior poll but isn't among this poll's targets — its
    # fingerprint must not linger (or a re-created "b" would look unchanged).
    seen = {"a": 1, "b": 9}

    fingerprint_poll(
        [("a", 1)], seen, probe=_probe_ok,
        work=lambda t: WatchResult(),
        on_error=lambda t, e: WatchResult(),
    )
    assert seen == {"a": 1}  # "b" pruned, "a" retained


def test_probe_none_skips_silently_without_retaining_a_key() -> None:
    seen: dict[str, int] = {}

    out = fingerprint_poll(
        [("a", 1)], seen, probe=lambda t: None,
        work=lambda t: WatchResult(events_created=5),
        on_error=lambda t, e: WatchResult(errors=["x"]),
    )
    assert out == WatchResult()  # nothing folded
    assert seen == {}  # skipped target left no fingerprint


def test_probe_watchresult_is_folded_and_target_skipped() -> None:
    # A pre-work error (e.g. db unstattable) reports via a folded WatchResult and
    # the target is skipped without ever being fingerprinted.
    seen: dict[str, int] = {}
    calls: list[str] = []

    def work(t):
        calls.append(t[0])
        return WatchResult()

    out = fingerprint_poll(
        [("a", 1)], seen,
        probe=lambda t: WatchResult(errors=["db not found"]),
        work=work, on_error=lambda t, e: WatchResult(),
    )
    assert out.errors == ["db not found"] and calls == []
    assert seen == {}
