"""Per-file poll fingerprints that survive a restart: ``<home>/watch-fingerprints.json``.

The file watchers skip an unchanged transcript on its ``(mtime_ns, size)``
fingerprint, held in memory for the life of the process. That cache starts empty,
so the first poll after every restart reads and digests **every** file in the
archive to re-derive what it already knew — measured here at 1,983 files and
1.4 GB per restart, and the watcher restarts on the order of twenty times a day
(a deploy, an edit, a crash). The work is pure rediscovery: essentially none of
those files changed.

Persisting the fingerprints removes that. What it also removes, if left alone, is
a property nobody designed but everyone was relying on: because the cache died
with the process, each restart happened to re-verify the whole corpus against its
watermarks — a real integrity sweep, running by accident at whatever rate the
daemon happened to bounce.

So the sweep stays; it just becomes deliberate. A cache older than
:func:`reverify_after_s` is ignored, which forces exactly the full pass a restart
used to force — on a schedule chosen for it rather than one set by how often the
process died. The default (6 hours) keeps several full verifications a day at a
fraction of the cost.

Fail-safe in every direction: a missing, unreadable, corrupt, or stale cache
yields no fingerprints, and no fingerprints means a full scan — the behavior
before this file existed. It can lose work but never invent it: a fingerprint that
is absent costs one re-read, while a fingerprint that is *wrong* would skip a real
change, so nothing here is written that was not observed this run.

Writes are throttled hard. The map is a few hundred KB and the loop polls every
few seconds; saving per poll would spend more in writes than the reads it saves.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

STATE_FILE = "watch-fingerprints.json"

#: Minimum seconds between saves, per process. The cache is an optimization, so
#: losing the last few minutes of it to a kill costs a re-read and nothing else.
_SAVE_INTERVAL_S = 300.0

_last_saved: dict[str, float] = {}


def reverify_after_s() -> float:
    """Age at which a persisted cache is ignored, forcing a full re-verify pass.

    Read per call from ``THREAD_ARCHIVE_FINGERPRINT_TTL_S`` so an operator can
    tighten it (or set ``0`` to disable persistence entirely and re-verify on
    every poll cycle, the pre-persistence behavior)."""
    raw = os.environ.get("THREAD_ARCHIVE_FINGERPRINT_TTL_S")
    if raw is None:
        return 6 * 3600.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 6 * 3600.0


def _path(home=None):
    from .._config import resolve_paths

    return resolve_paths(home).home / STATE_FILE


def _read(home=None) -> dict:
    try:
        with open(_path(home), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def load(source: str, home=None) -> dict[str, tuple[int, int]]:
    """This source's persisted fingerprints, or ``{}`` when there are none to
    trust — missing, unreadable, or past :func:`reverify_after_s`.

    ``{}`` is never an error: it means this poll re-derives the fingerprints by
    reading the files, which is what the watcher did before any of this existed.
    """
    ttl = reverify_after_s()
    if ttl <= 0:
        return {}
    try:
        doc = _read(home)
        stamped = doc.get("verified_at")
        if not isinstance(stamped, str):
            return {}
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamped)).total_seconds()
        if age > ttl or age < 0:  # stale, or a clock that moved backwards
            return {}
        entries = (doc.get("sources") or {}).get(source) or {}
        out: dict[str, tuple[int, int]] = {}
        for key, value in entries.items():
            # Shape-checked per entry rather than trusted wholesale: a truncated or
            # hand-edited file must degrade to a re-read, never to a bad skip.
            if (isinstance(key, str) and isinstance(value, list) and len(value) == 2
                    and all(isinstance(v, int) for v in value)):
                out[key] = (value[0], value[1])
        return out
    except Exception:  # noqa: BLE001 — advisory; a bad cache must not break a poll
        logger.debug("watch: could not load fingerprints", exc_info=True)
        return {}


def save(source: str, seen: dict[str, tuple[int, int]], home=None, *,
         force: bool = False) -> bool:
    """Persist this source's fingerprints, at most once per throttle interval.

    Returns whether a write happened. Merges into the other sources' entries
    rather than replacing the document, so each watcher owns only its own key.

    ``verified_at`` is stamped on write and is what :func:`load` ages: it marks
    when this process last *observed* the files, which is what the re-verify
    window is about."""
    now = time.monotonic()
    if not force and now - _last_saved.get(source, float("-inf")) < _SAVE_INTERVAL_S:
        return False
    try:
        path = _path(home)
        doc = _read(home)
        raw = doc.get("sources")
        sources: dict[str, Any] = raw if isinstance(raw, dict) else {}
        sources[source] = {k: [v[0], v[1]] for k, v in seen.items()}
        payload = {"verified_at": datetime.now(timezone.utc).isoformat(), "sources": sources}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a reader never sees a half-written map
        _last_saved[source] = now
        return True
    except Exception:  # noqa: BLE001 — advisory; the loop must survive
        logger.debug("watch: could not save fingerprints", exc_info=True)
        return False


def clear(home=None) -> None:
    """Drop every persisted fingerprint — the next poll re-verifies in full."""
    try:
        _path(home).unlink()
    except OSError:
        pass
    _last_saved.clear()


def forget(source: str, home=None) -> bool:
    """Drop one source's persisted fingerprints; the next poll re-reads its files.

    What a repair needs and a reset watermark alone cannot do. The watchers skip on
    the fingerprint *before* the watermark is ever consulted, so a drifted file that
    has since stopped changing is skipped by a poll no matter how thoroughly its
    watermark was cleared — and the ledger-driven re-import
    (:func:`thread_archive._repair.reimport_source`) would report a clean run having
    re-read nothing. Whole-source rather than per-file: the cost is one re-verify
    pass of that source, which is the pass a restart used to force anyway, and it
    needs no reconstruction of provider-specific paths from source ids.

    Returns whether anything was written. The other sources' entries are preserved.
    """
    try:
        doc = _read(home)
        raw = doc.get("sources")
        sources: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if source not in sources:
            return False
        sources.pop(source)
        path = _path(home)
        # The stamp is left as it was: this drops what one source knows, and
        # re-dating the document would silently extend every other source's cache.
        payload = {"verified_at": doc.get("verified_at"), "sources": sources}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        _last_saved.pop(source, None)
        return True
    except Exception:  # noqa: BLE001 — advisory; a repair must not break on it
        logger.debug("watch: could not forget fingerprints for %s", source, exc_info=True)
        return False


def stamp_age_s(home=None) -> Optional[float]:
    """Seconds since the last full observation, or ``None`` when never stamped —
    the operator-visible answer to "when was the corpus last verified"."""
    doc = _read(home)
    stamped = doc.get("verified_at")
    if not isinstance(stamped, str):
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamped)).total_seconds()
    except ValueError:
        return None
