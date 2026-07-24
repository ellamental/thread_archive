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
when the archive was first seen and last opened, and optionally a *role* — a
short descriptive tag (``live``, ``benchmark``, ``snapshot``) that says what an
archive is *for*, set via :func:`set_role` and shown wherever archives are
listed. A role is purely descriptive: it grants nothing and gates nothing. The
*interesting* state (loading, in-flight progress, counts) is not duplicated here
but read from each home on demand by :func:`list_archives`, so this file cannot
go stale about anything except which homes exist and what they are called.

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
from contextlib import contextmanager
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


@contextmanager
def suppress_registration():
    """Keep scratch homes out of the registry.

    A restore drill's temp home or a restore's staging directory is opened like
    any archive — and every open inside the block (including re-entrant opens on
    the read path) would advertise it as one of the user's archives. Ephemeral
    homes are workspace, not archives; this scopes the registry kill-switch env
    to the block and restores whatever was set before (tests point
    ``THREAD_ARCHIVE_REGISTRY`` at a sandbox path)."""
    prior = os.environ.get("THREAD_ARCHIVE_REGISTRY")
    os.environ["THREAD_ARCHIVE_REGISTRY"] = "0"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("THREAD_ARCHIVE_REGISTRY", None)
        else:
            os.environ["THREAD_ARCHIVE_REGISTRY"] = prior


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


ROLE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"


def resolve_ref(ref: str, entries: Optional[list[dict]] = None) -> Optional[dict]:
    """The registry entry ``ref`` names — matched as id, then label, then home path.

    Returns None when nothing matches; raises ``ValueError`` when a label matches
    more than one entry (labels are display names, nothing forbids a collision —
    an ambiguous ref must not silently pick one)."""
    rows = entries if entries is not None else read_registry()
    ref_s = str(ref)
    for key in ("id", "label"):
        hits = [e for e in rows if e.get(key) == ref_s]
        if len(hits) > 1:
            raise ValueError(
                f"{ref_s!r} matches {len(hits)} archives by {key} — "
                "use the id or home path"
            )
        if hits:
            return hits[0]
    home_s = str(Path(ref_s).expanduser())
    aid = archive_id(Path(home_s))
    for e in rows:
        if e.get("home") == home_s or e.get("id") == aid:
            return e
    return None


def set_role(ref: str, role: Optional[str]) -> dict:
    """Set (or clear, with ``role=None``) the descriptive role on the entry ``ref``
    names. Locked read-modify-write like every registry mutation; returns the
    updated entry. Raises ``KeyError`` for an unknown ref, ``ValueError`` for an
    ambiguous one or a malformed role."""
    import fcntl
    import re

    if role is not None and not re.match(ROLE_PATTERN, role):
        raise ValueError(
            f"role {role!r} must be 1-32 chars of lowercase [a-z0-9-], "
            "starting alphanumeric (e.g. 'live', 'benchmark', 'snapshot')"
        )
    if not _enabled():
        raise KeyError("archive registry is disabled (THREAD_ARCHIVE_REGISTRY=0)")
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = _read(path)
        entry = resolve_ref(ref, data.get("archives", []))
        if entry is None:
            raise KeyError(f"no registered archive matches {ref!r}")
        if role is None:
            entry.pop("role", None)
        else:
            entry["role"] = role
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return dict(entry)
    finally:
        os.close(fd)  # closing the fd releases the flock


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


# Recent load runs carried inline per archive. The health view wants "which
# archives are loading, which are loaded, and what did past loads cost" from one
# fetch — a per-archive history request would be an N+1 against N small files for
# no benefit. Bounded because a ledger grows forever and a health render must not
# scale with it.
_RUNS_PER_ARCHIVE = 8


def describe(entry: dict, *, active_home: Optional[Path] = None,
             runs: int = _RUNS_PER_ARCHIVE) -> dict:
    """One registry entry enriched with the state read live from its home: whether
    it still exists, its load state, its recent load runs, and its size on disk.
    Read-only and cheap — it stats the home and reads two small files, never opens
    the index. ``runs=0`` skips the history read."""
    from .load_runs import read_runs, read_state

    home = Path(entry.get("home", ""))
    out: dict[str, Any] = dict(entry)
    out["exists"] = home.is_dir()
    out["active"] = (active_home is not None
                     and str(Path(active_home).resolve()) == str(home.resolve())
                     if out["exists"] else False)
    if not out["exists"]:
        out["load"] = {}
        out["runs"] = []
        return out
    out["load"] = read_state(home)
    out["runs"] = read_runs(runs, home) if runs else []
    try:
        index = home / "index.db"
        out["index_bytes"] = index.stat().st_size if index.exists() else 0
    except OSError:
        out["index_bytes"] = 0
    return out


def list_archives(*, active_home: Optional[Path] = None,
                  runs: int = _RUNS_PER_ARCHIVE) -> list[dict]:
    """Every known archive with its current load state and recent load history,
    newest-opened first.

    The one call that answers "what archives do I have and what is each one
    doing" — including archives this process has not opened."""
    entries = [describe(e, active_home=active_home, runs=runs) for e in read_registry()]
    entries.sort(key=lambda e: str(e.get("last_opened") or ""), reverse=True)
    return entries
