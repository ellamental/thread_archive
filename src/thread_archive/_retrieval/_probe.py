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

**Arm totals are not an attribution.** The three arm buckets each cover work with
unrelated cost models, and the vector arm is the worst offender: one
``semantic_ms`` covers embedding the query (a model inference, or a tens-of-seconds
cold load), the scope-mask query that can put millions of event ids in play, an
inline KNN-matrix build that reads the whole vector pack off disk, the matvec
itself, and hydrating candidate ids back into hits. A slow arm total names the arm
and nothing else, which is precisely useless in the tail — where the value is. So
the vector arm reports :data:`SEMANTIC_SUBSTAGES` alongside its total, and a slow
search says *which* of those five it was.

The lexical arm hides the same problem behind a smaller number. ``fts_ms`` covers a
*pipeline*: an indexed FTS5 MATCH, the token-AND/token-OR passes behind it, a
full-table substring LIKE that runs only when those left the pool short, a
duplicate-flood re-gather — and then building every returned row into a hit. An
indexed pass and a full-table scan differ by orders of magnitude and the arm total
can't tell them apart, so it reports :data:`FTS_SUBSTAGES` and ``fts_passes``
beside it: whether the index itself was slow, or whether the fallback ladder walked
all the way down to the scan.

``set_ms`` is the fourth stage and the odd one out: the exact-set scan
(:func:`~.fts.matched_threads` / :func:`~.fts.count_matches`) that answers *how
many* rather than *which*. It rides no arm — it runs beside them, on the shapes
that promise a caller a complete enumeration — and its cost scales with the match
list rather than with the pool, so a search whose latency moved into this bucket
moved there for a different reason than any of the three above.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from time import perf_counter
from typing import Iterator, Optional

_CURRENT: contextvars.ContextVar[Optional["SearchProbe"]] = contextvars.ContextVar(
    "thread_archive_search_probe", default=None
)

#: The vector arm's internal split, summing to roughly ``semantic_ms``. Named here
#: so the ledger, the bench (:mod:`thread_archive._ops.speed`), and any analysis
#: over them agree on the set without restating it.
SEMANTIC_SUBSTAGES = ("embed_ms", "scope_ms", "matrix_ms", "knn_ms", "hydrate_ms")

#: The lexical arm's internal split, summing to roughly ``fts_ms``. ``match_ms`` and
#: ``scan_ms`` are its two SQL cost models — an indexed FTS5 MATCH against a
#: full-table substring LIKE, which is why they are separate numbers and not one
#: "sql_ms" — ``rescan_ms`` the whole duplicate-flood re-gather, and ``build_ms`` the
#: Python-side hydration of result rows into hits, which a wide candidate pool makes
#: a real cost rather than a rounding error.
FTS_SUBSTAGES = ("match_ms", "scan_ms", "rescan_ms", "build_ms")


class SearchProbe:
    """The mutable stage-timing accumulator one search fills.

    Times accumulate (``+=``) so a widen retry — two engine passes under one
    installed probe — records the work as the caller felt it, summed; the scalar
    facts (``did_rerank``, ``pool_size``) take the last pass's value, the one
    whose hits are returned. ``fts_passes`` is a tally rather than a state, so it
    sums alongside the times it explains.

    The cold flags are per-arm and set where the load would actually be paid, not
    sampled as one bit at entry: a model that is installed but never invoked (the
    cross-encoder, whenever re-rank is off) is permanently "available and not
    loaded", and folding that into a single flag pins it true forever and names
    nothing. ``embed_cold`` is sampled at entry — the vector arm runs on every
    non-structural query, so an unloaded embedder there means this search pays the
    load — while ``rerank_cold`` is set at the re-rank itself, so an arm that sits
    out contributes no cold signal at all.
    """

    __slots__ = (
        "fts_ms", "semantic_ms", "rerank_ms", "set_ms", "did_rerank", "pool_size",
        "embed_cold", "rerank_cold", "matrix_built", "fts_passes",
        *SEMANTIC_SUBSTAGES, *FTS_SUBSTAGES,
    )

    def __init__(self) -> None:
        self.fts_ms = 0.0
        self.semantic_ms = 0.0
        self.rerank_ms = 0.0
        self.set_ms = 0.0
        self.did_rerank = False
        self.pool_size = 0
        self.embed_cold = False
        self.rerank_cold = False
        self.matrix_built = False
        self.fts_passes = 0
        for name in (*SEMANTIC_SUBSTAGES, *FTS_SUBSTAGES):
            setattr(self, name, 0.0)

    @property
    def ran(self) -> bool:
        """Whether any retrieval work happened under this probe.

        A surface that installs one probe around a whole dispatch — the web
        viewer wraps its router, which serves searches and static assets through
        the same call — needs to tell "this request searched" from "this request
        didn't", so it can attach a breakdown to the former and leave the latter
        a two-field row. An untouched probe is not a search that took no time.
        """
        return bool(self.fts_ms or self.semantic_ms or self.rerank_ms or self.set_ms
                    or self.pool_size)

    @property
    def cold(self) -> bool:
        """Whether either model arm paid a load inside this search — the
        cold-model tail, as one bit for the consumers that only need to separate
        the regimes. Which arm it was rides along beside it."""
        return self.embed_cold or self.rerank_cold

    def as_record(self) -> dict:
        """The ledger fields: per-stage milliseconds plus the scalar facts.

        Each arm's sub-stages ride along only when that arm actually ran — a
        structural or tool-scoped search sits the vector arm out, a pool-cache hit
        sits both out — because explicit zeros would read as *measured and instant*
        rather than *did not happen*. ``cold`` and its per-arm attribution are
        likewise the exception, present only when a load was paid — so their
        presence, not a ``false`` on every warm record, carries the signal."""
        rec: dict = {
            "fts_ms": round(self.fts_ms, 1),
            "semantic_ms": round(self.semantic_ms, 1),
            "rerank_ms": round(self.rerank_ms, 1),
            "did_rerank": self.did_rerank,
            "pool_size": self.pool_size,
        }
        if self.fts_ms:
            for name in FTS_SUBSTAGES:
                rec[name] = round(getattr(self, name), 1)
            rec["fts_passes"] = self.fts_passes
        if self.semantic_ms:
            for name in SEMANTIC_SUBSTAGES:
                rec[name] = round(getattr(self, name), 1)
        if self.set_ms:
            rec["set_ms"] = round(self.set_ms, 1)
        if self.matrix_built:
            rec["matrix_built"] = True
        if self.cold:
            rec["cold"] = True
            if self.embed_cold:
                rec["embed_cold"] = True
            if self.rerank_cold:
                rec["rerank_cold"] = True
        return rec


def current() -> Optional[SearchProbe]:
    """The probe the current context installed, or ``None`` — the guard every
    timing point checks so an unmeasured search pays nothing."""
    return _CURRENT.get()


def record(stage: str, started: float) -> None:
    """Add the milliseconds since ``started`` (a :func:`time.perf_counter` read) to
    ``stage`` on the current probe, if one is installed.

    The record points inside the vector arm sit in a module that must never break
    search, and several are on paths that already swallow their own exceptions —
    so the guard, the arithmetic, and the fail-soft live here once instead of at
    each site."""
    probe = _CURRENT.get()
    if probe is None:
        return
    try:
        setattr(probe, stage, getattr(probe, stage) + (perf_counter() - started) * 1000.0)
    except AttributeError:  # an unknown stage name is a bug, never a broken search
        pass


def bump(name: str) -> None:
    """Add one to counter ``name`` on the current probe, if one is installed — the
    tally counterpart to :func:`record` (``fts_passes``). A pass count is what turns
    a slow arm total into a shape: the same milliseconds mean one thing spent in a
    single index hit and another spent walking a fallback ladder."""
    probe = _CURRENT.get()
    if probe is None:
        return
    try:
        setattr(probe, name, getattr(probe, name) + 1)
    except AttributeError:
        pass


def flag(name: str) -> None:
    """Set boolean ``name`` on the current probe, if one is installed — the scalar
    counterpart to :func:`record` (``matrix_built``, the per-arm cold bits)."""
    probe = _CURRENT.get()
    if probe is None:
        return
    try:
        setattr(probe, name, True)
    except AttributeError:
        pass


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
