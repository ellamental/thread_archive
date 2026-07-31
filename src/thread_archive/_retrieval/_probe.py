"""Per-search stage-timing probe — opt-in, fail-soft, zero-cost when unused.

A search's work splits across a few stages: the lexical FTS arm, the semantic
vector arm, and the shaping of the pool they fill. The usage ledger records the *total*
latency already; this probe lets it also record where that time went, without
:func:`thread_archive._retrieval.search` growing a second return value or the
timing points caring whether anyone is listening.

Stage times are durations, not shares of the total. The two pool arms run
concurrently, so a search's ``fts_ms`` and ``semantic_ms`` cover overlapping
wall-clock and can sum past the latency the caller waited — which is the point of
recording them apart: what the search waited on is the *slower* of the two, and
only separate numbers say which one that was.

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
many* rather than *which*. It rides no arm — it runs beside them, whenever a
saturated pool means the rows alone cannot say how big the answer was — and its
cost scales with the match list rather than with the pool, so a search whose
latency moved into this bucket moved there for a different reason than any of the
three above.

That stage is memoized, and a duration alone cannot see the memo working: the same
``set_ms`` is a cheap answer on a small corpus and a broken memo on a large one.
:data:`SET_OUTCOMES` counts what the stage actually did — a full scan, a bounded
delta over rows appended since the memoized answer, or a hand-back of that answer
unchanged — three costs that differ by orders of magnitude and are otherwise
indistinguishable in a total. Counters rather than one state because a search can
run the stage twice (the thread tally and the saturated-pool count) and they need
not agree.

Everything above measures how the pool was *found*. :data:`SHAPE_SUBSTAGES`
measures what happens to it afterwards — ranking, the coherence pass, the
same-anchor collapse, the exact-set count, and the per-hit enrichments — and it is
the half of a search that scales with the pool rather than with the corpus. That
distinction is why it is worth its own group: an arm gets slower because the index
or the matrix is slow, and these get slower because the pool is *large*, which a
caller controls through ``limit`` and an index tune cannot touch.

Unlike the arms, these five run strictly in sequence, so they do sum — to the
part of a search's latency that sits after its pool. ``extend_ms`` contains
``set_ms`` (the exact-set scan is the work it times), the one nesting in the set;
the other four are disjoint.
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
#: so the ledger, the bench (``search_lab/speed.py``), and any analysis
#: over them agree on the set without restating it.
SEMANTIC_SUBSTAGES = ("embed_ms", "scope_ms", "matrix_ms", "knn_ms", "hydrate_ms")

#: The lexical arm's internal split, summing to roughly ``fts_ms``. ``match_ms`` and
#: ``scan_ms`` are its two SQL cost models — an indexed FTS5 MATCH against a
#: full-table substring LIKE, which is why they are separate numbers and not one
#: "sql_ms" — ``rescan_ms`` the whole duplicate-flood re-gather, and ``build_ms`` the
#: Python-side hydration of result rows into hits, which a wide candidate pool makes
#: a real cost rather than a rounding error.
FTS_SUBSTAGES = ("match_ms", "scan_ms", "rescan_ms", "build_ms")

#: The post-pool half of a search, in the order it runs. ``rank_ms`` is the weighted
#: lexical ranker, which runs over the *whole* pool rather than just the cut, since
#: the pool is what a walk pages over; ``coherence_ms`` the corpus-graph head re-order; ``group_ms`` the
#: anchor collapse; ``extend_ms`` a saturated pool's exact-set count
#: (and so the outer bound on ``set_ms``); ``enrich_ms`` the per-hit
#: thread columns, titles, and context windows. Sequential, so unlike the arms these
#: sum — to whatever a search spent after its pool was fused.
SHAPE_SUBSTAGES = ("rank_ms", "coherence_ms", "group_ms", "extend_ms", "enrich_ms")

#: What the memoized exact-set stage did, tallied per set query. ``set_scans`` is a
#: full resolve of the match set; ``set_deltas`` a scan bounded to the rows appended
#: since a memoized answer, folded into it; ``set_hits`` an answer handed back
#: because the index had not moved. The three differ by orders of magnitude on a
#: real corpus — measured here, ~850 ms against ~19 ms against ~1 ms — so the split
#: is what says whether a slow ``set_ms`` is a large corpus or a memo that ingest is
#: defeating.
SET_OUTCOMES = ("set_scans", "set_deltas", "set_hits")


class SearchProbe:
    """The mutable stage-timing accumulator one search fills.

    Times accumulate (``+=``) so a widen retry — two engine passes under one
    installed probe — records the work as the caller felt it, summed; the scalar
    fact (``pool_size``) takes the last pass's value, the one
    whose hits are returned. ``fts_passes`` is a tally rather than a state, so it
    sums alongside the times it explains.

    ``embed_cold`` is sampled at entry — the vector arm runs on every
    non-structural query, so an unloaded embedder there means this search pays the
    load. ``embed_cached`` is its opposite end: the query vector came back from the
    embedder's cache, which is why ``embed_ms`` can be ~0 on an arm that ran.
    """

    __slots__ = (
        "fts_ms", "semantic_ms", "set_ms", "pool_size",
        "embed_cold", "embed_cached", "matrix_built", "fts_passes",
        *SEMANTIC_SUBSTAGES, *FTS_SUBSTAGES, *SHAPE_SUBSTAGES, *SET_OUTCOMES,
    )

    def __init__(self) -> None:
        self.fts_ms = 0.0
        self.semantic_ms = 0.0
        self.set_ms = 0.0
        self.pool_size = 0
        self.embed_cold = False
        self.embed_cached = False
        self.matrix_built = False
        self.fts_passes = 0
        for name in (*SEMANTIC_SUBSTAGES, *FTS_SUBSTAGES, *SHAPE_SUBSTAGES):
            setattr(self, name, 0.0)
        for name in SET_OUTCOMES:
            setattr(self, name, 0)

    @property
    def ran(self) -> bool:
        """Whether any retrieval work happened under this probe.

        A surface that installs one probe around a whole dispatch — the web
        viewer wraps its router, which serves searches and static assets through
        the same call — needs to tell "this request searched" from "this request
        didn't", so it can attach a breakdown to the former and leave the latter
        a two-field row. An untouched probe is not a search that took no time.
        """
        return bool(self.fts_ms or self.semantic_ms or self.set_ms
                    or self.pool_size)

    @property
    def cold(self) -> bool:
        """Whether the vector arm paid a model load inside this search — the
        cold-model tail, as one bit for the consumers that only need to separate
        the regimes."""
        return self.embed_cold

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
            "pool_size": self.pool_size,
        }
        if self.fts_ms:
            for name in FTS_SUBSTAGES:
                rec[name] = round(getattr(self, name), 1)
            rec["fts_passes"] = self.fts_passes
        if self.semantic_ms:
            for name in SEMANTIC_SUBSTAGES:
                rec[name] = round(getattr(self, name), 1)
            # Only meaningful on an arm that ran, and only as a presence: it is what
            # separates an ``embed_ms`` of ~0 that reused a vector from one that
            # never embedded at all.
            if self.embed_cached:
                rec["embed_cached"] = True
        if self.set_ms:
            rec["set_ms"] = round(self.set_ms, 1)
        for name in SET_OUTCOMES:
            if getattr(self, name):
                rec[name] = getattr(self, name)
        for name in SHAPE_SUBSTAGES:
            if getattr(self, name):
                rec[name] = round(getattr(self, name), 1)
        if self.matrix_built:
            rec["matrix_built"] = True
        if self.cold:
            rec["cold"] = True
            if self.embed_cold:
                rec["embed_cold"] = True
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
