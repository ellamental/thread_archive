"""The registry of known archives: ``~/.thread/archives.json``.

A thread-archive *home* is self-describing — its truth log, index, and load state
all live under it — but nothing has ever recorded that a home *exists*. That makes
one archive the only archive anything can talk about: to report on a home you must
already be inside it, so "what archives do I have, and what state is each one in"
has no answer, and the n=1 case is indistinguishable from the n=many case it is
supposed to be an instance of.

This registry is that missing list. Every :func:`~thread_archive._api.open_archive`
registers its home, so an archive becomes known by being used — no separate
enrollment step to forget. Entries carry the resolved home path, a display label,
and when the archive was first seen and last opened; the *interesting* state
(loading, in-flight progress, counts) is not duplicated here but read from each
home on demand by :func:`list_archives`, so this file cannot go stale about
anything except which homes exist.

Lives beside the default home in the family's ``~/.thread/`` namespace rather than
inside any one archive — an archive must not be the authority on whether other
archives exist. ``$THREAD_ARCHIVE_REGISTRY`` overrides the path;
``THREAD_ARCHIVE_REGISTRY=0`` disables registration entirely.

Advisory and fail-soft: a registry write must never break opening an archive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_OFF = frozenset({"0", "false", "no", "off"})

# Re-registering on every open would rewrite the file on every API call for no new
# information. An in-process memo keeps a hot path (an MCP server calling
# open_archive per request) down to one write per home per interval.
_REGISTER_INTERVAL_S = 300.0
_last_registered: dict[str, float] = {}


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_REGISTRY", "1").strip().lower() not in _OFF


def registry_path() -> Path:
    """``$THREAD_ARCHIVE_REGISTRY`` if set, else ``~/.thread/archives.json``.

    Resolved per call, never frozen into a constant: ``$HOME`` is configuration
    (a test sandbox redirects it), and a constant captures whatever it said at
    import and then ignores it."""
    override = os.environ.get("THREAD_ARCHIVE_REGISTRY", "").strip()
    if override and override.lower() not in _OFF:
        return Path(override).expanduser()
    return Path.home() / ".thread" / "archives.json"


def archive_id(home: Path) -> str:
    """Stable short id for a home — a digest of its resolved path, so the same
    archive keeps its id across runs and two homes never collide on their basename."""
    return hashlib.sha256(str(Path(home).resolve()).encode("utf-8")).hexdigest()[:12]


def default_label(home: Path) -> str:
    """A display name for ``home``: its directory name, qualified by the parent when
    that name is the generic one every install shares (``~/.thread/archive``)."""
    p = Path(home)
    if p.name in ("archive", "thread-archive") and p.parent.name not in ("", "/"):
        parent = p.parent.name
        return p.name if parent == ".thread" else f"{parent}/{p.name}"
    return p.name or str(p)


def _read(path: Optional[Path] = None) -> dict:
    try:
        data = json.loads((path or registry_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"archives": []}
    if not isinstance(data, dict) or not isinstance(data.get("archives"), list):
        return {"archives": []}
    return data


def read_registry(path: Optional[Path] = None) -> list[dict]:
    """Every known archive entry, as recorded — no per-home state read."""
    return [a for a in _read(path).get("archives", []) if isinstance(a, dict)]


def merge_entry(archives: list[dict], home: Path, *, at: str,
                label: Optional[str] = None) -> list[dict]:
    """The entry list with ``home`` present and stamped ``last_opened=at``.

    Pure — the read-modify-write half of registration with the file I/O taken out,
    so the merge rule (first sighting is preserved, label is not overwritten by the
    default) is testable on its own."""
    aid = archive_id(home)
    out = [dict(a) for a in archives if isinstance(a, dict)]
    for entry in out:
        if entry.get("id") == aid:
            entry["home"] = str(home)
            entry["last_opened"] = at
            if label:
                entry["label"] = label
            entry.setdefault("first_seen", at)
            return out
    out.append({"id": aid, "home": str(home), "label": label or default_label(home),
                "first_seen": at, "last_opened": at})
    return out


def register(home: Path, *, label: Optional[str] = None, force: bool = False) -> None:
    """Record ``home`` as a known archive (locked read-modify-write).

    Throttled per process so the hot path doesn't rewrite the file on every open;
    ``force`` bypasses the throttle. Fail-soft — any error is logged and swallowed,
    because failing to note that an archive exists must never stop it opening."""
    if not _enabled():
        return
    key = str(home)
    now = time.monotonic()
    if not force and now - _last_registered.get(key, 0.0) < _REGISTER_INTERVAL_S:
        return
    _last_registered[key] = now
    import fcntl

    at = datetime.now(timezone.utc).isoformat()
    try:
        path = registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = _read(path)
            data["archives"] = merge_entry(data.get("archives", []), home, at=at, label=label)
            tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            os.close(fd)  # closing the fd releases the flock
    except OSError as e:
        logger.debug("archive registry: register failed (%s)", e)


def forget(home: Path) -> bool:
    """Drop ``home`` from the registry. True when an entry was removed. The archive
    itself is untouched — this forgets that we know about it, nothing more."""
    import fcntl

    aid = archive_id(home)
    _last_registered.pop(str(home), None)
    try:
        path = registry_path()
        if not path.exists():
            return False
        fd = os.open(path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = _read(path)
            kept = [a for a in data.get("archives", []) if a.get("id") != aid]
            if len(kept) == len(data.get("archives", [])):
                return False
            data["archives"] = kept
            tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            os.close(fd)
    except OSError as e:
        logger.debug("archive registry: forget failed (%s)", e)
        return False
    return True


def describe(entry: dict, *, active_home: Optional[Path] = None) -> dict:
    """One registry entry enriched with the state read live from its home: whether
    it still exists, its load state, and its size on disk. Read-only and cheap —
    it stats the home and reads one small JSON, never opens the index."""
    from .load_runs import read_state

    home = Path(entry.get("home", ""))
    out: dict[str, Any] = dict(entry)
    out["exists"] = home.is_dir()
    out["active"] = (active_home is not None
                     and str(Path(active_home).resolve()) == str(home.resolve())
                     if out["exists"] else False)
    if not out["exists"]:
        out["load"] = {}
        return out
    out["load"] = read_state(home)
    try:
        index = home / "index.db"
        out["index_bytes"] = index.stat().st_size if index.exists() else 0
    except OSError:
        out["index_bytes"] = 0
    return out


def list_archives(*, active_home: Optional[Path] = None) -> list[dict]:
    """Every known archive with its current load state, newest-opened first.

    The one call that answers "what archives do I have and what is each one
    doing" — including archives this process has not opened."""
    entries = [describe(e, active_home=active_home) for e in read_registry()]
    entries.sort(key=lambda e: str(e.get("last_opened") or ""), reverse=True)
    return entries
