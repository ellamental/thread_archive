"""Capture-coverage check: the sources' stores reconciled against the archive.

:func:`verify` guards truth↔index — what the archive stored is consistent with
itself. This module guards one hop upstream, source↔truth: what the archive
stored is what actually happened on this machine. The two observables already
exist on both sides — each watcher can stat its store cheaply
(:meth:`.._watcher.base.SourceWatcher.discover`), and the archive knows what it
ingested (``import_state`` watermarks, newest archived event per source) — so
coverage is their reconciliation:

- **went dark** (FAIL): a source with real imported history whose store is now
  missing or empty. A harness update that moves its store directory otherwise
  turns a live source into "not installed" with zero signal — the watcher just
  skips unavailable sources. Resolution is either fixing the watcher's paths or
  explicitly disabling the source in ``config.json`` (the sanctioned off switch).
- **stale ingest** (FAIL): store activity newer than the newest archived event,
  beyond grace — checked only for sources whose store mtimes move on content
  writes (``store_mtime_tracks_content``; live-SQLite stores churn mtimes without
  new conversations and are exempt). Compared against archived *events*, not
  import watermarks, deliberately: soft format drift advances watermarks while
  producing nothing, and this comparison is the one that still catches it.
- **never ingested** (warn): a store with content and zero import history —
  either a first import still in flight, or a hole.
- **report-only**: sources disabled in config, and archive sources with no
  watcher at all (export-based providers age between manual drops; that is
  expected, so their age is shown, never red — except that a known export-fed
  source aging past ``EXPORT_STALE_DAYS`` earns a *warning*: the archive can't
  watch a provider's servers, so "time to drop a fresh export" has no other
  surface). A source fed by both a watcher and account exports (grok) ages its
  export channel by itself — see ``EXPORT_FED_SOURCES``. Disabled sources report
  their store's current activity alongside their import history: a deliberate
  opt-out's store staying active is normal, but a source disabled by
  *accident* (a config bug, a wizard regression) has no other surface where
  its unarchived activity shows — visibility here is what keeps the
  sanctioned off switch from doubling as a silent capture hole.
- **ledger volume** (warn): recent capture-skip or validation-drift records.
  Both ledgers record failures that never throw; the warning is what makes them
  reach anything watching coverage instead of waiting to be read.

Runs nightly as a pipeline stage (recording ``coverage_last``; an out-of-band
green run retires a red nightly stage, see :mod:`.health`) and on demand via
``archive coverage``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .health import record_health, stamp_heartbeat

# A store write should surface as archived events well within this window; the
# slack absorbs message-timestamp vs file-mtime skew and imports of old content.
GRACE_HOURS = 6.0
# Sessions of imported history before a missing store reads as "went dark"
# rather than "was never really used here".
MIN_HISTORY_FOR_DARK = 5
# Sources fed by manual account exports — the provider's servers can't be
# watched, so the export channel ages between drops by design. Past this window
# the aging is a coverage hole worth a warning: everything since the last export
# exists only on the provider's servers. Disabling the source in config.json
# silences it. A source may ALSO have a live watcher (grok: the Grok-CLI watcher
# and xAI account exports share source='grok') — for those, staleness is judged
# on the export channel alone (``source_metadata.surface == 'web'``), because
# fresh CLI events would otherwise mask an aging web-side export forever.
EXPORT_FED_SOURCES = {"claude": "claude.ai", "chatgpt": "ChatGPT", "grok": "xAI/Grok"}
EXPORT_STALE_DAYS = 45.0


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _epoch(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _archive_side() -> tuple[dict, dict]:
    """Per-source archive state: ``{source: (watermarks, last_import_at)}`` from
    import_state, and ``{source: newest event occurred_at}`` (one grouped pass —
    nightly-priced on a millions-of-events index)."""
    from sqlalchemy import func, select

    from .._store import Event, ImportState, Thread, get_session

    with get_session() as s:
        history = {
            source: (int(count), _epoch(last))
            for source, count, last in s.execute(
                select(
                    ImportState.source,
                    func.count(),
                    func.max(ImportState.last_import_at),
                ).group_by(ImportState.source)
            )
        }
        newest_event = {
            source: _epoch(newest)
            for source, newest in s.execute(
                select(Thread.source, func.max(Event.occurred_at))
                .join(Thread, Event.thread_id == Thread.id)
                .where(Thread.source.is_not(None))
                .group_by(Thread.source)
            )
        }
    return history, newest_event


def _newest_export_event(source: str) -> Optional[float]:
    """Newest event among ``source``'s *export-imported* threads (the export
    importers stamp ``source_metadata.surface = 'web'``; watcher-tailed threads
    carry no surface). The channel discriminator for sources fed by both a live
    watcher and manual account exports."""
    from sqlalchemy import func, select

    from .._store import Event, Thread, get_session

    with get_session() as s:
        newest = s.execute(
            select(func.max(Event.occurred_at))
            .join(Thread, Event.thread_id == Thread.id)
            .where(
                Thread.source == source,
                func.json_extract(Thread.source_metadata, "$.surface") == "web",
            )
        ).scalar()
    return _epoch(newest)


def check_coverage(
    *,
    home: Optional[str] = None,
    watchers: Optional[list] = None,
    all_watchers: Optional[list] = None,
    grace_hours: float = GRACE_HOURS,
    min_history: int = MIN_HISTORY_FOR_DARK,
    export_stale_days: float = EXPORT_STALE_DAYS,
) -> dict:
    """Reconcile every enabled source's store against the archive (see module
    docstring for the checks). Returns the full report; records a compact
    ``coverage_last`` in health.json. ``watchers`` overrides the enabled set
    (tests inject stubs); ``all_watchers`` overrides the full known set the
    disabled/unwatched reporting is computed against — when only ``watchers``
    is injected it doubles as the full set, so a stub-driven test never
    discovers the real machine's stores."""
    from .._api import open_archive
    from .._importers._skip_ledger import summarize_skips
    from .._importers._validation_ledger import summarize_drift
    from .._watcher.sources import (
        _MECHANISM_SOURCES,
        default_watchers,
        enabled_watchers,
    )

    open_archive(home)
    if watchers is None:
        watchers = enabled_watchers(home)
        if all_watchers is None:
            all_watchers = default_watchers()
    if all_watchers is None:
        all_watchers = watchers
    watchers = [w for w in watchers if w.source_name not in _MECHANISM_SOURCES]

    history, newest_event = _archive_side()
    grace = grace_hours * 3600
    failed: list[str] = []
    warnings: list[str] = []
    sources: dict[str, dict] = {}

    for w in watchers:
        name = w.source_name
        d = w.discover()
        hist_count, last_import = history.get(name, (0, None))
        newest = newest_event.get(name)
        store_empty = not d.available or ((d.items or 0) == 0 and (d.bytes or 0) == 0)
        entry = {
            "available": d.available,
            "store_items": d.items,
            "store_bytes": d.bytes,
            "store_latest": _iso(d.latest),
            "history": hist_count,
            "last_import_at": _iso(last_import),
            "newest_event_at": _iso(newest),
            "mtime_tracks_content": bool(w.store_mtime_tracks_content),
        }

        if store_empty and hist_count >= min_history:
            entry["failed"] = "went_dark"
            failed.append(
                f"{name} went dark: store missing/empty with {hist_count} imported "
                "sessions of history (fix the store path, or disable the source in "
                "config.json if it was retired)"
            )
        elif (
            w.store_mtime_tracks_content
            and not store_empty
            and d.latest is not None
            and hist_count > 0
            and d.latest - (newest or 0.0) > grace
        ):
            entry["failed"] = "stale_ingest"
            failed.append(
                f"{name} ingest is stale: store activity at {_iso(d.latest)} but "
                f"newest archived event is {entry['newest_event_at'] or 'absent'} "
                "— recent store writes are not becoming events (wedged ingest, or "
                "a parser blind to a changed format)"
            )
        elif not store_empty and hist_count == 0:
            entry["warning"] = "never_ingested"
            warnings.append(
                f"{name}: store has content but was never imported "
                "(first import still in flight, or a capture hole)"
            )
        sources[name] = entry

    # Report-only context: explicitly disabled sources, and archive sources with
    # no watcher (manual/export-based, plus historical providers). Never red.
    enabled_names = {w.source_name for w in watchers}
    disabled = {}
    for w in all_watchers:
        n = w.source_name
        if n not in enabled_names and n not in _MECHANISM_SOURCES:
            hist_count, last_import = history.get(n, (0, None))
            d = w.discover()
            disabled[n] = {
                "history": hist_count,
                "last_import_at": _iso(last_import),
                "store_items": d.items,
                "store_latest": _iso(d.latest),
            }
    watcher_names = {w.source_name for w in all_watchers}
    from .._config import load_config, source_enabled

    cfg = load_config(home)
    now = datetime.now(timezone.utc).timestamp()
    unwatched: dict[str, dict] = {}
    for source, epoch in sorted(newest_event.items()):
        if not source:
            continue
        label = EXPORT_FED_SOURCES.get(source)
        watched = source in watcher_names
        if watched:
            # A watcher keeps the source's newest_event young, so per-source
            # recency says nothing about its export channel — age that channel
            # by itself, or fresh CLI events hide a stale account export forever.
            if not label:
                continue
            epoch = _newest_export_event(source)
            if epoch is None:
                continue  # no export ever imported: no channel to age
        uw_entry: dict = {"newest_event_at": _iso(epoch)}
        if watched:
            uw_entry["channel"] = "export"
        if (
            label
            and epoch is not None
            and source_enabled(cfg, source)
            and now - epoch > export_stale_days * 86400
        ):
            uw_entry["warning"] = "export_stale"
            warnings.append(
                f"{source} account export is stale: newest export-imported event "
                f"is {(now - epoch) / 86400:.0f}d old — conversations since then "
                f"exist only on {label}'s servers (drop a fresh export, or disable "
                "the source in config.json)"
            )
        unwatched[source] = uw_entry

    skips = summarize_skips()
    drift = summarize_drift()
    # Recent ledger records are unresolved capture loss until someone looks:
    # both ledgers exist precisely because these failures never throw, so
    # recording without warning left them invisible to everything watching
    # coverage (nightly notify, ops digest). Warn, never red — a single benign
    # drift record must not fail the pipeline, but it must surface.
    if drift["recent"]:
        warnings.append(
            f"format drift: {drift['recent']} validation-drift record(s) "
            f"({drift['recent_findings']} finding(s)) in the last {drift['days']:.0f}d "
            "— a parser no longer fully understands a source's format; see the "
            "drift ledger"
        )
    if skips["recent"]:
        warnings.append(
            f"capture skips: {skips['recent']} skip record(s) "
            f"({skips['recent_lines']} line(s)) in the last {skips['days']:.0f}d "
            "— store content was consumed without becoming events; see the "
            "skip ledger"
        )
    result = {
        "ok": not failed,
        "failed": failed,
        "warnings": warnings,
        "sources": sources,
        "disabled": disabled,
        "unwatched": unwatched,
        "skips": skips,
        "drift": drift,
    }
    record_health("coverage_last", {
        "ok": result["ok"],
        "failed": failed,
        "warnings": warnings,
        "sources_checked": len(sources),
        "skips_recent": skips["recent"],
        "drift_recent": drift["recent"],
    })
    stamp_heartbeat()
    return result
