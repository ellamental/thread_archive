"""One warm pass at a time per machine (``_retrieval._warm_turn``).

Warming loads a torch model, reads the vector pack end to end, and pulls the corpus
graph off disk — none of it shareable, because the model has to end up resident in the
process doing the warming. Several services warm independently and restarts arrive in
bursts, so the passes overlap by default, and overlapping is the expensive case: they
evict each other's page cache and contend for the accelerator, so each pays the others'
thrash on top of its own work.

These cover that the passes queue instead, and — the part that matters more — that
every way the queue can fail still ends in a warmed process. A cold process is the one
outcome worse than a contended one.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time

from thread_archive import _retrieval


def _lock_path(home):
    return home / _retrieval._WARM_LOCK_FILE


def test_an_uncontended_turn_is_taken_immediately(archive_home) -> None:
    with _retrieval._warm_turn() as wait_ms:
        assert wait_ms < 500.0, "nothing was holding the lock; there was nothing to wait for"
    assert _lock_path(archive_home).exists(), "the turn is taken on a file under the home"


def test_a_second_pass_waits_for_the_first(archive_home) -> None:
    """The point of the exercise. Two passes that would otherwise run concurrently run
    one after the other, and the second one's wait is what it spent queued."""
    order: list[str] = []
    started = threading.Event()

    def first():
        with _retrieval._warm_turn():
            order.append("first-in")
            started.set()
            time.sleep(0.5)
            order.append("first-out")

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(5.0), "the first pass never took its turn"

    with _retrieval._warm_turn() as wait_ms:
        order.append("second-in")
    t.join(10.0)

    assert order == ["first-in", "first-out", "second-in"], (
        f"the passes overlapped instead of queueing: {order}"
    )
    assert wait_ms >= 400.0, "the second pass must have waited out the first"


def test_a_wedged_holder_degrades_to_warming_anyway(archive_home) -> None:
    """flock releases on process *death*, so the lock cannot outlive a crash — but a
    holder that is alive and stuck would block forever. The timeout is what turns that
    into the old unserialized behavior instead of a process that never warms."""
    path = _lock_path(archive_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)  # held for the whole block, never released in time
    try:
        entered = False
        t0 = time.perf_counter()
        with _retrieval._warm_turn(timeout_s=0.4, poll_s=0.05) as wait_ms:
            entered = True
        waited = (time.perf_counter() - t0) * 1000.0
        assert entered, "a stuck holder left this process permanently cold"
        assert wait_ms >= 400.0, "it must actually have waited its turn out"
        assert waited < 5_000.0, "the timeout did not bound the wait"
    finally:
        os.close(fd)


def test_a_lock_that_cannot_be_opened_still_warms(archive_home) -> None:
    """Serialization is an optimization, never a gate: if the lock cannot be taken at
    all, the pass runs unserialized rather than not running.

    Staged as a real filesystem refusal — a directory where the lock file goes, which
    ``os.open`` cannot open for writing — rather than by faking the failure."""
    path = _lock_path(archive_home)
    path.mkdir(parents=True, exist_ok=True)

    entered = False
    with _retrieval._warm_turn(timeout_s=0.4) as wait_ms:
        entered = True
    assert entered, "the pass was gated on a lock it could not take"
    assert wait_ms >= 0.0


def test_the_turn_is_released_even_when_the_body_raises(archive_home) -> None:
    """A warm pass that dies mid-stage must not strand the next process behind it."""
    class Boom(Exception):
        pass

    try:
        with _retrieval._warm_turn():
            raise Boom
    except Boom:
        pass

    # Freely takeable again: nothing is still holding it.
    fd = os.open(_lock_path(archive_home), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
