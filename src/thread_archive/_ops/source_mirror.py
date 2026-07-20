"""The raw source mirror: verbatim copies of the harness stores archive ingests.

The importers normalize; this preserves. Every transcript file the enabled
watchers would consume — plus the small JSON sidecars their importers read —
is copied verbatim, gzip-compressed, under
``<home>/source-mirror/<provider>/<original absolute path>.gz``, and every
live SQLite store gets a consistent snapshot via the SQLite backup API.
Nothing is ever deleted from the mirror: a session pruned from its harness
store (Claude Code deletes transcripts after ~30 days) stays here. That ends
the retention race behind every source re-read — a parser taught a new field,
a backfill repairing an importer bug — which otherwise loses whatever the
harness pruned first.

Layout mirrors the source paths exactly (``…/claude-code/Users/<you>/.claude/
projects/<proj>/<session>.jsonl.gz``), so re-parse tooling can reconstruct the
original path and source_id without a lookup table. A per-provider
``.manifest.json`` maps each source path to the ``(size, mtime_ns)`` it was
last copied at — an unchanged file is one stat, no read. A file that *shrank*
(a truncate/rewrite the archive may not have consumed) rotates the previous
copy to a numbered generation instead of overwriting it; a SQLite store keeps
one ``.prev.gz`` generation.

Scope and honesty:

- Coverage is what the enabled watchers reach: per-session transcript files
  (``FileSessionWatcher.iter_files``) and live SQLite stores
  (``DbScanWatcher`` targets). A provider whose watcher is neither shape is
  reported ``unsupported``, never silently skipped.
- Sidecars are approximate by design: ``*.json`` regular files up to
  ``_SIDECAR_MAX_BYTES``, in each transcript's directory and its parent
  (grok's ``summary.json``, cowork's ``local_<id>.json`` session metadata,
  Claude Code's per-project index). Anything larger or stranger is counted in
  ``sidecars_capped`` — a bounded copy that reads as complete would be worse
  than none.
- The nightly ``backup`` stage does NOT sweep the mirror (it mirrors
  ``truth/`` plus the recovery bundle); the mirror is defense-in-depth for
  source re-reads, not part of the restore set.

Sibling of ``backup`` (which mirrors the archive home *out* to a
destination); this mirrors the sources *in*. Fail-soft per file: one
unreadable path costs that path, never the sweep. A provider sweep is only
``ok: false`` when errors exceed ``_ERROR_FRACTION`` of its files — a single
mid-poll vanished file must not fail the nightly.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .._config import resolve_paths
from .health import record_health

logger = logging.getLogger(__name__)

MIRROR_SUBDIR = "source-mirror"
MANIFEST_FILE = ".manifest.json"

_SIDECAR_MAX_BYTES = 2 * 1024 * 1024
_ERROR_FRACTION = 0.10
_GZIP_LEVEL = 6


def _mirror_dest(provider_root: Path, src: Path) -> Path:
    """``<provider_root>/<absolute source path, anchor stripped>.gz``."""
    rel = src.resolve()
    return provider_root.joinpath(*rel.parts[1:]).with_name(rel.name + ".gz")


def _load_manifest(provider_root: Path) -> dict:
    try:
        with open(provider_root / MANIFEST_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # Missing on first run; corrupt → recopy everything (safe, just slow).
        return {}


def _save_manifest(provider_root: Path, manifest: dict) -> None:
    provider_root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(provider_root), prefix=".manifest-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, sort_keys=True)
        os.replace(tmp, provider_root / MANIFEST_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rotate_generation(dest: Path) -> None:
    """Move ``dest`` aside to the first free ``dest.N`` so a shrunken source
    can't overwrite the only copy of content the archive may not have read."""
    if not dest.exists():
        return
    n = 1
    while dest.with_name(f"{dest.name}.{n}").exists():
        n += 1
    os.replace(dest, dest.with_name(f"{dest.name}.{n}"))


def _gzip_to(reader, dest: Path) -> int:
    """Stream ``reader`` into ``dest`` (atomic publish). Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".mirror-")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(
            fileobj=raw, mode="wb", compresslevel=_GZIP_LEVEL, mtime=0
        ) as out:
            shutil.copyfileobj(reader, out)
        written = os.path.getsize(tmp)
        os.replace(tmp, dest)
        return written
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _ProviderSweep:
    """Mutable per-provider counters + the manifest they justify."""

    def __init__(self, provider_root: Path) -> None:
        self.root = provider_root
        self.manifest = _load_manifest(provider_root)
        self.files = 0
        self.copied = 0
        self.unchanged = 0
        self.sidecars = 0
        self.sidecars_capped = 0
        self.generations = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.errors: list[str] = []
        self._seen: set[str] = set()

    def mirror_file(self, src: Path, *, sidecar: bool = False) -> None:
        key = str(src)
        if key in self._seen:
            return
        self._seen.add(key)
        try:
            st = src.stat()
            if not src.is_file():
                return
        except OSError as e:
            self.errors.append(f"{src}: stat: {e}")
            return
        self.files += 1
        rec = self.manifest.get(key)
        if rec and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns:
            self.unchanged += 1
            return
        dest = _mirror_dest(self.root, src)
        try:
            if rec and st.st_size < int(rec.get("size") or 0):
                _rotate_generation(dest)
                self.generations += 1
            with open(src, "rb") as fin:
                self.bytes_out += _gzip_to(fin, dest)
        except OSError as e:
            self.errors.append(f"{src}: copy: {e}")
            return
        # The pre-copy stat is recorded: a file that grew mid-copy looks
        # changed next sweep and is recopied — never marked current early.
        self.manifest[key] = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.bytes_in += st.st_size
        self.copied += 1
        if sidecar:
            self.sidecars += 1

    def mirror_sidecars(self, transcript: Path) -> None:
        """``*.json`` regular files beside the transcript and one level up."""
        for directory in (transcript.parent, transcript.parent.parent):
            try:
                candidates = sorted(directory.glob("*.json"))
            except OSError:
                continue
            for p in candidates:
                try:
                    if not p.is_file():
                        continue
                    if p.stat().st_size > _SIDECAR_MAX_BYTES:
                        self.sidecars_capped += 1
                        continue
                except OSError:
                    continue
                self.mirror_file(p, sidecar=True)

    def mirror_sqlite(self, db_path: Path) -> None:
        """A consistent snapshot of a live SQLite store, via the backup API.

        Fingerprinted on the main file *and* its WAL (a write can land in the
        WAL alone). Always overwrites, keeping exactly one ``.prev.gz``
        generation — a live DB changes every sweep, so numbered generations
        would grow without bound, and the archive's own truth already holds
        the imported history."""
        key = str(db_path)
        if key in self._seen:
            return
        self._seen.add(key)
        wal = db_path.with_name(db_path.name + "-wal")
        try:
            st = db_path.stat()
            wal_st: Optional[os.stat_result] = wal.stat() if wal.exists() else None
        except OSError as e:
            self.errors.append(f"{db_path}: stat: {e}")
            return
        self.files += 1
        fingerprint = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "wal_size": wal_st.st_size if wal_st else 0,
            "wal_mtime_ns": wal_st.st_mtime_ns if wal_st else 0,
        }
        rec = self.manifest.get(key)
        if rec and all(rec.get(k) == v for k, v in fingerprint.items()):
            self.unchanged += 1
            return
        dest = _mirror_dest(self.root, db_path)
        tmp_db = None
        try:
            # The snapshot temp file lives beside the destination, on the
            # mirror's filesystem, so the gzip pass reads stable storage.
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_db = tempfile.mkstemp(dir=str(dest.parent), prefix=".snap-")
            os.close(fd)
            src_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            try:
                dst_conn = sqlite3.connect(tmp_db)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
            if dest.exists():
                os.replace(dest, dest.with_name(dest.name[: -len(".gz")] + ".prev.gz"))
            with open(tmp_db, "rb") as fin:
                self.bytes_out += _gzip_to(fin, dest)
            self.bytes_in += os.path.getsize(tmp_db)
        except (OSError, sqlite3.Error) as e:
            self.errors.append(f"{db_path}: snapshot: {e}")
            return
        finally:
            if tmp_db is not None:
                try:
                    os.unlink(tmp_db)
                except OSError:
                    pass
        self.manifest[key] = {
            **fingerprint,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.copied += 1

    def result(self) -> dict:
        ok = not self.errors or len(self.errors) <= max(1, self.files) * _ERROR_FRACTION
        out: dict[str, Any] = {
            "ok": ok,
            "files": self.files,
            "copied": self.copied,
            "unchanged": self.unchanged,
            "sidecars": self.sidecars,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
        }
        if self.generations:
            out["generations"] = self.generations
        if self.sidecars_capped:
            out["sidecars_capped"] = self.sidecars_capped
        if self.errors:
            out["errors"] = self.errors[:10]
            out["error_count"] = len(self.errors)
        return out


def _file_targets(watcher) -> Iterator[Path]:
    for path, _source_id in watcher.iter_files():
        yield path


def _db_targets(watcher) -> Iterator[Path]:
    for db_path, _scan, _label in watcher._targets():  # noqa: SLF001 — see note below
        if db_path is not None:
            yield db_path


def mirror_sources(home: Optional[str] = None) -> dict:
    """Sweep every enabled source into the mirror. Returns the summary it also
    records as the ``source_mirror_last`` health record.

    Built on the watcher shapes rather than a second path registry, so the
    mirror covers exactly what ingest covers — a plugin provider's files join
    automatically, and the two can't drift apart. ``DbScanWatcher._targets``
    is package-internal by name, but this module and the watchers are one
    codebase; a third watcher shape lands in ``unsupported`` loudly rather
    than being half-mirrored."""
    from .._watcher.sources import DbScanWatcher, FileSessionWatcher, enabled_watchers

    started = time.monotonic()
    root = resolve_paths(home).home / MIRROR_SUBDIR
    providers: dict[str, dict] = {}
    unsupported: list[str] = []
    ok = True
    for watcher in enabled_watchers(home):
        name = watcher.source_name
        try:
            if not watcher.is_available():
                continue
        except Exception:  # noqa: BLE001 — availability probe must not stop the sweep
            logger.exception("source mirror: availability probe failed for %s", name)
            continue
        sweep = _ProviderSweep(root / name)
        try:
            if isinstance(watcher, FileSessionWatcher):
                for transcript in _file_targets(watcher):
                    sweep.mirror_file(transcript)
                    sweep.mirror_sidecars(transcript)
            elif isinstance(watcher, DbScanWatcher):
                for db_path in _db_targets(watcher):
                    sweep.mirror_sqlite(db_path)
            else:
                unsupported.append(name)
                continue
            _save_manifest(sweep.root, sweep.manifest)
        except Exception as e:  # noqa: BLE001 — one provider must not stop the rest
            logger.exception("source mirror: sweep failed for %s", name)
            sweep.errors.append(f"sweep: {type(e).__name__}: {e}")
            try:
                _save_manifest(sweep.root, sweep.manifest)
            except OSError:
                pass
        providers[name] = sweep.result()
        ok = ok and providers[name]["ok"]
    result = {
        "ok": ok,
        "root": str(root),
        "providers": providers,
        "unsupported": unsupported,
        "duration_s": round(time.monotonic() - started, 1),
    }
    record_health("source_mirror_last", {
        "ok": ok,
        "copied": sum(p["copied"] for p in providers.values()),
        "files": sum(p["files"] for p in providers.values()),
        "bytes_out": sum(p["bytes_out"] for p in providers.values()),
        "errors": sum(p.get("error_count", 0) for p in providers.values()),
        "unsupported": unsupported,
        "duration_s": result["duration_s"],
    })
    return result
