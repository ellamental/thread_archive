"""Ledger-driven re-import: the recovery half of a parser fix.

A fixed parser changes nothing by itself — the content its broken predecessor
consumed is behind watermarks that say "fully imported". The skip and drift
ledgers recorded exactly which ``(source, source_id)`` pairs that happened to,
so recovery is mechanical: rewind those watermarks to line 0, re-read exactly those
files (event-level dedup keeps the previously understood content from doubling and
the newly understood content lands as new events), then replay any drift-quarantine
snapshot whose original file the provider has since pruned.

Two things here look like details and are the whole mechanism. The watermark is
**rewound rather than deleted** — an absent watermark on a thread that already has
events triggers the importer's adoption guard, which re-stamps it at EOF and
imports nothing. And the files are **read directly rather than polled for** — a
watcher skips an unchanged file on its ``(mtime, size)`` fingerprint before any
watermark is consulted, and the fingerprint cache belongs to the running daemon,
which will flush its own copy back over anything a repair clears. Either detail
alone turns this module into a no-op that reports success.

This is what makes "best effort" honest: however late the fix, nothing that
reached a ledger or a snapshot is lost.

**A re-read is also the verdict on the fix.** Re-parsing exactly the files the
ledgers named, through the parser as it stands now, is the same experiment that
produced the records — so its outcome is what closes them
(:func:`.._importers._validation_ledger.record_resolution`). The stamp is taken
before the first re-read, so anything the re-parse itself records lands after it
and stays open: a fix that didn't work closes nothing and the source stays
degraded. Without this the degradation verdict outlives its cause by the whole
rolling ledger window, telling the operator to run the repair they just ran.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .._config import resolve_paths

logger = logging.getLogger(__name__)

# How far back the ledgers are read for watermarks to reset. Wider than the
# drift-snapshot ACTIVE_WINDOW so a slow fix still covers everything a
# snapshot preserved.
REIMPORT_WINDOW_DAYS = 60.0


def _recent_source_ids(source: str, *, home: Optional[str], days: float) -> set[str]:
    """Every ``source_id`` the skip or drift ledger names for ``source`` within
    the window. Malformed lines are skipped — the ledgers are advisory."""
    from .._importers._skip_ledger import LEDGER_FILE as SKIP_FILE
    from .._importers._validation_ledger import LEDGER_FILE as DRIFT_FILE

    home_dir = resolve_paths(home).home
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    ids: set[str] = set()
    for filename, source_key in ((SKIP_FILE, "source"), (DRIFT_FILE, "provider")):
        try:
            lines = (home_dir / filename).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if rec.get(source_key) != source:
                    continue
                at = datetime.fromisoformat(rec["at"])
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if at.timestamp() >= cutoff and rec.get("source_id"):
                    ids.add(str(rec["source_id"]))
            except (ValueError, KeyError, TypeError):
                continue
    return ids


def _reset_watermarks(source: str, source_ids: set[str]) -> int:
    """Rewind the import watermarks for ``(source, source_id)`` pairs to line 0, so
    the next read re-imports those files whole. Returns rows rewound.

    Rewound, emphatically not deleted. Deleting looks like the stronger move and is
    the weaker one: an absent watermark on a thread that already has events is the
    signal for :func:`~thread_archive._importers._state.adopt_if_unwatermarked`,
    which stamps the watermark back at EOF and imports nothing — so deleting a
    watermark is a reliable way to make a repair do *less* than nothing while
    reporting success. A zeroed watermark is the state the cursor already models
    (:func:`~thread_archive._importers._cursor.resolve_source_cursor` resumes at
    line 0 and dedup collapses what we already hold), and it keeps the row's
    ``thread_id`` so the re-read lands in the same conversation.
    """
    if not source_ids:
        return 0
    from sqlalchemy import update

    from .._store import ImportState, dml_rowcount, get_session

    with get_session() as s:
        count = dml_rowcount(
            s,
            update(ImportState)
            .where(ImportState.source == source, ImportState.source_id.in_(source_ids))
            .values(last_line_count=0, last_file_size=0, last_content_hash=None,
                    last_message_uuid=None),
        )
        s.commit()
    return count


def _replay_snapshots(provider, *, home: Optional[str]) -> dict:
    """Import drift-quarantine copies whose originals the provider has pruned.

    Only files the snapshot manifest recorded a ``source_id`` for are
    replayable (the importer needs the id the watermark and thread key on);
    a file whose original still exists is skipped — the direct re-read covers it.
    Watermarks for replayed ids are rewound first: the stale watermark was
    computed over the same bytes, so the importer would otherwise resume past
    the content the fix now understands.
    """
    from .._watcher.drift_snapshot import DRIFT_DIRNAME

    replayed = events = 0
    errors: list[str] = []
    ids: set[str] = set()
    source_dir = resolve_paths(home).dumps_dir / DRIFT_DIRNAME / provider.name
    if not source_dir.is_dir() or provider.kind != "line-stream" or not provider.importer:
        return {"replayed": 0, "events_created": 0, "errors": errors, "ids": ids}
    for gen in sorted(p for p in source_dir.iterdir() if p.is_dir()):
        try:
            manifest = json.loads((gen / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in manifest.get("files", []):
            source_id = entry.get("source_id")
            original = entry.get("path")
            stored = gen / str(entry.get("stored"))
            if not source_id or not stored.is_file():
                continue
            if original and Path(original).exists():
                continue  # still live — the poll re-imports the real file
            try:
                _reset_watermarks(provider.name, {str(source_id)})
                result = provider.importer(stored, str(source_id))
                replayed += 1
                ids.add(str(source_id))
                events += getattr(result, "events_created", 0) or 0
            except Exception as e:  # noqa: BLE001 — one bad copy must not stop recovery
                errors.append(f"{source_id}: {e}")
                logger.warning(
                    "snapshot replay failed for %s:%s", provider.name, source_id,
                    exc_info=True,
                )
    return {"replayed": replayed, "events_created": events, "errors": errors, "ids": ids}


def _reimport_files(provider, source_ids: set[str]) -> dict:
    """Re-read exactly the ledgered files, by asking the watcher where they live.

    Targeted rather than a poll, and not merely as an economy. A poll decides what
    to read from its fingerprint cache, and the watcher **daemon** owns that cache:
    it holds the map in memory for the life of the process and flushes it
    periodically, so a repair that clears the persisted copy is racing a writer that
    will put it back. Reading the ledgered files directly needs no cache to be in
    any particular state and cannot lose that race.

    Provider-neutral: the watcher already yields ``(path, source_id)`` pairs for its
    store, so nothing here reconstructs a provider's path layout. Returns counts and
    per-file errors; a provider that isn't file-backed returns zeros and the caller
    falls back to a poll.
    """
    read = events = 0
    errors: list[str] = []
    ids: set[str] = set()
    miss = {"read": 0, "events_created": 0, "errors": errors, "eligible": False, "ids": ids}
    if provider.kind != "line-stream" or not provider.importer or provider.watcher is None:
        return miss
    try:
        w = provider.watcher()
        if not w.is_available():
            return miss
        by_id = {sid: path for path, sid in w.store_items()}
    except Exception as e:  # noqa: BLE001 — an unlistable store is a miss, not a crash
        logger.warning("%s: could not enumerate the store: %s", provider.name, e)
        return {**miss, "errors": [str(e)]}

    for source_id in sorted(source_ids):
        path = by_id.get(source_id)
        if path is None:
            continue  # pruned by the provider — the snapshot replay is its recovery
        try:
            result = provider.importer(path, source_id)
            read += 1
            ids.add(source_id)
            events += getattr(result, "events_created", 0) or 0
        except Exception as e:  # noqa: BLE001 — one bad file must not stop recovery
            errors.append(f"{source_id}: {e}")
            logger.warning(
                "re-import failed for %s:%s", provider.name, source_id, exc_info=True
            )
    return {"read": read, "events_created": events, "errors": errors,
            "eligible": True, "ids": ids}


def _close_repaired(source: str, reread: set[str], *, through: datetime) -> dict:
    """Close the ledger records for files this pass re-read without re-breaking.

    ``through`` is the pre-re-read stamp, so "did not re-break" is decided by what
    the re-parse itself wrote: a file that drifted or skipped again during recovery
    is named in ``still_drifting`` and its records stay open. Nothing is closed on
    faith — a file the provider had already pruned was never re-read, so it is not a
    candidate here and its records age out of the coverage window on their own.
    """
    from .._importers import _skip_ledger, _validation_ledger

    if not reread:
        return {"drift_closed": 0, "skips_closed": 0, "still_drifting": []}
    open_again = (
        _validation_ledger.substantive_since(source, through)
        | _skip_ledger.substantive_since(source, through)
    ) & reread
    clean = reread - open_again
    return {
        "drift_closed": _validation_ledger.record_resolution(
            source, clean, through=through, by="reimport"
        ),
        "skips_closed": _skip_ledger.record_resolution(
            source, clean, through=through, by="reimport"
        ),
        "still_drifting": sorted(open_again),
    }


def _refresh_coverage(home: Optional[str]) -> bool:
    """Re-derive the degradation verdicts now that the ledgers have moved.

    The verdict agents and the operator actually see is the one cached in
    health.json, so a repair that closes records but leaves that cache standing has
    fixed the archive and not the thing telling everyone it is broken — the nightly
    would clear it eventually, which is a day of wrong advice. Snapshot pass off: a
    repair is not the discovery of new drift, and the raw store was already
    quarantined when the source first degraded. Fail-soft — recovery already
    happened, and a report that won't compute must not turn it into an error.
    """
    from .._ops.coverage import check_coverage

    try:
        check_coverage(home=home, snapshot=False)
        return True
    except Exception:  # noqa: BLE001 — the recovery stands whatever the report does
        logger.exception("could not refresh coverage after the re-import")
        return False


def reimport_source(
    source: str, *, home: Optional[str] = None, days: float = REIMPORT_WINDOW_DAYS
) -> dict:
    """Recover ``source``'s ledgered content through the (fixed) import path.

    Returns a summary dict: watermarks reset, files re-read, snapshot replay counts,
    and the verdict on the fix — how many ledger records the clean re-reads closed,
    and which files are drifting still. Refreshes the coverage verdicts afterwards
    so what the operator and the MCP notice read matches what just happened.

    The caller activates the patch first — running this against the broken parser
    just re-consumes the same content the same way, which the ledger will say.
    """
    from .._api import open_archive
    from .._providers import get as get_provider

    open_archive(home)
    provider = get_provider(source, home=home)
    if provider is None:
        raise ValueError(f"unknown provider {source!r}")

    # Stamped before anything is re-read: every record already in the ledgers
    # predates it, and every record the re-parse writes will not.
    started = datetime.now(timezone.utc)
    ids = _recent_source_ids(source, home=home, days=days)
    reset = _reset_watermarks(source, ids)
    logger.info(
        "%s: reset %d watermark(s) from %d ledgered source id(s)", source, reset, len(ids)
    )
    # Dropped before anything reads it, so a poll-based recovery starts from no
    # cache. The targeted path below does not depend on this having stuck — the
    # running daemon may flush its own copy back at any moment.
    from .._watcher import fingerprints

    forgot = fingerprints.forget(source, home=home)

    direct = _reimport_files(provider, ids)
    poll = None
    if not direct["eligible"] and provider.watcher is not None:
        # Not file-backed (a db-scan source): the ledgered ids are rows inside one
        # store, so re-reading "those files" has no meaning and a poll is the only
        # recovery there is.
        w = provider.watcher()
        if w.is_available():
            poll = w.poll()
    logger.info(
        "%s: re-import — %d file(s) re-read, %d event(s) created",
        source, direct["read"], direct["events_created"] + (poll.events_created if poll else 0),
    )

    replay = _replay_snapshots(provider, home=home)
    if replay["replayed"]:
        logger.info(
            "%s: snapshot replay — %d pruned file(s), %d event(s) recovered",
            source, replay["replayed"], replay["events_created"],
        )

    reread = direct["ids"] | replay["ids"]
    closed = _close_repaired(source, reread, through=started)
    if closed["still_drifting"]:
        logger.warning(
            "%s: %d file(s) still record findings on re-read — the import is not fixed",
            source, len(closed["still_drifting"]),
        )
    elif closed["drift_closed"] or closed["skips_closed"]:
        logger.info(
            "%s: closed %d drift and %d skip record(s)",
            source, closed["drift_closed"], closed["skips_closed"],
        )
    return {
        "source_ids": len(ids),
        "watermarks_reset": reset,
        "fingerprints_dropped": forgot,
        "files_reread": direct["read"],
        # Ledgered ids no re-read could reach: the provider pruned the file and no
        # quarantine copy stood in for it. Their records are unfalsifiable now — the
        # honest report is that they exist, not silence.
        "unreachable": len(ids - reread),
        "poll_items": poll.items_imported if poll else direct["read"],
        "poll_events": (poll.events_created if poll else 0) + direct["events_created"],
        "poll_errors": (list(poll.errors) if poll else []) + direct["errors"],
        "snapshot_replayed": replay["replayed"],
        "snapshot_events": replay["events_created"],
        "snapshot_errors": replay["errors"],
        "drift_closed": closed["drift_closed"],
        "skips_closed": closed["skips_closed"],
        "still_drifting": closed["still_drifting"],
        "coverage_refreshed": _refresh_coverage(home),
    }
