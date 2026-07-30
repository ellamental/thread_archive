"""One expensive computation per key, shared by whoever asks for it at once.

The problem this solves is specific: a computation whose cost *rises* when it
overlaps itself. A directory walk is the case here — two walks of one tree do not
take a walk's time each, they contend for the same disk and take several times
longer than either alone — and a polled endpoint is a machine for producing that
overlap. Left alone, load makes the thing slower, which makes the overlap window
wider, which makes it slower still.

So callers that arrive while a computation is in flight wait for it instead of
starting their own. Waiting costs them nothing in freshness: a result that lands
while a caller is queued is still newer than the moment that caller asked, which
is the most any of them could honestly want. That is why the default budget here
is zero rather than some tolerance — sharing an in-flight computation is not a
staleness concession, and a caller has to opt in separately to be served something
computed *before* it asked.

Not a general cache. There is no invalidation beyond age, because the only thing
it is meant to hold is a measurement of something the caller does not control and
cannot be notified about.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable


class SharedWork:
    """Keyed results, computed once per key across concurrent callers.

    ``max_entries`` bounds retained results and the per-key locks beside them,
    least-recently-used evicted first — one process usually has one key, and the
    bound is what keeps a caller that sweeps many from growing this without limit.
    """

    def __init__(self, max_entries: int = 8) -> None:
        self._max_entries = max(1, max_entries)
        self._results: "OrderedDict[Any, tuple[float, Any]]" = OrderedDict()
        self._locks: "OrderedDict[Any, threading.Lock]" = OrderedDict()
        self._guard = threading.Lock()
        self._computed = 0
        self._shared = 0

    def stats(self) -> dict:
        """``{entries, computed, shared}`` — how many computations actually ran and
        how many calls were served by one they did not run.

        A sharing scheme that never shares is pure overhead wearing the shape of an
        optimization, and nothing at the call site tells the two apart."""
        with self._guard:
            return {"entries": len(self._results),
                    "computed": self._computed, "shared": self._shared}

    def _fresh_enough(self, key: Any, floor: float) -> tuple[bool, Any]:
        """``(found, value)`` for a result taken at or after ``floor``."""
        with self._guard:
            entry = self._results.get(key)
            if entry is None or entry[0] < floor:
                return False, None
            self._results.move_to_end(key)
            return True, entry[1]

    def _lock_for(self, key: Any) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            self._locks.move_to_end(key)
            while len(self._locks) > self._max_entries:
                self._locks.popitem(last=False)
            return lock

    def get(
        self, key: Any, compute: Callable[[], Any], *, max_age_s: float = 0.0,
    ) -> Any:
        """The result for ``key``, running ``compute`` only if nothing usable exists.

        ``max_age_s`` is how old a result the caller will accept. The default of
        zero still shares an in-flight computation — the floor is the moment of the
        call, not the moment the last result landed — so opting in is only ever
        needed to reuse something that finished *before* the caller asked.

        ``compute`` runs outside the shared guard and under a per-key lock, so keys
        do not block each other and a slow computation does not stall bookkeeping
        for the rest. An exception propagates to the caller that ran it and to
        nobody else: the others find no result and try it themselves, which is the
        safe direction for a computation that may simply have raced a deletion.
        """
        floor = time.monotonic() - max(0.0, max_age_s)
        found, value = self._fresh_enough(key, floor)
        if found:
            with self._guard:
                self._shared += 1
            return value

        with self._lock_for(key):
            # Re-checked inside the lock: whoever held it was computing this same
            # key, and their result landed while this call queued. Without this the
            # queue serializes into a computation apiece — strictly worse than
            # letting them race, rather than the point of the lock.
            found, value = self._fresh_enough(key, floor)
            if found:
                with self._guard:
                    self._shared += 1
                return value
            value = compute()
            with self._guard:
                self._computed += 1
                self._results[key] = (time.monotonic(), value)
                self._results.move_to_end(key)
                while len(self._results) > self._max_entries:
                    self._results.popitem(last=False)
            return value

    def clear(self) -> None:
        """Drop every result and reset the counters. For a caller that knows the
        thing measured has changed in a way age cannot express."""
        with self._guard:
            self._results.clear()
            self._locks.clear()
            self._computed = self._shared = 0
