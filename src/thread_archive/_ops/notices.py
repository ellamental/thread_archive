"""The action queue: what the archive is asking someone to do, and what they've silenced.

The operational records (``health.json``, the pipeline verdict, the library
matrix) are *facts* — when a stage last ran, what it found. A notice is the
**judgment** over those facts, with the remedy attached: "capture is stale",
"the backup shares a filesystem with the archive", and the command that fixes
it. Built here rather than in the viewer so one implementation answers every
surface, and so a silence can be honored by all of them.

Silences live in ``<home>/silenced-notices.json``, install-local operational
state alongside ``health.json`` and outside the truth dir for the same reason:
the backup must not have to mirror an operator's UI choices.

A silence is bound to the *condition*, never to the key alone:

- it carries the notice's **fingerprint** — the notice's own text with counts,
  ages, and timestamps elided — so a condition that changes shape (a second
  failing stage, a different degradation reason) is a new thing to look at and
  speaks up again, while a warning that only ages ("47d" → "48d") stays quiet;
- it **retires the moment the notice stops firing** (:func:`notice_board`
  prunes it), so a fault that is fixed and later comes back is never hidden by
  the silence someone made about the first occurrence.

Together those two rules mean a silence can only ever hide the exact condition
the operator read and dismissed. That is what makes silencing safe on a page
whose whole job is to be believed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .._config import resolve_paths

log = logging.getLogger(__name__)

_MINUTE = 60.0
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR

# A capture pass that hasn't landed in this long is a stalled watcher, not a
# quiet machine: the watcher polls on a far shorter cycle even with nothing to do.
_CAPTURE_STALE_S = 15 * _MINUTE
# The protection pipeline runs nightly; past this the scheduled job itself is
# suspect, with a day and a half of slack for a laptop that was asleep at 04:00.
_PIPELINE_STALE_S = 36 * _HOUR


# ---------------------------------------------------------------------------
# formatting the judgment
# ---------------------------------------------------------------------------
def _elapsed(iso: object) -> Optional[float]:
    """Seconds since an ISO stamp; None when it is absent or unparseable."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, datetime.now(timezone.utc).timestamp() - when.timestamp())


def _age(iso: object) -> str:
    """A human age for a record stamp, at the resolution the reader acts on."""
    seconds = _elapsed(iso)
    if seconds is None:
        return "never"
    if seconds < _MINUTE:
        return "just now"
    if seconds < _HOUR:
        return f"{int(seconds // _MINUTE)}m ago"
    if seconds < _DAY:
        return f"{int(seconds // _HOUR)}h ago"
    return f"{int(seconds // _DAY)}d ago"


def _count(n: object) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) and not isinstance(n, bool) else "—"


_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")


def _shell_arg(value: str) -> str:
    """Quote a path into the command a notice prints, so it can be pasted as-is."""
    if _SAFE_ARG.match(value):
        return value
    escaped = value.replace("'", "'\"'\"'")
    return f"'{escaped}'"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _fingerprint(key: str, title: str, detail: str) -> str:
    """The identity of the *condition* a notice reports.

    Digits are elided before hashing: a coverage warning that counts days, a
    provider line that counts parse errors, and a stale-capture title that
    carries an age all rewrite themselves constantly without the underlying
    fault changing. Hashing the raw text would expire a silence every time the
    clock moved; hashing the shape expires it when the fault moves.
    """
    shape = re.sub(r"\d+", "#", f"{key}\n{title}\n{detail}")
    return hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]


def _notice(key: str, tone: str, title: str, detail: str, command: Optional[str] = None) -> dict:
    return {
        "key": key,
        "tone": tone,
        "title": title,
        "detail": detail,
        "command": command,
        "fingerprint": _fingerprint(key, title, detail),
    }


def build_notices(records: dict) -> list[dict]:
    """Every condition worth an operator's attention, strongest concern first.

    ``records`` is the status mapping — :func:`thread_archive._api.operational_records`
    plus ``libraries``. Anything absent from it simply raises no notice: this
    reads a partial status (a half-configured install, a record no run has
    written yet) without inventing faults it can't see.

    Tone is what the notice *asks for*, not how bad it feels: ``bad`` is a hole
    in the archive's protection, ``warn`` is a fault that costs quality or
    durability margin, ``good`` is maintenance that is available rather than
    owed.
    """
    out: list[dict] = []
    pipeline = records.get("pipeline") or {}
    backup = records.get("last_backup") or {}
    nightly_record = records.get("last_nightly") or {}
    dest = pipeline.get("dest") or backup.get("dest") or nightly_record.get("dest")
    nightly_command = f"thread-archive backup nightly {_shell_arg(str(dest))}" if dest else "thread-archive setup"

    watch_pass = records.get("last_watch_pass")
    if not watch_pass:
        out.append(_notice(
            "capture-missing", "bad",
            "Capture has never reported a completed pass",
            "Run one pass now. If it succeeds, install or restart the watcher so new "
            "conversations keep arriving.",
            "thread-archive watch --once",
        ))
    elif (_elapsed(watch_pass.get("at")) or float("inf")) > _CAPTURE_STALE_S:
        out.append(_notice(
            "capture-stale", "bad",
            f"Capture is stale — last check {_age(watch_pass.get('at'))}",
            "A stalled watcher can leave recent conversations outside the archive.",
            "thread-archive watch --once",
        ))

    watch_errors = records.get("last_watch_errors")
    if watch_errors:
        out.append(_notice(
            "watch-errors", "bad",
            "A provider failed during capture",
            " · ".join(watch_errors.get("errors") or [])
            or "The latest watcher run recorded provider errors.",
            "thread-archive status",
        ))

    for source, data in ((watch_pass or {}).get("sources") or {}).items():
        if not data.get("errors") and not data.get("parse_errors"):
            continue
        out.append(_notice(
            f"source-{_slug(str(source))}", "bad",
            f"{source} is not importing cleanly",
            f"{_count(data.get('parse_errors'))} parse errors and "
            f"{_count(data.get('errors'))} watcher errors since this capture process started.",
            f"thread-archive source fix {_shell_arg(str(source))}",
        ))

    if not pipeline.get("ran"):
        out.append(_notice(
            "nightly-missing", "bad",
            "The protection pipeline has never completed",
            "Backup, integrity verification, and a restore drill have not yet been "
            "proven together.",
            nightly_command,
        ))
    elif not pipeline.get("ok"):
        out.append(_notice(
            "nightly-failed", "bad",
            f"Protection failed at {', '.join(pipeline.get('failed_stages') or []) or 'an unknown stage'}",
            "The pipeline verdict accounts for later successful reruns, so these "
            "failures are still unresolved.",
            nightly_command,
        ))
    elif (_elapsed(pipeline.get("nightly_at")) or float("inf")) > _PIPELINE_STALE_S:
        out.append(_notice(
            "nightly-stale", "bad",
            f"Protection is stale — last pipeline {_age(pipeline.get('nightly_at'))}",
            "The scheduled backup and recovery proof may have stopped running.",
            nightly_command,
        ))

    if records.get("backup_same_device") is True:
        out.append(_notice(
            "same-disk", "warn",
            "Backup is on the same filesystem as the archive",
            "This protects against index corruption and accidental deletion, but not "
            "loss of the disk. Move the scheduled destination to another disk.",
            "thread-archive service install --backup --dest /Volumes/<backup-disk>/thread-archive",
        ))

    coverage = records.get("last_coverage") or {}
    if coverage and not coverage.get("ok"):
        out.append(_notice(
            "coverage-failed", "bad",
            "Capture coverage has gaps",
            " · ".join(coverage.get("failed") or [])
            or "The coverage audit found missing or degraded source data.",
            "thread-archive source coverage",
        ))
    for warning in coverage.get("warnings") or []:
        # Keyed on the warning's subject (its text up to the first colon — the
        # source name, or the ledger the records came from), so the silence for
        # one stale export doesn't move to another when the list reorders.
        subject = _slug(str(warning).split(":", 1)[0]) or "coverage"
        out.append(_notice(
            f"coverage-warning-{subject}", "warn",
            "Coverage warning", str(warning), "thread-archive source coverage",
        ))

    # A feature running without the library that does it well is the one fault
    # nothing else on the page can show: search keeps answering, so every other
    # check stays green while ranking quality sits below the archive's own gated
    # baseline. An absent library the install has no use for is 'off' and never
    # lands here.
    for library in records.get("libraries") or []:
        if library.get("state") != "degraded":
            continue
        out.append(_notice(
            f"library-{_slug(str(library.get('name')))}", "warn",
            f"{library.get('name')} is not installed",
            f"{library.get('capability')} is degraded. {library.get('detail')}",
            "pip install 'thread-archive[all]'",
        ))

    update = records.get("last_self_update") or {}
    if update.get("action") == "update":
        out.append(_notice(
            "update", "good",
            f"{update.get('tag') or 'A new release'} is available",
            str(update.get("reason") or "Applying updates is explicit."),
            "thread-archive self-update",
        ))
    elif update and not update.get("ok"):
        out.append(_notice(
            "update-blocked", "warn",
            f"Updates are {update.get('action') or 'blocked'}",
            str(update.get("reason") or "The update check did not complete successfully."),
            "thread-archive self-update --check",
        ))

    return _uniquify(out)


def _uniquify(notices: list[dict]) -> list[dict]:
    """Guarantee distinct keys — a key is a silence's address, and two notices
    sharing one would let a silence on either hide both. Derived keys carry a
    subject (a source, a library, a warning's leading phrase) that is unique in
    practice; this is the backstop for the case where it isn't."""
    seen: dict[str, int] = {}
    for notice in notices:
        key = notice["key"]
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count > 1:
            notice["key"] = f"{key}-{count}"
            notice["fingerprint"] = _fingerprint(
                notice["key"], notice["title"], notice["detail"]
            )
    return notices


# ---------------------------------------------------------------------------
# the silence store
# ---------------------------------------------------------------------------
def _silences_path() -> Path:
    return resolve_paths().home / "silenced-notices.json"


def read_silences() -> dict[str, dict]:
    """Every silence on record: ``{key: {at, fingerprint, title}}``.

    Unreadable or malformed content reads as no silences — a corrupt file must
    fail toward *showing* warnings, never toward hiding them."""
    try:
        data = json.loads(_silences_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _update(mutate: Callable[[dict], Optional[dict]]) -> dict:
    """Locked read-modify-write of the silence file; returns what it now holds.

    Exclusive flock on a sidecar lock file, the same discipline ``health.json``
    writes under: the viewer serves its requests on threads, so two silences
    clicked in two tabs are two concurrent read-modify-writes, and an unlocked
    whole-file replace loses whichever landed first. ``mutate`` returns the new
    mapping, or None to leave the file untouched.

    Errors propagate: a silence is a deliberate act, and one that did not make it
    to disk must be reported rather than swallowed into a page that looks like it
    worked and reverts on the next poll.
    """
    path = _silences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = read_silences()
        updated = mutate(current)
        if updated is None or updated == current:
            return current
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(updated, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return updated
    finally:
        os.close(fd)  # closing the fd releases the flock


def notice_board(records: dict) -> dict:
    """The action queue as a surface should show it: ``{active, silenced}``.

    Silences are applied here and *only* here, so nothing that reads notices can
    forget to honor them. Applying them is also what retires them: a silence
    whose notice is no longer firing, or is firing with a different fingerprint,
    is dropped from the store on the spot — the condition it was made about is
    gone, and the next occurrence deserves to be seen. The prune is fail-soft;
    a read-only or full disk costs the cleanup, not the page.
    """
    built = build_notices(records)
    by_key = {n["key"]: n for n in built}
    stored = read_silences()
    live = {
        key: entry
        for key, entry in stored.items()
        if key in by_key and by_key[key]["fingerprint"] == entry.get("fingerprint")
    }
    retired = set(stored) - set(live)
    if retired:
        try:
            # Drops exactly the retired keys rather than rewriting the file to
            # `live`: another thread may have silenced something between the read
            # above and this lock, and that silence must survive the cleanup.
            _update(lambda current: {k: v for k, v in current.items() if k not in retired})
        except OSError:
            log.warning("could not prune retired silences", exc_info=True)
    return {
        "active": [n for n in built if n["key"] not in live],
        "silenced": [
            {**n, "silenced_at": live[n["key"]].get("at")}
            for n in built
            if n["key"] in live
        ],
    }


def silence(key: str, records: dict) -> dict:
    """Silence the notice named ``key``; returns the board it leaves behind.

    Only a notice that is *currently firing* can be silenced — the stored
    fingerprint has to come from a real condition, and a key that matches
    nothing is a caller error worth reporting rather than a silence that would
    sit in the file waiting to hide something later.
    """
    match = next((n for n in build_notices(records) if n["key"] == key), None)
    if match is None:
        raise KeyError(key)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": match["fingerprint"],
        "title": match["title"],
    }
    _update(lambda current: {**current, key: entry})
    return notice_board(records)


def unsilence(key: str, records: dict) -> dict:
    """Lift the silence on ``key``; returns the board it leaves behind.

    Idempotent: lifting a silence that isn't there is the state the caller asked
    for, and two tabs unsilencing the same notice must not turn the second click
    into an error.
    """
    _update(lambda current: {k: v for k, v in current.items() if k != key})
    return notice_board(records)
