"""Drift quarantine: preservation snapshots of a degraded source's raw store.

When coverage marks a source degraded — its importer consuming content without
producing events, or its parser flagging sustained validation drift — the raw
source files are the only recoverable record, and providers prune them on their
own schedule (Claude Code keeps roughly 30 days). The fix comes later, at the
user's cadence (``archive fix-import``), so preservation cannot wait for it:
this module copies the store's recently-active files into
``<home>/dumps/drift/<source>/<stamp>/``, where a late fix's re-import can
still reach them however long the fix takes.

Snapshots are incremental generations. Each generation copies only files whose
``(path, size, mtime_ns)`` identity is absent from every previous generation's
manifest, so sustained drift costs roughly the new activity per pass, not a
full store copy each night. A fresh generation is taken at most once per
``REFRESH_HOURS`` per source. Generations are never auto-deleted — that is the
quarantine promise (the archive does not discard what may be the only copy);
the operator clears ``dumps/drift/<source>/`` once a fix's re-import has
recovered the content. Each generation is bounded (file count and total bytes,
newest files first) and logs what the bound dropped — a silent cap would read
as full coverage.

Live SQLite stores are copied through the sqlite3 backup API (a consistent
point-in-time copy even mid-write); everything else is a plain file copy.
Fail-soft throughout: snapshotting is best-effort preservation and must never
break the coverage check that invokes it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .._config import resolve_paths

logger = logging.getLogger(__name__)

DRIFT_DIRNAME = "drift"
# At most one new generation per source per this window; drift persisting past
# it earns an incremental refresh so the quarantine keeps covering the
# provider's prune horizon.
REFRESH_HOURS = 7 * 24.0
# Only files active within this window are candidates: older files either
# imported fine before the drift began or were already captured by an earlier
# generation. Comfortably wider than the shortest known provider prune cycle.
ACTIVE_WINDOW_DAYS = 45.0
# Per-generation bounds, newest-first, truncation logged.
MAX_BYTES = 500 * 1024 * 1024


def max_files() -> int:
    """Files a single generation may copy (2000 by default).

    Read per call from ``THREAD_ARCHIVE_DRIFT_MAX_FILES``: a constant would
    answer once at import and ignore any later word on it. The cap exists so one
    pass over a huge store can't run away; a store whose active window really
    holds more files than this needs the cap raised, or the quarantine covers
    only its newest slice.
    """
    return int(os.environ.get("THREAD_ARCHIVE_DRIFT_MAX_FILES") or 2000)

_STAMP_FMT = "%Y%m%dT%H%M%SZ"
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _generations(source_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        return []
    return sorted(p for p in source_dir.iterdir() if p.is_dir())


def _latest_stamp(source_dir: Path) -> Optional[datetime]:
    for gen in reversed(_generations(source_dir)):
        try:
            return datetime.strptime(gen.name, _STAMP_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _seen_identities(source_dir: Path) -> set[tuple[str, int, int]]:
    """The union of every prior generation's file identities."""
    seen: set[tuple[str, int, int]] = set()
    for gen in _generations(source_dir):
        try:
            manifest = json.loads((gen / "manifest.json").read_text(encoding="utf-8"))
            for f in manifest.get("files", []):
                seen.add((str(f["path"]), int(f["size"]), int(f["mtime_ns"])))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return seen


def _dest_relpath(path: Path) -> Path:
    """A copy's path inside the generation dir: the source's absolute path with
    the root anchor dropped, so provenance stays readable and collisions are
    impossible."""
    return Path(*path.parts[1:]) if path.is_absolute() else path


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(src, "rb") as fh:
            is_sqlite = fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        is_sqlite = False
    if is_sqlite:
        # Backup API: a consistent copy of a live DB, WAL content included —
        # a plain copy of a mid-write DB can be unreadable.
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as conn, sqlite3.connect(
            dest
        ) as out:
            conn.backup(out)
        return
    shutil.copy2(src, dest)


def snapshot_source(
    watcher, *, reason: str, home: Optional[str] = None
) -> Optional[str]:
    """One snapshot attempt for a degraded source. Returns the generation dir
    written, or ``None`` (nothing new to preserve, refresh window not yet
    elapsed, or the watcher cannot enumerate its store)."""
    name = watcher.source_name
    source_dir = resolve_paths(home).dumps_dir / DRIFT_DIRNAME / name

    last = _latest_stamp(source_dir)
    now = datetime.now(timezone.utc)
    if last is not None and (now - last).total_seconds() < REFRESH_HOURS * 3600:
        return None

    # Per-file source_ids, where the watcher can name them: what lets a fix's
    # re-import replay a snapshot copy after the provider pruned the original
    # (the importer needs the id the watermark and thread are keyed on).
    id_by_path: dict[str, str] = {}
    if hasattr(watcher, "iter_files"):
        try:
            id_by_path = {str(p): sid for p, sid in watcher.iter_files()}
        except Exception:  # noqa: BLE001 — ids are an enrichment, not a gate
            id_by_path = {}

    seen = _seen_identities(source_dir)
    cutoff = now.timestamp() - ACTIVE_WINDOW_DAYS * 86400
    candidates: list[tuple[float, Path, int, int]] = []
    for path in watcher.store_paths():
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size == 0 or st.st_mtime < cutoff:
            continue
        if (str(path), st.st_size, st.st_mtime_ns) in seen:
            continue
        candidates.append((st.st_mtime, path, st.st_size, st.st_mtime_ns))
    if not candidates:
        return None

    candidates.sort(reverse=True)  # newest first: closest to the prune horizon
    kept: list[tuple[float, Path, int, int]] = []
    total = 0
    file_cap = max_files()
    for cand in candidates:
        if len(kept) >= file_cap or total + cand[2] > MAX_BYTES:
            continue
        kept.append(cand)
        total += cand[2]
    dropped = len(candidates) - len(kept)

    gen_dir = source_dir / now.strftime(_STAMP_FMT)
    n = 1
    while gen_dir.exists():  # same-second successor must not merge into it
        n += 1
        gen_dir = source_dir / f"{now.strftime(_STAMP_FMT)}-{n}"
    copied: list[dict] = []
    for _, path, size, mtime_ns in kept:
        dest = gen_dir / _dest_relpath(path)
        try:
            _copy_file(path, dest)
        except Exception:  # noqa: BLE001 — one uncopyable file must not stop the pass
            logger.warning("drift snapshot: could not copy %s", path, exc_info=True)
            continue
        entry = {
            "path": str(path),
            "size": size,
            "mtime_ns": mtime_ns,
            "stored": str(dest.relative_to(gen_dir)),
        }
        if str(path) in id_by_path:
            entry["source_id"] = id_by_path[str(path)]
        copied.append(entry)
    if not copied:
        shutil.rmtree(gen_dir, ignore_errors=True)
        return None

    manifest = {
        "at": now.isoformat(),
        "source": name,
        "reason": reason,
        "files": copied,
        "dropped": dropped,
    }
    (gen_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if dropped:
        logger.warning(
            "drift snapshot for %s: bounds dropped %d candidate file(s) — the "
            "generation is NOT a full copy of recent store activity",
            name,
            dropped,
        )
    logger.info(
        "drift snapshot for %s (%s): preserved %d file(s), %.1f MB → %s",
        name,
        reason,
        len(copied),
        total / 1e6,
        gen_dir,
    )
    return str(gen_dir)


def snapshot_degraded(
    watchers: list, degraded: dict[str, dict], *, home: Optional[str] = None
) -> dict[str, str]:
    """Snapshot every degraded source that has a watcher. ``degraded`` is
    coverage's verdict map (``{source: {reason, since}}``). Returns
    ``{source: generation dir}`` for generations actually written."""
    by_name = {w.source_name: w for w in watchers}
    written: dict[str, str] = {}
    for source, verdict in degraded.items():
        w = by_name.get(source)
        if w is None:
            continue
        try:
            gen = snapshot_source(w, reason=str(verdict.get("reason")), home=home)
        except Exception:  # noqa: BLE001 — preservation must never break coverage
            logger.exception("drift snapshot failed for %s", source)
            continue
        if gen:
            written[source] = gen
    return written
