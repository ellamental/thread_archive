"""The watch loop: poll every source, import changes, keep the truth current.

The live-ingest path is **inline-durable** — there is no checkpoint in the hot
loop. Each provider import commits its own transaction, and the truth-log seam
flushes every event (and every new thread's metadata record) to its
``threads/<id>.jsonl`` file *before* that COMMIT (see :mod:`..truth.jsonl_log`).
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
from .sources import default_watchers

logger = logging.getLogger(__name__)


class Watcher:
    """Polls a set of source watchers on an interval and imports new content.

    Assumes the archive engine is already initialized (``init_engine`` + ``init_db``)
    — the CLI / library entry point does that before constructing the Watcher.

    ``interval`` is the poll cadence; ``maintenance_interval`` is the (much slower)
    cadence for the cheap upkeep pass (shard rebalance + manifest watermark + the
    metadata-update backstop). Ingest durability does not depend on either cadence.
    """

    def __init__(
        self,
        watchers: Optional[list[SourceWatcher]] = None,
        *,
        interval: float = 5.0,
        maintenance_interval: float = 300.0,
        embed: bool = True,
        embed_interval: float = 300.0,
        embed_batch: int = 512,
    ) -> None:
        self.watchers = watchers if watchers is not None else default_watchers()
        self.interval = interval
        self.maintenance_interval = maintenance_interval
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

    def poll_once(self) -> WatchResult:
        """One poll across every available source. Imported events are durable in the
        truth log on commit (inline) — this does not checkpoint."""
        total = WatchResult()
        for w in self.watchers:
            try:
                if not w.is_available():
                    continue
                total = total + w.poll()
            except Exception as e:  # noqa: BLE001 — a broken source must not stop the loop
                logger.warning("%s: poll error: %s", w.source_name, e)
                total = total + WatchResult(errors=[f"{w.source_name}: poll error: {e}"])

        if total.events_created > 0:
            logger.info(
                "watch: imported %d events (%d items) across sources",
                total.events_created, total.items_imported,
            )
        return total

    def maintain(self) -> dict:
        """Cheap periodic upkeep: shard rebalance + thread-metadata backstop + manifest
        watermark + the thread-meta search docs (titles/summaries → FTS). Deliberately
        *not* the cross-thread overlay snapshots — conversation ingest never changes
        those, so the live path leaves them untouched."""
        from ..retrieval.fts import index_thread_meta
        from ..truth import checkpoint

        counts = checkpoint(snapshots=False)
        # Sync title/summary search docs (diff-based — unchanged threads write
        # nothing). Catches both fresh imports and out-of-band summary writes.
        try:
            meta = index_thread_meta()
            if meta:
                counts = {**counts, "thread_meta_docs": meta}
        except Exception as e:  # noqa: BLE001 — upkeep must not kill the loop
            logger.warning("watch: thread-meta index error: %s", e)
        logger.info("watch: maintenance %s", counts)
        return counts

    def _embed_due(self, now: float, last_embed: float) -> bool:
        """Cohost gate: something to embed, and either a draining backlog (run
        every poll cycle) or the idle probe interval elapsed."""
        return (self.embed_enabled and self._embed_more
                and (self._backlog or (now - last_embed) >= self.embed_interval))

    def embed_pending(self) -> int:
        """Embed the freshest user/text events still missing a vector (bounded by
        ``embed_batch``). Returns the count embedded — 0 when caught up or when the
        embed backend isn't installed."""
        from ..retrieval.vectors import index_events_local

        n = index_events_local(max_events=self.embed_batch, newest_first=True)
        if n:
            logger.info("watch: embedded %d new vectors", n)
        return n

    def run(self) -> None:
        """Loop forever (until :meth:`stop`): poll every ``interval`` seconds, and run
        maintenance every ``maintenance_interval`` seconds when something was imported
        since the last maintenance pass.

        Each pass runs under the *shared* reindex lock (see
        :func:`..truth.try_shared_ingest_lock`): while ``archive reindex`` holds it
        exclusive for its build-and-swap, the pass is skipped entirely — poll,
        maintenance, and embed all write to the truth and/or the index, and a write
        landing mid-rebuild would silently miss the swapped-in index. Sources replay
        from their own import state, so skipped passes lose nothing."""
        from ..truth import try_shared_ingest_lock

        self._stop = False
        last_maintenance = time.monotonic()
        last_embed = time.monotonic()
        dirty = False
        reconnect = False
        while not self._stop:
            with try_shared_ingest_lock() as acquired:
                if not acquired:
                    logger.info("watch: reindex in progress — skipping ingest pass")
                    reconnect = True
                else:
                    if reconnect:
                        # A reindex ran while we skipped: index.db was atomically
                        # replaced, so our pooled connections point at the orphaned
                        # old inode. Dispose them — the next connection reopens the
                        # path and lands on the new file.
                        from ..store import get_engine

                        get_engine().dispose()
                        logger.info("watch: reconnected to the reindexed index")
                        reconnect = False
                    result = self.poll_once()
                    if result.events_created > 0:
                        dirty = True
                        self._embed_more = True  # new events to embed

                    now = time.monotonic()
                    if dirty and (now - last_maintenance) >= self.maintenance_interval:
                        try:
                            self.maintain()
                        except Exception as e:  # noqa: BLE001 — upkeep must not kill the loop
                            logger.warning("watch: maintenance error: %s", e)
                        last_maintenance = now
                        dirty = False

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

            # Sleep in short slices so stop() is responsive.
            slept = 0.0
            while slept < self.interval and not self._stop:
                time.sleep(min(0.5, self.interval - slept))
                slept += 0.5

    def stop(self) -> None:
        self._stop = True

    def available(self) -> list[SourceWatcher]:
        return [w for w in self.watchers if w.is_available()]
