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

from .base import SourceWatcher, WatchResult
from .sources import enabled_watchers

logger = logging.getLogger(__name__)


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

    def _record_errors(self, errors: list[str]) -> None:
        """Surface poll errors into ``<home>/health.json`` (``watch_errors_last``),
        throttled to once a minute so a persistently broken source doesn't churn
        the file every poll. Log lines alone leave a failing source invisible to
        ``thread_archive status`` and anything watching health — a provider format
        change could stall one source's ingest for weeks while everything looks
        green. The record's age is the recency signal; ``count_since_start``
        distinguishes a one-off from a streak. Fail-soft: recording is advisory
        and must never take the poll loop down."""
        self._errors_total += len(errors)
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

    def _bump_source(self, name: str, r: WatchResult, ms: float = 0.0) -> None:
        """Fold one watcher's poll result — and the wall time it took — into the
        per-source running totals.

        ``ms`` is cumulative, like every other counter here, because the question it
        answers is cumulative: which source the loop actually spends its time in. A
        source polled 1.5M times for nothing and a source polled twice expensively
        are indistinguishable by counts alone, and only one of them is worth
        making cheaper."""
        t = self._source_totals.setdefault(name, {
            "checked": 0, "items": 0, "events": 0,
            "lines": 0, "parse_errors": 0, "errors": 0, "ms": 0,
        })
        t["checked"] += r.sources_checked
        t["items"] += r.items_imported
        t["events"] += r.events_created
        t["lines"] += r.lines_processed
        t["parse_errors"] += r.parse_errors
        t["errors"] += len(r.errors)
        t["ms"] = int(t.get("ms", 0) + ms)

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
        (or dead) one look identical to ``thread_archive status``. This record's age
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
        # painting `thread_archive status` red under a heartbeat that says the daemon
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
        for w in self.watchers:
            source_started = time.monotonic()
            try:
                if not w.is_available():
                    continue
                # Only thread the progress callback when tracking — the continuous
                # path calls poll() with its historical signature, so a source
                # (a plugin, a test stub) that hasn't adopted on_item still works.
                r = w.poll(on_item=on_item) if on_item is not None else w.poll()
            except Exception as e:  # noqa: BLE001 — a broken source must not stop the loop
                logger.warning("%s: poll error: %s", w.source_name, e)
                r = WatchResult(errors=[f"{w.source_name}: poll error: {e}"])
            # A source that raised still gets charged its time — one that is slow to
            # fail is a real cost of the loop. (An unavailable source `continue`s
            # above and is charged nothing, which is what it costs.)
            self._bump_source(w.source_name, r, (time.monotonic() - source_started) * 1000.0)
            total = total + r

        self._pass_ms = (time.monotonic() - pass_started) * 1000.0
        self._pass_ms_max = max(self._pass_ms_max, self._pass_ms)
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

        started = time.monotonic()
        counts = checkpoint(snapshots=False)
        checkpoint_ms = (time.monotonic() - started) * 1000.0
        # Sync title/summary search docs (diff-based — unchanged threads write
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
        try:
            from .._ops.health import record_health

            record_health("watch_maintain_last", {
                "ms": round(checkpoint_ms + meta_ms + code_ms, 1),
                "checkpoint_ms": round(checkpoint_ms, 1),
                "thread_meta_ms": round(meta_ms, 1),
                "code_ms": round(code_ms, 1),
                "code_backfilling": folded.get("done") is False,
                "counts": {k: v for k, v in counts.items() if isinstance(v, int)},
            })
        except Exception:  # noqa: BLE001 — advisory; the loop must survive
            logger.debug("watch: could not record maintenance timing", exc_info=True)
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
        :func:`.._truth.try_shared_ingest_lock`): while ``thread_archive reindex`` holds it
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

        self._owner_fd = acquire_ingest_owner()
        if self._owner_fd is None:
            logger.warning(
                "watch: another process holds the ingest-owner lock — running anyway"
            )
        try:
            self._run_loop()
        finally:
            self._release_owner()

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
            self._release_owner()

    def stop(self) -> None:
        self._stop = True

    def available(self) -> list[SourceWatcher]:
        return [w for w in self.watchers if w.is_available()]
