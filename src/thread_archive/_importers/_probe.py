"""Per-import stage-timing probe — opt-in, fail-soft, zero-cost when unused.

Ingest's cost has always been reported as one number per source per poll: the
watcher charges a watcher's whole ``poll()`` to that source's cumulative ``ms``.
That number says *which source* the loop spends its life in and nothing else,
which is the wrong grain for every question worth asking of it. A source whose ms
climbs may be reading enormous files, hashing them twice to prove an append,
re-parsing JSON the archive already has, running a dedup query that now scans
millions of rows, or fsyncing a commit behind a contended lock. Those have
nothing in common — different fixes, different urgency, and only one of them is
about the source at all.

So one import reports where its time went, in the stages it actually runs:

``read_ms``
    The transcript's bytes off disk. Scales with the file, and every poll of a
    live session re-reads the whole thing — a long-running session's transcript
    is read end-to-end on every change to its tail.

``parse_ms``
    Those bytes into line dicts. Also whole-file on every poll, for the same
    reason, and it is the JSON parse rather than the read that usually dominates.

``cursor_ms``
    The append proof (:mod:`._cursor`): a digest of every byte, plus a second
    digest of the prefix under the old watermark whenever the file grew. Two full
    passes over the file to establish that nothing under the cursor changed. It is
    invisible in any per-source total and it is not small.

``normalize_ms``
    The provider's parser turning raw lines into normalized messages — the one
    stage whose cost is the provider's shape rather than the archive's.

``validate_ms``
    Unmodeled-field preservation and the drift check that feeds
    ``validation-drift.jsonl``.

``build_ms``
    Normalized messages into typed events (the event builder). Pure CPU, scales
    with what is *new* rather than with the file.

``dedup_ms``
    The queries against what the thread already holds: the dedup-key membership
    check that keeps a re-import idempotent, the message-level anchor probes on a
    continuation merge, and the open-turn seed that lets a straddling turn rejoin
    its stream. One cost model — all three scale with the *thread's* size rather
    than the poll's, which makes this the stage that degrades as a conversation
    gets long.

``write_ms``
    The event rows themselves.

``fts_ms``
    Indexing those events into the search surface, inside the same transaction.
    Search currency is bought here, on the ingest path, and its price belongs
    beside the write it rides with rather than folded into it.

``commit_ms``
    The transaction: truth-log staging, fsync, index write. The stage a contended
    ingest lock or a busy disk shows up in, and the one that has nothing to do
    with how big the transcript was.

The contract is the search probe's, deliberately: a context-local slot a caller
installs (``with install() as probe:``) and reads after, with every record point a
cheap ``is None`` check when nobody is listening. The stages sit in helpers shared
by every importer — archive's own and every plugin's, which reach them through
:func:`~.._importers._line_stream.line_stream_importer` — so instrumenting them
once covers sources this module has never heard of, and no importer signature
changes to carry a timer.

Unlike a search's arms these run strictly in sequence, so they **do** sum — to
about the wall time of one import, less the orchestration between them. A poll
that imported several files sums their stages, since the probe accumulates.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from time import perf_counter
from typing import Iterator, Optional

_CURRENT: contextvars.ContextVar[Optional["IngestProbe"]] = contextvars.ContextVar(
    "thread_archive_ingest_probe", default=None
)

#: Every stage an import is split into, in the order it runs. Named here so the
#: ledger, the watcher's health record, and any analysis over them agree on the
#: set without restating it.
STAGES = (
    "read_ms", "parse_ms", "cursor_ms", "normalize_ms", "validate_ms",
    "build_ms", "dedup_ms", "write_ms", "fts_ms", "commit_ms",
)

#: The volume facts a stage time is only readable against. A commit that took a
#: second is fine for a hundred thousand events and alarming for three, and the
#: same is true of every stage here — so the denominators travel with the
#: numerators rather than being joined back from a different record.
#:
#: ``items`` is whatever the source imports one of: a transcript file for the
#: file sources, a composer/session/frame row for the ones that scan a live DB.
#: Deliberately not ``files`` — the DB sources have none, and a counter that only
#: half the sources can fill is one no cross-source rate can be computed from.
COUNTERS = ("items", "bytes", "lines", "events")


class IngestProbe:
    """The mutable stage-timing accumulator one import (or one poll) fills.

    Times and counts accumulate, so a probe installed around a whole poll pass
    reports the pass as the loop felt it: every file it read, summed by stage.
    Installed around a single import instead, it reports that import.
    """

    __slots__ = (*STAGES, *COUNTERS)

    def __init__(self) -> None:
        for name in STAGES:
            setattr(self, name, 0.0)
        for name in COUNTERS:
            setattr(self, name, 0)

    @property
    def ran(self) -> bool:
        """Whether any import work happened under this probe.

        A poll that fingerprint-skipped every target did no work — which is not
        the same as work that took no time, and the ledger must not record it as
        an import that cost nothing."""
        return bool(self.items or self.events or any(getattr(self, s) for s in STAGES))

    def total_ms(self) -> float:
        """The stages summed. They run in sequence, so this is the import's own
        cost — against the poll's wall time, the difference is orchestration:
        directory walks, fingerprint stats, and the targets that were skipped."""
        return sum(getattr(self, s) for s in STAGES)

    def as_record(self) -> dict:
        """The ledger fields: per-stage milliseconds plus the volume counters.

        Stages that did not run are omitted rather than written as zero — an
        unchanged file's poll never reaches the parser, and a ``0.0`` there would
        read as *measured and instant* rather than *did not happen*. The counters
        are always present: zero items is a fact worth recording, not an absence.
        """
        rec: dict = {name: getattr(self, name) for name in COUNTERS}
        for name in STAGES:
            value = getattr(self, name)
            if value:
                rec[name] = round(value, 1)
        rec["total_ms"] = round(self.total_ms(), 1)
        return rec


def current() -> Optional[IngestProbe]:
    """The probe the current context installed, or ``None`` — the guard every
    record point checks so an unmeasured import pays nothing."""
    return _CURRENT.get()


def record(stage: str, started: float) -> None:
    """Add the milliseconds since ``started`` (a :func:`time.perf_counter` read) to
    ``stage`` on the current probe, if one is installed.

    The record points sit on the ingest path, which must never break for
    telemetry, so the guard, the arithmetic, and the fail-soft live here once
    instead of at each site."""
    probe = _CURRENT.get()
    if probe is None:
        return
    try:
        setattr(probe, stage, getattr(probe, stage) + (perf_counter() - started) * 1000.0)
    except AttributeError:  # an unknown stage name is a bug, never a broken import
        pass


def count(name: str, n: int = 1) -> None:
    """Add ``n`` to counter ``name`` on the current probe, if one is installed —
    the volume half, without which no stage time can be read."""
    probe = _CURRENT.get()
    if probe is None:
        return
    try:
        setattr(probe, name, getattr(probe, name) + n)
    except AttributeError:
        pass


@contextmanager
def timed(stage: str) -> Iterator[None]:
    """Time the block into ``stage``, charging it even if the block raises.

    Work that fails slowly is the case most worth seeing, and it is exactly the
    case a plain "record after" would drop."""
    started = perf_counter()
    try:
        yield
    finally:
        record(stage, started)


@contextmanager
def install() -> Iterator[IngestProbe]:
    """Install a fresh probe for the duration of the block and yield it to read
    after. Restores the prior slot on exit, so probes nest without leaking."""
    probe = IngestProbe()
    token = _CURRENT.set(probe)
    try:
        yield probe
    finally:
        _CURRENT.reset(token)
