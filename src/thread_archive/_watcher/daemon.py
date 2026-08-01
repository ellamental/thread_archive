"""The watch loop: poll every source, import changes, keep the truth current.

The live-ingest path is **inline-durable** — there is no checkpoint in the hot
loop. Each provider import commits its own transaction, and the truth-log seam
flushes every event (and every new thread's metadata record) to its
``threads/<id>.jsonl`` file *before* that COMMIT (see :mod:`.._truth.jsonl_log`).
So an imported event is durable in the JSONL the instant it lands in SQLite —
nothing waits for a checkpoint.

What a checkpoint still does — rewrite the cross-thread overlay snapshots, run the
thread-metadata-update backstop, rebalance the shard layout — is either irrelevant
to conversation ingest (the overlays don't change; the importer never mutates a
thread row after creation) or rare (rebalance only when the archive crosses ~16k
threads). So instead of rewriting tens of MB of overlay snapshots every few seconds,
the loop runs the **cheap maintenance** form (``checkpoint(snapshots=False)``) on a
slow cadence, and only when something was actually imported since the last pass.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .._importers import _probe as _ingest_probe
from .base import SourceWatcher, WatchResult
from .sources import enabled_watchers

logger = logging.getLogger(__name__)

#: How long the idle-pass rollup accumulates before writing a row
#: (:meth:`WatchDaemon._record_idle`). At the default five-second poll that is a
#: row per ~60 quiet passes — enough resolution to see the loop's floor drift over
#: weeks, few enough rows that the quiet loop stays cheap to keep.
_IDLE_ROLLUP_S = 300.0


class Watcher:
    """Polls a set of source watchers on an interval and imports new content.

    Assumes the archive engine is already initialized (``init_engine`` + ``init_db``)
    — the CLI / library entry point does that before constructing the Watcher.

    ``interval`` is the poll cadence; ``maintenance_interval`` is the (much slower)
    cadence for the cheap upkeep pass (shard rebalance + manifest watermark + the
    metadata-update backstop). Ingest durability does not depend on either cadence.

    With no explicit ``watchers``, the set is the default providers minus any
    the operator disabled in ``<home>/config.json`` (see
    :func:`.sources.enabled_watchers`); ``home`` only locates that config.
    """

    def __init__(
        self,
        watchers: Optional[list[SourceWatcher]] = None,
        *,
        home: Optional[str] = None,
        interval: float = 5.0,
        maintenance_interval: float = 300.0,
        embed: bool = True,
        embed_interval: float = 300.0,
        embed_batch: int = 512,
        code_batches: int = 8,
    ) -> None:
        self.watchers = watchers if watchers is not None else enabled_watchers(home)
        self.home = home
        self.interval = interval
        self.maintenance_interval = maintenance_interval
        # Code-axis fold budget per maintenance pass, in event-id windows. Steady
        # state is one nearly-empty window; the number is what bounds the *first*
        # pass on an archive whose history predates the projection, which walks the
        # whole event log and would otherwise hold the loop for minutes.
        self.code_batches = code_batches
        # Live vector cohost: keep the semantic arm current with ingest so recent
        # threads are findable by *meaning*, not just by keyword. The embed backend
        # loads once and stays warm in this process; each pass is bounded by
        # ``embed_batch`` so a backlog drains over cycles without stalling the loop,
        # and it no-ops without the [embeddings] extra. ``_embed_more`` starts True so
        # a pre-existing backlog drains on its own, then idles until new imports land.
        # ``embed_interval`` paces the *idle probe*; while a backlog is draining
        # (``_backlog`` — the last pass filled its batch) the cohost runs every poll
        # cycle instead, so a bulk write (a summarizer backfill, a re-embed) is
        # searchable in minutes, not hours. Each pass stays batch-bounded, so the
        # shared ingest lock is never held long.
        self.embed_enabled = embed
        self.embed_interval = embed_interval
        self.embed_batch = embed_batch
        self._embed_more = True
        self._backlog = False
        self._stop = False
        # The ingest-owner lock fd (see :mod:`.lazy`), held for the loop's
        # lifetime. None until acquired: the loop re-attempts each pass until it
        # holds it, so a startup lost to a transient holder (a lazy pass mid-
        # flight) still converges to ownership rather than running lockless for
        # the daemon's whole life.
        self._owner_fd: Optional[int] = None
        self._errors_total = 0
        self._errors_recorded_at: Optional[float] = None
        self._stale_errors_cleared = False
        from datetime import datetime, timezone

        self._started_at = datetime.now(timezone.utc).isoformat()
        self._passes = 0
        self._source_totals: dict[str, dict[str, int]] = {}
        self._heartbeat_recorded_at: Optional[float] = None
        # Pass wall time: the last one and the worst one since start. The worst is
        # kept because the heartbeat is throttled to one write per 5 minutes — a
        # last-pass-only number samples whichever pass happened to be running at
        # write time and would miss the slow ones entirely.
        self._pass_ms: Optional[float] = None
        self._pass_ms_max: float = 0.0
        self._lag_s: Optional[float] = None
        # The quiet half of the loop, accumulated between rollup rows: passes that
        # imported nothing, what they cost, and how many targets they looked at.
        # See :func:`.ingest_log.record_idle` for why this is a window rather than
        # a row per pass, and why it exists at all when health.json already counts.
        self._idle_passes = 0
        self._idle_ms = 0.0
        self._idle_max_ms = 0.0
        self._idle_checked = 0
        self._idle_load_sum = 0.0
        self._idle_load_n = 0
        self._idle_window_started = time.monotonic()

    def _record_errors(self, errors: list[str]) -> None:
        """Surface poll errors two ways: the current state, and the durable record.

        ``health.json``'s ``watch_errors_last`` is the *state* — the last five
        messages and a running count, throttled to once a minute so a persistently
        broken source doesn't churn the file every poll. It answers "is ingest
        healthy right now", which is what ``thread-archive status`` and anything
        watching health need, and it is cleared once a poll comes back green.

        Being cleared is why it cannot be the only record. A fault that ran for two
        days and then resolved leaves ``health.json`` saying nothing happened, while
        the conversations it failed to capture are gone from the harness on the
        harness's own schedule. :mod:`.._ops.ingest_errors` keeps the history, folded
        by signature so a fault that fires every poll costs a handful of rows.

        The ledger is written ahead of the throttle: the throttle exists to spare
        ``health.json`` a rewrite, and applying it to the ledger would drop the first
        sighting of a new fault whenever an old one had written recently. Fail-soft
        throughout: recording is advisory and must never take the poll loop down."""
        self._errors_total += len(errors)
        try:
            from .._config import resolve_paths
            from .._ops import ingest_errors

            ingest_errors.record(errors, home=resolve_paths().home)
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record poll errors in the ledger", exc_info=True)
        now = time.monotonic()
        if self._errors_recorded_at is not None and now - self._errors_recorded_at < 60:
            return
        try:
            from .._ops.health import record_health

            record_health("watch_errors_last", {
                "count_since_start": self._errors_total,
                "errors": errors[:5],
            })
            self._errors_recorded_at = now
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.exception("watch: could not record poll errors in health.json")

    def _record_ingest(self, source: str, probe, pass_ms: float, r: WatchResult) -> None:
        """Append this source's poll to the ingest ledger, with its stage split.

        Fail-soft and quiet: a pass that imported nothing writes no row (see
        :mod:`.ingest_log`), so the cost on the common empty poll is one
        attribute read."""
        try:
            from .._config import resolve_paths
            from . import ingest_log

            ingest_log.record_pass(
                source, home=resolve_paths().home, probe=probe, pass_ms=pass_ms,
                result=r, lag_s=self._lag_s,
            )
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record ingest pass", exc_info=True)

    def _record_idle(self, pass_ms: float, checked: int) -> None:
        """Fold one no-work pass into the idle window, flushing it when due.

        Called only for passes that imported nothing — the ones
        :meth:`_record_ingest` deliberately writes no row for."""
        self._idle_passes += 1
        self._idle_ms += pass_ms
        self._idle_max_ms = max(self._idle_max_ms, pass_ms)
        self._idle_checked += checked
        # Per pass, not once per window: the row's cost is only readable against
        # what the box was doing while it was paid, and a reading taken at flush
        # describes the flush.
        from .._ops import machine

        load = machine.load1()
        if load is not None:
            self._idle_load_sum += load
            self._idle_load_n += 1
        if time.monotonic() - self._idle_window_started >= _IDLE_ROLLUP_S:
            self._flush_idle()

    def _flush_idle(self) -> None:
        """Write the accumulated idle window and start a new one.

        Also called on shutdown, so a daemon that stops between rollups still
        accounts for the passes it ran — a daemon restarted more often than the
        rollup interval would otherwise report no idle cost at all, which is the
        restart-erases-everything failure this rollup exists to end.

        Fail-soft: a rollup must never take the poll loop down."""
        window_s = time.monotonic() - self._idle_window_started
        try:
            from .._config import resolve_paths
            from . import ingest_log

            ingest_log.record_idle(
                home=resolve_paths().home,
                passes=self._idle_passes,
                total_ms=self._idle_ms,
                max_ms=self._idle_max_ms,
                window_s=window_s,
                checked=self._idle_checked,
                load1_avg=(self._idle_load_sum / self._idle_load_n
                           if self._idle_load_n else None),
            )
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record idle rollup", exc_info=True)
        # Reset whether or not the write landed: a failed rollup drops one window,
        # where carrying it forward would fold two windows' passes into a rate that
        # matches neither.
        self._idle_passes = 0
        self._idle_ms = 0.0
        self._idle_max_ms = 0.0
        self._idle_checked = 0
        self._idle_load_sum = 0.0
        self._idle_load_n = 0
        self._idle_window_started = time.monotonic()

    def _bump_source(self, name: str, r: WatchResult, ms: float = 0.0,
                     import_ms: float = 0.0) -> None:
        """Fold one watcher's poll result — and the wall time it took — into the
        per-source running totals.

        ``ms`` is cumulative, like every other counter here, because the question it
        answers is cumulative: which source the loop actually spends its time in. A
        source polled 1.5M times for nothing and a source polled twice expensively
        are indistinguishable by counts alone, and only one of them is worth
        making cheaper.

        ``import_ms`` is the part of that time an import actually ran, summed from
        the pass's stage probe. The remainder — ``ms`` minus this — is the loop
        looking for work: directory walks and fingerprint stats over every target,
        paid on every poll whether or not anything changed. On a source with
        thousands of files and a handful of daily changes that remainder is nearly
        all of it, and the two numbers have entirely different fixes."""
        t = self._source_totals.setdefault(name, {
            "checked": 0, "items": 0, "events": 0,
            "lines": 0, "parse_errors": 0, "errors": 0, "ms": 0, "import_ms": 0,
        })
        t["checked"] += r.sources_checked
        t["items"] += r.items_imported
        t["events"] += r.events_created
        t["lines"] += r.lines_processed
        t["parse_errors"] += r.parse_errors
        t["errors"] += len(r.errors)
        t["ms"] = int(t.get("ms", 0) + ms)
        t["import_ms"] = int(t.get("import_ms", 0) + import_ms)

    def _sample_lag(self) -> None:
        """Sample ingest lag: how far behind real time the newest ingested event is.

        Freshness is the ingest side's latency — an archive that indexes an hour late
        is slow in the way that matters to an agent asking about the conversation it
        just had, and no counter here can see it. Read off the last row the store
        wrote (primary-key ordered, so it costs an index seek, not a scan of millions
        of rows) and compared to now.

        Only sampled on a pass that actually imported: on a quiet loop the newest
        event simply ages, which is the machine being idle, not ingest falling behind.
        A bulk backfill of old conversations inflates it for the same reason — the
        number is the age of what was last written, and it is honest about being that.
        Fail-soft; a lag sample must never take the poll loop down."""
        try:
            from datetime import datetime, timezone

            from sqlalchemy import text as sa_text

            from .._store import get_session

            with get_session() as s:
                newest = s.execute(
                    sa_text("SELECT occurred_at FROM events ORDER BY id DESC LIMIT 1")
                ).scalar()
            if not newest:
                return
            if isinstance(newest, str):
                newest = datetime.fromisoformat(newest)
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            self._lag_s = round((datetime.now(timezone.utc) - newest).total_seconds(), 1)
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not sample ingest lag", exc_info=True)

    def _record_pass(self) -> None:
        """Surface ingest liveness into ``health.json`` (``watch_pass_last``),
        throttled like :meth:`_record_errors`. Errors already get recorded, but
        errors alone leave silence ambiguous: a healthy quiet loop and a wedged
        (or dead) one look identical to ``thread-archive status``. This record's age
        disambiguates, and its per-source cumulative counters (checked / items /
        events / lines / parse_errors / errors / ms since process start) are the yield
        accounting a capture audit reads — a source whose ``lines`` climb while
        ``events`` stay flat is a parser gone blind, and one whose ``ms`` climbs while
        its counts stay flat is the loop paying for nothing.

        The pass timings (``pass_ms``, ``pass_ms_max``) and ``lag_s`` carry the
        loop's own latency: how long a sweep takes, how bad the worst has been, and
        how far behind real time the newest ingested event is. Fail-soft: advisory,
        must never take the poll loop down."""
        # Clear-on-green: watch_errors_last is a failure-only record — nothing
        # retires it, so a prior run's error (it persists across restarts) keeps
        # painting `thread-archive status` red under a heartbeat that says the daemon
        # restarted clean hours ago. Once this run has logged a poll with no
        # errors, drop any stale record — at most once per run, and never while
        # this run has actually errored (then the record is current, not stale).
        if not self._stale_errors_cleared and self._errors_total == 0:
            try:
                from .._ops.health import clear_health

                clear_health("watch_errors_last")
                self._stale_errors_cleared = True
            except Exception:  # noqa: BLE001 — advisory; the loop must survive
                logger.exception("watch: could not clear stale watch errors")
        now = time.monotonic()
        if (self._heartbeat_recorded_at is not None
                and now - self._heartbeat_recorded_at < 300):
            return
        try:
            import os

            from .._ops.health import record_health

            rec: dict = {
                "pid": os.getpid(),
                "started_at": self._started_at,
                "passes": self._passes,
                "sources": {
                    name: dict(t)
                    for name, t in sorted(self._source_totals.items())
                    if any(t.values())
                },
            }
            if self._pass_ms is not None:
                rec["pass_ms"] = round(self._pass_ms, 1)
                rec["pass_ms_max"] = round(self._pass_ms_max, 1)
            if self._lag_s is not None:
                rec["lag_s"] = self._lag_s
            record_health("watch_pass_last", rec)
            self._heartbeat_recorded_at = now
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.exception("watch: could not record pass heartbeat in health.json")

    def poll_once(self, phase=None) -> WatchResult:
        """One poll across every available source. Imported events are durable in the
        truth log on commit (inline) — this does not checkpoint.

        ``phase`` (a :class:`~.._ops.load_runs.Phase`) tracks this sweep as a load:
        the pending item count seeds its total up front (so a first import reports an
        ETA, not just a spinner) and each imported file advances it. Defaults to no
        tracking, so the continuous daemon loop runs the identical, untracked path
        and never writes a per-poll ledger row."""
        on_item = None
        if phase is not None:
            phase.total = self._pending_estimate()

            def on_item(r: WatchResult) -> None:  # noqa: E306
                phase.advance(max(1, r.items_imported))
                phase.count("events", r.events_created)
                phase.count("lines", r.lines_processed)
                if r.parse_errors:
                    phase.count("parse_errors", r.parse_errors)

        total = WatchResult()
        pass_started = time.monotonic()
        worked = False
        for w in self.watchers:
            source_started = time.monotonic()
            # One probe per source rather than one per pass: the stage split is only
            # actionable when it names the source that paid it, and a shared probe
            # would blend a DB scan's dedup cost into a file source's parse cost.
            with _ingest_probe.install() as probe:
                try:
                    if not w.is_available():
                        continue
                    # Only thread the progress callback when tracking — the continuous
                    # path calls poll() with no arguments, so a source (a plugin, a
                    # test stub) whose poll() takes no ``on_item`` still works.
                    r = w.poll(on_item=on_item) if on_item is not None else w.poll()
                except Exception as e:  # noqa: BLE001 — a broken source must not stop the loop
                    logger.warning("%s: poll error: %s", w.source_name, e)
                    r = WatchResult(errors=[f"{w.source_name}: poll error: {e}"])
            source_ms = (time.monotonic() - source_started) * 1000.0
            # A source that raised still gets charged its time — one that is slow to
            # fail is a real cost of the loop. (An unavailable source `continue`s
            # above and is charged nothing, which is what it costs.)
            self._bump_source(w.source_name, r, source_ms, probe.total_ms())
            self._record_ingest(w.source_name, probe, source_ms, r)
            worked = worked or bool(probe.ran)
            total = total + r

        self._pass_ms = (time.monotonic() - pass_started) * 1000.0
        self._pass_ms_max = max(self._pass_ms_max, self._pass_ms)
        # The same test :meth:`_record_ingest` applies per source, at the pass
        # level: a pass where no source ran an import is one this ledger would
        # otherwise be silent about, and its cost is the loop's floor.
        if not worked:
            self._record_idle(self._pass_ms, total.sources_checked)
        if total.events_created > 0:
            self._sample_lag()
        self._passes += 1
        self._record_pass()
        if total.errors:
            self._record_errors(total.errors)
        if total.events_created > 0:
            logger.info(
                "watch: imported %d events (%d items) across sources",
                total.events_created, total.items_imported,
            )
        return total

    def _pending_estimate(self) -> Optional[int]:
        """A cheap up-front guess at how many items this sweep will import, for the
        load phase's total — the sum of each available source's stat-only discovery
        count. It is the *store* size, not the not-yet-imported remainder (that would
        need a per-item watermark join): on a fresh home the two are equal, which is
        the first-load case the ETA is for; on an incremental catch-up it overcounts,
        and the already-imported items advance fast as fingerprint skips. None when no
        source can count itself cheaply, so the phase falls back to a bare counter."""
        total = 0
        counted = False
        for w in self.watchers:
            try:
                if not w.is_available():
                    continue
                items = w.discover().items
            except Exception:  # noqa: BLE001 — a discovery failure must not block the poll
                continue
            if items:
                total += items
                counted = True
        return total if counted else None

    def maintain(self) -> dict:
        """Cheap periodic upkeep: shard rebalance + thread-metadata backstop + manifest
        watermark + the thread-meta search docs (titles/summaries → FTS) + the code
        axis (paths/commits → their projections). Deliberately
        *not* the cross-thread overlay snapshots — conversation ingest never changes
        those, so the live path leaves them untouched.

        Timed into ``health.json`` (``watch_maintain_last``), split across its two
        halves. "Cheap" is a property this work has to keep earning: both the manifest
        snapshot and the rebalance sweep scale with the archive rather than with what
        just arrived, which is the shape that turns into a quadratic term as the
        corpus grows. Interval-gating bounds how often that is paid, not how much —
        so the cost itself is worth watching, and a regression here would otherwise
        surface only as the poll loop mysteriously slowing down."""
        from .._retrieval.fts import index_thread_meta
        from .._truth import checkpoint
        from .._truth.maintenance import last_timings

        started = time.monotonic()
        counts = checkpoint(snapshots=False)
        checkpoint_ms = (time.monotonic() - started) * 1000.0
        # The checkpoint's own split (lock wait, snapshots, rebalance, backstop),
        # read from the pass that just ran on this thread.
        checkpoint_split = last_timings()
        # Sync title search docs (diff-based — unchanged threads write
        # nothing). Catches both fresh imports and out-of-band summary writes.
        meta_started = time.monotonic()
        try:
            meta = index_thread_meta()
            if meta:
                counts = {**counts, "thread_meta_docs": meta}
        except Exception as e:  # noqa: BLE001 — upkeep must not kill the loop
            logger.warning("watch: thread-meta index error: %s", e)
        meta_ms = (time.monotonic() - meta_started) * 1000.0
        # The code axis. Bounded per pass: the first pass on an existing archive is
        # a corpus-wide backfill, and the poll loop must not disappear into it — the
        # cursor keeps the remainder for the next pass rather than losing the work.
        code_started = time.monotonic()
        try:
            from .._retrieval.code import refresh_code_index

            folded = refresh_code_index(max_batches=self.code_batches)
            if folded["paths"] or folded["commits"]:
                counts = {**counts, "code_paths": folded["paths"],
                          "code_commits": folded["commits"]}
        except Exception as e:  # noqa: BLE001 — upkeep must not kill the loop
            logger.warning("watch: code index error: %s", e)
            folded = {"done": None}
        code_ms = (time.monotonic() - code_started) * 1000.0
        logger.info("watch: maintenance %s", counts)
        timings = {
            "ms": checkpoint_ms + meta_ms + code_ms,
            "checkpoint_ms": checkpoint_ms,
            "thread_meta_ms": meta_ms,
            "code_ms": code_ms,
            **checkpoint_split,
        }
        try:
            from .._ops.health import record_health

            record_health("watch_maintain_last", {
                **{k: round(v, 1) for k, v in timings.items()},
                "code_backfilling": folded.get("done") is False,
                "counts": {k: v for k, v in counts.items() if isinstance(v, int)},
            })
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record maintenance timing", exc_info=True)
        try:
            from .._config import resolve_paths
            from . import ingest_log

            ingest_log.record_maintenance(
                home=resolve_paths().home, timings=timings, counts=counts,
            )
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record maintenance pass", exc_info=True)
        return counts

    def _embed_due(self, now: float, last_embed: float) -> bool:
        """Cohost gate: something to embed, and either a draining backlog (run
        every poll cycle) or the idle probe interval elapsed."""
        return (self.embed_enabled and self._embed_more
                and (self._backlog or (now - last_embed) >= self.embed_interval))

    @staticmethod
    def _import_state_stamp() -> Optional[tuple]:
        """Cheap change probe over the source-import watermarks: ``(count, max
        last_import_at)``. Every ``upsert_import_state`` bumps ``last_import_at``,
        so this moves on *watermark-only* changes too — an adoption, an empty-
        content cursor advance — which create no events and so never set the
        dirty flag, yet still need the maintenance pass to refresh the
        ``import_state.jsonl`` snapshot (the lost-index recovery seed). ``None``
        when the probe can't run (the loop falls back to the dirty flag alone)."""
        from sqlalchemy import func, select

        from .._store import ImportState, get_session

        try:
            with get_session() as s:
                return tuple(
                    s.execute(
                        select(func.count(), func.max(ImportState.last_import_at))
                    ).one()
                )
        except Exception:  # noqa: BLE001 — a probe failure must not kill the loop
            return None

    def embed_pending(self) -> int:
        """Embed the freshest user/text events still missing a vector (bounded by
        ``embed_batch``). Returns the count embedded — 0 when caught up or when the
        embed backend isn't installed.

        The pass reports itself into ``health.json`` (``watch_embed_last``). This is
        the semantic half of ingest freshness, and it fails silently in a way the
        lexical half does not: if this drain falls behind, every other signal stays
        green — the poll loop is healthy, ``lag_s`` is low, searches return hits —
        and the only symptom is that the *right* hit is missing from the vector arm
        because the conversation was never embedded. A drain that has stopped and one
        that has caught up both embed zero docs per pass; only the pending count
        tells them apart."""
        from .._ops.load_runs import CollectingPhase
        from .._retrieval.vectors import index_events_local

        phase = CollectingPhase()
        started = time.monotonic()
        n = index_events_local(max_events=self.embed_batch, newest_first=True, phase=phase)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if n:
            logger.info("watch: embedded %d new vectors", n)
        self._record_embed(n, elapsed_ms, phase)
        return n

    def _record_embed(self, embedded: int, elapsed_ms: float, phase) -> None:
        """Surface one embed-cohost pass into ``health.json``. Fail-soft: advisory,
        must never take the loop down.

        ``pending`` is what the pass found still missing a vector, capped by
        ``embed_batch`` — so ``capped`` marks the case where the real backlog is
        larger than one pass can see, which is the whole signal that a drain is
        behind rather than caught up. The ``select`` / ``model_load`` / ``encode`` /
        ``write`` split rides along from the drain's own sub-timings, so a slow pass
        says which of the four it was."""
        try:
            rec: dict = {
                "embedded": embedded,
                "ms": round(elapsed_ms, 1),
                "pending": phase.total or 0,
            }
            if phase.total and phase.total >= self.embed_batch:
                rec["capped"] = True
            rec.update(phase.detail_ms())
            chunks = phase.counts.get("chunks_pending")
            if chunks:
                rec["chunks_pending"] = chunks
            age = self._newest_vector_age_s()
            if age is not None:
                rec["newest_vector_age_s"] = age

            from .._ops.health import record_health

            record_health("watch_embed_last", rec)
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record embed pass", exc_info=True)
        try:
            from .._config import resolve_paths
            from . import ingest_log

            ingest_log.record_embed(
                home=resolve_paths().home,
                embedded=embedded,
                elapsed_ms=elapsed_ms,
                detail_ms=phase.detail_ms(),
                pending=phase.total or 0,
                capped=bool(phase.total and phase.total >= self.embed_batch),
            )
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record embed drain", exc_info=True)

    def _newest_vector_age_s(self) -> Optional[float]:
        """How old the newest embedded event is — the vector arm's freshness.

        An index seek, not a scan: ``event_id`` leads ``event_vectors``' primary key.
        Structurally an over-estimate, and honestly so: only the user/text/title/
        summary pools are embedded, so a trailing run of tool events (which never get
        vectors) ages this number without anything being behind. It is a ceiling on
        vector staleness, which is the direction that matters."""
        try:
            from datetime import datetime, timezone

            from sqlalchemy import text as sa_text

            from .._store import get_session

            with get_session() as s:
                newest = s.execute(sa_text(
                    "SELECT e.occurred_at FROM events e WHERE e.id = "
                    "(SELECT max(event_id) FROM event_vectors)"
                )).scalar()
            if not newest:
                return None
            if isinstance(newest, str):
                newest = datetime.fromisoformat(newest)
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            return round((datetime.now(timezone.utc) - newest).total_seconds(), 1)
        except Exception:  # noqa: BLE001 — advisory
            logger.debug("watch: could not sample vector freshness", exc_info=True)
            return None

    def run(self) -> None:
        """Loop forever (until :meth:`stop`): poll every ``interval`` seconds, and run
        maintenance every ``maintenance_interval`` seconds when something was imported
        — or a source watermark moved — since the last maintenance pass.

        Each pass runs under the *shared* reindex lock (see
        :func:`.._truth.try_shared_ingest_lock`): while ``thread-archive index rebuild`` holds it
        exclusive for its build-and-swap, the pass is skipped entirely — poll,
        maintenance, and embed all write to the truth and/or the index, and a write
        landing mid-rebuild would silently miss the swapped-in index. Sources replay
        from their own import state, so skipped passes lose nothing.

        The loop holds the ingest-owner lock (see :mod:`.lazy`) for its whole
        lifetime, so MCP servers' lazy catch-up passes degrade to no-op flock
        probes while a daemon is alive. Advisory: if another owner already holds
        it (a second daemon — a misconfig, or a lazy pass mid-flight) the loop
        logs and runs anyway rather than dying into launchd's restart throttle,
        and keeps re-attempting each pass so a *transient* holder never leaves
        it running lockless for good (see :meth:`_run_loop`)."""
        from .lazy import acquire_ingest_owner

        self._check_schema()
        self._owner_fd = acquire_ingest_owner()
        if self._owner_fd is None:
            logger.warning(
                "watch: another process holds the ingest-owner lock — running anyway"
            )
        try:
            self._run_loop()
        finally:
            self._release_owner()

    def _check_schema(self) -> None:
        """Name a model/index schema mismatch at startup, before it can only be read
        as thousands of failing imports.

        A daemon holds its declared models for its whole lifetime, so an index
        migrated (or rebuilt, or restored) underneath a running one leaves the two
        disagreeing until something restarts the process. Every import then fails on
        the first column the daemon expects and the index lacks — the same opaque
        ``no such column`` per session, per poll, for as long as it takes someone to
        notice. The condition is one PRAGMA sweep to detect and is invisible from the
        symptom, which names a column rather than the disagreement that explains it.

        Advisory, and deliberately non-fatal: capture is the product, a mismatch is
        usually *partial* (an absent column costs the queries that touch it, not all
        of them), and a daemon that refuses to start captures nothing at all. So it
        reports and runs. Recording it under its own health key is what makes it
        legible while it lasts — ``watch_errors_last`` would carry the symptom, and
        only for as long as the errors keep coming."""
        try:
            from .._ops.health import clear_health, record_health
            from .._ops.verify import _verify_schema

            schema = _verify_schema()
            if schema.get("ok"):
                clear_health("schema_mismatch_last")
                return
            missing = {
                k: schema.get(k) or []
                for k in ("missing_tables", "missing_columns",
                          "missing_indexes", "missing_unique_constraints")
                if schema.get(k)
            }
            logger.error(
                "watch: index schema is behind the declared models — imports touching "
                "the missing objects will fail until the index is rebuilt "
                "(`thread-archive index rebuild`). Missing: %s",
                "; ".join(f"{k.removeprefix('missing_')}: {', '.join(v)}" for k, v in missing.items()),
            )
            record_health("schema_mismatch_last", missing)
        except Exception:  # noqa: BLE001 — advisory; a check must not stop capture
            logger.debug("watch: could not check the index schema", exc_info=True)

    def _release_owner(self) -> None:
        """Release the ingest-owner lock if held; idempotent. Closing the fd
        releases the flock. Called from both :meth:`run`'s finally and this
        loop's, so a direct ``_run_loop`` caller (tests) never leaks the fd it
        acquired."""
        if self._owner_fd is not None:
            import os

            os.close(self._owner_fd)
            self._owner_fd = None

    def _run_loop(self) -> None:
        from .._truth import try_shared_ingest_lock
        from .lazy import acquire_ingest_owner

        # _stop is initialized in __init__ and deliberately NOT reset here: run()
        # acquires the owner lock before this call, so the loop is observable as
        # started (a caller can already see the lock held and call stop()) before
        # _run_loop begins. Resetting _stop here would clobber that stop() and
        # leave the daemon running past a join. A fresh run needs a fresh Watcher.
        last_maintenance = time.monotonic()
        last_embed = time.monotonic()
        dirty = False
        last_stamp = self._import_state_stamp()
        consecutive_errors = 0
        try:
            while not self._stop:
                # The whole pass is guarded: an exception escaping here (the lock
                # acquisition's index-swap reconnect, a poll bug) would otherwise
                # exit the process, and launchd's KeepAlive restarts it every few
                # seconds with all source fingerprints reset — a hot re-scan loop on
                # exactly the faults (disk full, index swap mid-flight) most likely
                # to persist. Survive instead, with exponential backoff.
                try:
                    # Take ownership if we don't hold it yet. The daemon owns the
                    # ingest-owner lock for its lifetime; a startup that lost the
                    # race to a transient holder (a lazy pass mid-flight) keeps
                    # re-attempting here until it wins, so lazy passes go back to
                    # degrading to no-op probes. Once held it's a cheap None-check.
                    if self._owner_fd is None:
                        self._owner_fd = acquire_ingest_owner()
                    # Acquire runs the index-swap reconnect (reconnect_if_swapped): if a
                    # reindex replaced index.db — even one that fit entirely inside a
                    # sleep between passes — pooled connections are disposed before this
                    # pass touches the store.
                    with try_shared_ingest_lock() as acquired:
                        if not acquired:
                            logger.info("watch: reindex in progress — skipping ingest pass")
                        else:
                            result = self.poll_once()
                            if result.events_created > 0:
                                dirty = True
                                self._embed_more = True  # new events to embed

                            now = time.monotonic()
                            if (now - last_maintenance) >= self.maintenance_interval:
                                # Run when events were imported (dirty) OR when watermarks
                                # alone moved (see _import_state_stamp) — either changes
                                # state the maintenance snapshot must capture.
                                stamp = self._import_state_stamp()
                                if dirty or (stamp is not None and stamp != last_stamp):
                                    try:
                                        self.maintain()
                                    except Exception as e:  # noqa: BLE001 — upkeep must not kill the loop
                                        logger.warning("watch: maintenance error: %s", e)
                                    dirty = False
                                last_maintenance = now
                                if stamp is not None:
                                    last_stamp = stamp

                            # Vector cohost: embed the freshest missing vectors, bounded per
                            # pass. A filled batch means a backlog — drain again next poll
                            # cycle rather than waiting out the idle interval; once a pass
                            # comes back short, fall back to the slow probe cadence.
                            if self._embed_due(now, last_embed):
                                try:
                                    n = self.embed_pending()
                                    self._backlog = n >= self.embed_batch
                                    self._embed_more = self._backlog
                                except Exception as e:  # noqa: BLE001 — embedding must not kill the loop
                                    logger.warning("watch: embed error: %s", e)
                                    self._backlog = False
                                    self._embed_more = False  # don't hot-loop a persistent failure
                                last_embed = now

                except Exception:  # noqa: BLE001 — the loop must outlive any one pass
                    consecutive_errors += 1
                    delay = min(self.interval * (2 ** min(consecutive_errors, 6)), 300.0)
                    logger.exception(
                        "watch: ingest pass failed (%d in a row) — backing off %.0fs",
                        consecutive_errors, delay,
                    )
                else:
                    consecutive_errors = 0
                    delay = self.interval

                # Sleep in short slices so stop() is responsive.
                slept = 0.0
                while slept < delay and not self._stop:
                    time.sleep(min(0.5, delay - slept))
                    slept += 0.5
        finally:
            # Flush the partial idle window. A daemon restarted more often than the
            # rollup interval would otherwise report no idle cost at all — which is
            # the restart-erases-everything failure this rollup exists to end.
            if self._idle_passes:
                self._flush_idle()
            self._release_owner()

    def stop(self) -> None:
        self._stop = True

    def available(self) -> list[SourceWatcher]:
        return [w for w in self.watchers if w.is_available()]
