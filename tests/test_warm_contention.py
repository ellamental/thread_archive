"""A warm pass is a competing caller, and the ledger has to say so.

Warming is the heaviest thing a process ever does — a torch model into resident
memory, the vector pack read end to end, a real search over it. :mod:`._contention`
records a *peak* across everything in flight, and a peak is only true for every
other caller if the heavy ones enter it too: a pass that competes without entering
leaves a search running beside it reporting an idle machine.

The other half is the pass's own row. A stage total says the model took ninety
seconds; it cannot say whether that is the model or the box, and the same load
measures seconds when the box is quiet. Without the sample those are one row.
"""

from __future__ import annotations

import json
import threading

from thread_archive import _retrieval
from thread_archive._retrieval import _contention, usage


class _GateEmbedder:
    """An embedder that blocks inside ``warm()`` until released.

    Passed through :func:`._retrieval.warm_models`'s own ``embedder`` parameter —
    the seam the function documents — so the pass is provably mid-flight while
    another caller enters the span, instead of the test racing a real model load.
    ``warm()`` is the whole contract the embed stage asks of it.
    """

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def warm(self) -> bool:
        self._entered.set()
        self._release.wait(30.0)
        return True


def _rows(home, kind):
    path = home / usage.LEDGER_FILE
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [r for r in rows if r.get("kind") == kind]


def _warm_in_background(embedder) -> threading.Thread:
    t = threading.Thread(target=_retrieval.warm_models, kwargs={"embedder": embedder})
    t.start()
    return t


def test_a_call_beside_a_warm_pass_sees_it(archive_home) -> None:
    """The invariant :mod:`._contention` states: a caller that competes and does not
    enter the span is invisible to everyone else's peak, and the ledger then reads
    idle on a machine that was not."""
    entered, release = threading.Event(), threading.Event()
    t = _warm_in_background(_GateEmbedder(entered, release))
    try:
        assert entered.wait(30.0), "the warm pass never reached its embed stage"
        with _contention.in_flight() as span:
            peak = span.peak
    finally:
        release.set()
        t.join(60.0)

    assert peak >= 2, (
        f"a call running beside a warm pass saw a peak of {peak} — the pass is "
        "competing for the box without counting itself"
    )


def test_the_warm_row_carries_the_conditions_it_ran_under(archive_home) -> None:
    entered, release = threading.Event(), threading.Event()
    t = _warm_in_background(_GateEmbedder(entered, release))
    assert entered.wait(30.0), "the warm pass never reached its embed stage"
    release.set()
    t.join(60.0)

    rows = _rows(archive_home, "warm")
    assert rows, "the pass recorded no row at all"
    row = rows[-1]
    # uptime_s is the one field with no reading that means "nothing to report", so
    # it is the one that must always be there — and it is what joins this row to
    # the searches of the same process (``at - uptime_s`` is the process start).
    assert "uptime_s" in row, f"no contention sample on the warm row: {row}"
    assert isinstance(row["uptime_s"], (int, float))
    assert row["wait_ms"] >= 0.0, "the queue stage is still its own reading"


def test_a_queued_pass_is_not_counted_as_competing(archive_home) -> None:
    """``wait_ms``' job, not the peak's. A pass asleep on the warm lock is doing
    nothing, and counting it in flight would report contention to every search that
    ran beside a *queue* rather than beside work."""
    entered, release = threading.Event(), threading.Event()
    holder = _warm_in_background(_GateEmbedder(entered, release))
    try:
        assert entered.wait(30.0), "the first pass never took its turn"
        # A second pass can only be queued: the first is holding the machine's turn
        # and will not release it until this test says so.
        queued_entered, queued_release = threading.Event(), threading.Event()
        queued = _warm_in_background(_GateEmbedder(queued_entered, queued_release))
        try:
            assert not queued_entered.wait(1.0), "the second pass did not queue"
            with _contention.in_flight() as span:
                peak = span.peak
        finally:
            queued_release.set()
            release.set()
            queued.join(60.0)
    finally:
        release.set()
        holder.join(60.0)

    assert peak == 2, (
        f"peak was {peak}: the queued pass counted itself as competing while it was "
        "asleep on the lock"
    )
