"""Ledger-driven re-import: the recovery half of a parser fix.

A fixed parser changes nothing by itself — the content its broken predecessor
consumed is behind watermarks that say "fully imported". The skip and drift
ledgers recorded exactly which ``(source, source_id)`` pairs that happened to,
so recovery is mechanical: delete those watermarks, poll the source once (the
importer re-reads each file from line 0; event-level dedup keeps the previously
understood content from doubling and the newly understood content lands as new
events), then replay any drift-quarantine snapshot whose original file the
provider has since pruned.

This is what makes "best effort" honest: however late the fix, nothing that
reached a ledger or a snapshot is lost.
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
    """Delete the import watermarks for ``(source, source_id)`` pairs, so the
    next poll re-imports those files from line 0. Returns rows deleted."""
    if not source_ids:
        return 0
    from sqlalchemy import delete

    from .._store import ImportState, get_session

    with get_session() as s:
        result = s.execute(
            delete(ImportState).where(
                ImportState.source == source, ImportState.source_id.in_(source_ids)
            )
        )
        s.commit()
    return int(result.rowcount or 0)


def _replay_snapshots(provider, *, home: Optional[str]) -> dict:
    """Import drift-quarantine copies whose originals the provider has pruned.

    Only files the snapshot manifest recorded a ``source_id`` for are
    replayable (the importer needs the id the watermark and thread key on);
    a file whose original still exists is skipped — the live poll covers it.
    Watermarks for replayed ids are reset first: the stale watermark was
    computed over the same bytes, so the importer would otherwise resume past
    the content the fix now understands.
    """
    from .._watcher.drift_snapshot import DRIFT_DIRNAME

    replayed = events = 0
    errors: list[str] = []
    source_dir = resolve_paths(home).dumps_dir / DRIFT_DIRNAME / provider.name
    if not source_dir.is_dir() or provider.kind != "line-stream" or not provider.importer:
        return {"replayed": 0, "events_created": 0, "errors": errors}
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
                events += getattr(result, "events_created", 0) or 0
            except Exception as e:  # noqa: BLE001 — one bad copy must not stop recovery
                errors.append(f"{source_id}: {e}")
                logger.warning(
                    "snapshot replay failed for %s:%s", provider.name, source_id,
                    exc_info=True,
                )
    return {"replayed": replayed, "events_created": events, "errors": errors}


def reimport_source(
    source: str, *, home: Optional[str] = None, days: float = REIMPORT_WINDOW_DAYS
) -> dict:
    """Recover ``source``'s ledgered content through the (fixed) import path.

    Returns a summary dict: watermarks reset, live-poll import counts, snapshot
    replay counts. The caller activates the patch first — running this against
    the broken parser just re-consumes the same content the same way.
    """
    from .._api import open_archive
    from .._providers import get as get_provider

    open_archive(home)
    provider = get_provider(source, home=home)
    if provider is None:
        raise ValueError(f"unknown provider {source!r}")

    ids = _recent_source_ids(source, home=home, days=days)
    reset = _reset_watermarks(source, ids)
    logger.info(
        "%s: reset %d watermark(s) from %d ledgered source id(s)", source, reset, len(ids)
    )

    poll = None
    if provider.watcher is not None:
        w = provider.watcher()
        if w.is_available():
            poll = w.poll()
            logger.info(
                "%s: re-import poll — %d item(s), %d event(s) created",
                source, poll.items_imported, poll.events_created,
            )

    replay = _replay_snapshots(provider, home=home)
    if replay["replayed"]:
        logger.info(
            "%s: snapshot replay — %d pruned file(s), %d event(s) recovered",
            source, replay["replayed"], replay["events_created"],
        )
    return {
        "source_ids": len(ids),
        "watermarks_reset": reset,
        "poll_items": poll.items_imported if poll else 0,
        "poll_events": poll.events_created if poll else 0,
        "poll_errors": list(poll.errors) if poll else [],
        "snapshot_replayed": replay["replayed"],
        "snapshot_events": replay["events_created"],
        "snapshot_errors": replay["errors"],
    }
