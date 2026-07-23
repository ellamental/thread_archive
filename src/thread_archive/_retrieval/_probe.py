"""Per-search stage-timing probe — opt-in, fail-soft, zero-cost when unused.

A search's wall-clock splits across a few stages: the lexical FTS arm, the
semantic vector arm, and the cross-encoder re-rank. The usage ledger records the
*total* latency already; this probe lets it also record where that time went,
without :func:`thread_archive._retrieval.search` growing a second return value or
the timing points caring whether anyone is listening.

The contract is a context-local slot: a caller that wants a breakdown installs a
:class:`SearchProbe` (``with install() as probe:``) and reads it after; the
search records stage times into whatever probe is current. No probe installed →
:func:`current` is ``None`` and every record point is a cheap ``is None`` check,
so a search that nobody is measuring is never slowed. A context variable (not a
thread-local) so the slot is correct whether the surface runs sync or on an event
loop, and an inner search can't see an outer's probe by accident.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional

_CURRENT: contextvars.ContextVar[Optional["SearchProbe"]] = contextvars.ContextVar(
    "thread_archive_search_probe", default=None
)


class SearchProbe:
    """The mutable stage-timing accumulator one search fills.

    Times accumulate (``+=``) so a widen retry — two engine passes under one
    installed probe — records the work as the caller felt it, summed; the scalar
    facts (``did_rerank``, ``pool_size``, ``cold``) take the last pass's value,
    the one whose hits are returned.
    """

    __slots__ = ("fts_ms", "semantic_ms", "rerank_ms", "did_rerank", "pool_size", "cold")

    def __init__(self) -> None:
        self.fts_ms = 0.0
        self.semantic_ms = 0.0
        self.rerank_ms = 0.0
        self.did_rerank = False
        self.pool_size = 0
        self.cold = False

    def as_record(self) -> dict:
        """The ledger fields: per-stage milliseconds plus the scalar facts. ``cold``
        rides along only when true (it is the exception — the cold-model tail — so
        its presence, not a ``false`` on every warm record, carries the signal)."""
        rec: dict = {
            "fts_ms": round(self.fts_ms, 1),
            "semantic_ms": round(self.semantic_ms, 1),
            "rerank_ms": round(self.rerank_ms, 1),
            "did_rerank": self.did_rerank,
            "pool_size": self.pool_size,
        }
        if self.cold:
            rec["cold"] = True
        return rec


def current() -> Optional[SearchProbe]:
    """The probe the current context installed, or ``None`` — the guard every
    timing point checks so an unmeasured search pays nothing."""
    return _CURRENT.get()


@contextmanager
def install() -> Iterator[SearchProbe]:
    """Install a fresh probe for the duration of the block and yield it to read
    after. Restores the prior slot on exit, so probes nest without leaking."""
    probe = SearchProbe()
    token = _CURRENT.set(probe)
    try:
        yield probe
    finally:
        _CURRENT.reset(token)
