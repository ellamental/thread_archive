"""Sharing one expensive computation between the callers that want it at once.

The case this exists for is a computation that gets *slower* when it overlaps
itself — a directory walk, where two at once contend for one disk and take several
times longer than either alone. A polled endpoint produces exactly that overlap,
and left alone it compounds: load makes it slower, slower widens the overlap
window, wider makes it slower again.

Driven here over a real computation with a real barrier rather than through the
caller that needs it, because the contract is about *concurrency* and the only way
to pin that honestly is to make threads genuinely collide on purpose.
"""

from __future__ import annotations

import threading

import pytest

from thread_archive._ops.shared_work import SharedWork


def test_overlapping_callers_compute_once():
    """The whole point: four callers that arrive while a computation is in flight
    get its result instead of starting three more of them."""
    work = SharedWork()
    inside = threading.Event()
    release = threading.Event()
    runs = []

    def slow() -> str:
        runs.append(1)
        inside.set()
        release.wait(5.0)   # held open so the others are provably still waiting
        return "measured"

    out: list[str] = []
    lock = threading.Lock()

    def ask() -> None:
        got = work.get("k", slow)
        with lock:
            out.append(got)

    first = threading.Thread(target=ask)
    first.start()
    assert inside.wait(5.0), "the first computation never started"

    rest = [threading.Thread(target=ask) for _ in range(3)]
    for t in rest:
        t.start()
    # Everyone else is now queued behind a computation that has not returned.
    assert len(runs) == 1

    release.set()
    for t in [first, *rest]:
        t.join(5.0)

    assert out == ["measured"] * 4
    assert len(runs) == 1, f"{len(runs)} computations for 4 overlapping callers"
    assert work.stats() == {"entries": 1, "computed": 1, "shared": 3}


def test_sharing_in_flight_work_needs_no_staleness_budget():
    """A result that lands while a caller is queued is still newer than the moment
    that caller asked, so the default budget of zero shares it. Opting in is only
    ever needed to reuse something computed *before* the call."""
    work = SharedWork()
    runs = []
    inside, release = threading.Event(), threading.Event()

    def slow() -> int:
        runs.append(1)
        inside.set()
        release.wait(5.0)
        return 7

    t = threading.Thread(target=lambda: work.get("k", slow))
    t.start()
    assert inside.wait(5.0)
    joiner = threading.Thread(target=lambda: work.get("k", slow))  # zero budget
    joiner.start()
    release.set()
    t.join(5.0)
    joiner.join(5.0)

    assert len(runs) == 1, "a zero budget refused an in-flight result"


def test_a_sequential_caller_recomputes_unless_it_says_otherwise():
    """The default is to compute. A caller that just changed the thing being
    measured and asked what changed must not be handed the number from before."""
    work = SharedWork()
    n = iter(range(100))

    assert work.get("k", lambda: next(n)) == 0
    assert work.get("k", lambda: next(n)) == 1, "a sequential call reused a result"
    # A budget reaches back to the result already on hand rather than making one.
    assert work.get("k", lambda: next(n), max_age_s=60) == 1, "the budget was ignored"
    assert work.get("k", lambda: next(n), max_age_s=60) == 1
    assert work.get("k", lambda: next(n)) == 2, "the default stopped computing"


def test_keys_do_not_block_each_other():
    """One key's slow computation must not stall another's — the lock is per key,
    not one gate over the whole thing."""
    work = SharedWork()
    held = threading.Event()
    release = threading.Event()

    def blocked() -> str:
        held.set()
        release.wait(5.0)
        return "a"

    slow = threading.Thread(target=lambda: work.get("a", blocked))
    slow.start()
    assert held.wait(5.0)
    assert work.get("b", lambda: "b") == "b"   # would hang on a single global lock
    release.set()
    slow.join(5.0)


def test_a_raised_computation_reaches_only_its_own_caller():
    """A computation that raced a deletion should not hand its exception to
    everyone waiting — they find nothing stored and try it themselves, which is the
    safe direction."""
    work = SharedWork()

    def boom() -> None:
        raise OSError("vanished mid-walk")

    with pytest.raises(OSError):
        work.get("k", boom)
    assert work.get("k", lambda: "recovered") == "recovered"
    assert work.stats()["computed"] == 1   # the failure stored nothing


def test_entries_and_locks_are_bounded():
    """One process usually has one key. The bound is what stops a caller sweeping
    many from growing this without limit."""
    work = SharedWork(max_entries=2)
    for i in range(6):
        work.get(f"k{i}", lambda i=i: i)
    assert work.stats()["entries"] == 2
    assert len(work._locks) == 2


def test_clear_drops_results_and_counters():
    work = SharedWork()
    work.get("k", lambda: 1)
    work.get("k", lambda: 2, max_age_s=60)
    assert work.stats() == {"entries": 1, "computed": 1, "shared": 1}
    work.clear()
    assert work.stats() == {"entries": 0, "computed": 0, "shared": 0}
