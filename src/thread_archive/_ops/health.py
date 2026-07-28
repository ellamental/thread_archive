"""Operational health records (``<home>/health.json``) + the pipeline verdict.

When verify / backup / the nightly last ran and how they went — the staleness
signal that tells a dead scheduled job apart from a healthy one. Deliberately
OUTSIDE the truth dir: this is install-local operational state, so the truth
mirror doesn't carry it and the backup can't dirty the tree it is mirroring. A
reference snapshot rides the backup's ``.recovery`` bundle so the history
survives the loss of the home, but ``thread-archive backup restore`` never installs it — a
restored home must not claim the source install's health history.

The verdict half (:func:`pipeline_verdict` / :func:`stamp_heartbeat`) turns those
records into the family-monitor heartbeat: the last nightly's failed stages,
minus every stage a later at-least-as-strong run has since proven good.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .._config import resolve_paths


def _health_path() -> Path:
    return resolve_paths().home / "health.json"


def read_health() -> dict:
    try:
        return json.loads(_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_health(key: str, record: dict) -> None:
    """Set ``key`` to ``record`` (stamped with ``at``) — a locked read-modify-
    write (exclusive flock on ``health.json.lock``, same discipline as the
    manifest's): concurrent completions (a verify racing a backup, the nightly's
    stages) each own different keys, and an unlocked whole-file replace would
    lose whichever writer published first. Advisory data, so a failed write
    logs and never breaks the operation it describes."""
    import fcntl
    from datetime import datetime, timezone

    try:
        p = _health_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p.with_name(f"{p.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            health = read_health()
            health[key] = {"at": datetime.now(timezone.utc).isoformat(), **record}
            tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(health, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        finally:
            os.close(fd)  # closing the fd releases the flock
    except OSError:
        import logging

        logging.getLogger(__name__).exception("could not record %s in health.json", key)


def clear_health(key: str) -> None:
    """Remove ``key`` from ``health.json`` if present — the clear-on-green
    counterpart to :func:`record_health`, under the same exclusive flock. A
    failure-only record (``watch_errors_last``) can otherwise only ever go red:
    it is written when a fault occurs and nothing retires it, so a stale fault —
    or one from a previous daemon run — keeps painting ``thread-archive status`` red
    long after the source recovered. A no-op (no write) when the key is absent,
    so the green path costs a lock and a read, not a rewrite. Advisory: a failed
    clear logs and never breaks the caller."""
    import fcntl

    try:
        p = _health_path()
        if not p.exists():
            return
        fd = os.open(p.with_name(f"{p.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            health = read_health()
            if key not in health:
                return
            del health[key]
            tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(health, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        finally:
            os.close(fd)  # closing the fd releases the flock
    except OSError:
        import logging

        logging.getLogger(__name__).exception("could not clear %s in health.json", key)


# Each pipeline stage, mapped to the health record that a later, out-of-band run
# of that same stage writes. This is what lets a stage's failure be *retired* by
# evidence rather than only by another full nightly.
_STAGE_RECORD = {
    "backup": "backup_last",
    "verify": "verify_last",
    "restore-drill": "restore_drill_last",
    "coverage": "coverage_last",
}

# A stage failure can also be *tolerated* — kept off the degraded board without a
# re-run — while a recent enough green record of that stage still stands. This
# forgives a *transient* failure of an expensive, resource-dependent stage, and is
# distinct from recovery (which demands a postdating, at-least-as-strong re-run):
# tolerance accepts a *prior* success, on the argument that it was still true.
#
# Only the restore-drill qualifies. It is ~1h of work that reads the backup
# mirror — possibly an intermittently-mounted network volume — so one failed drill
# with a good drill days behind it means "the backup restored fine last week and
# the mount flaked tonight", not "the archive is unprotected". A real restore-path regression
# fails *every* night and trips the board once the last good drill ages out of the
# window. Freshness is untouched: a job that stops RUNNING still goes stale (the
# monitor's >30h check on nightly_at), tolerance only softens a run that ran and
# failed. verify/backup/coverage get no grace — they are cheap and local, so a
# failure there is real and must show the next morning.
_STAGE_GRACE_DAYS = {"restore-drill": 14}


def _parse_at(value: object) -> Optional[float]:
    """Epoch seconds from a health/heartbeat ``at`` stamp; None if absent or bad."""
    from datetime import datetime, timezone

    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def _stage_recovered(stage: str, nightly: dict, health: dict) -> bool:
    """Has ``stage`` — which failed in the last nightly — since been proven good?

    True only when that stage's own health record is green, **postdates** the
    nightly that failed, and came from a check **at least as strong** as the one
    that failed.

    Strength is what makes this safe, and it matters for verify alone: the
    nightly escalates verify on age gates (``deep``, ``hashes``, and the mirror
    parse-scan, which rides ``--backup`` exactly when deep is due). A later
    *basic* verify passing says nothing about a deep tier that failed, so
    clearing on it would be a false green — the cheap check laundering the
    expensive red. Requiring ≥ strength is the whole guard; without it this
    function is a bug, not a feature.
    """
    rec = health.get(_STAGE_RECORD.get(stage, ""))
    if not isinstance(rec, dict) or not rec.get("ok"):
        return False
    when, nightly_at = _parse_at(rec.get("at")), _parse_at(nightly.get("at"))
    if when is None or nightly_at is None or when <= nightly_at:
        return False
    if stage != "verify":
        return True
    # The nightly's verify parse-scans the mirror exactly when its deep tier is
    # due, so `deep` gates the backup-scan requirement as well as its own.
    deep = bool(nightly.get("deep"))
    for tier, required in (("deep", deep), ("hashes", bool(nightly.get("hashes"))),
                           ("backup", deep)):
        if required and not rec.get(tier):
            return False
    return True


def _stage_tolerated(stage: str, health: dict) -> bool:
    """Is ``stage`` within its grace window — a **green** record of that stage no
    older than its ``_STAGE_GRACE_DAYS``? Unlike recovery, the success need not
    postdate the failing nightly: a recent prior success is what forgives a
    transient failure. A stage with no grace, no record, or a record that is
    absent / not green / unparseable is never tolerated."""
    grace = _STAGE_GRACE_DAYS.get(stage)
    if grace is None:
        return False
    rec = health.get(_STAGE_RECORD.get(stage, ""))
    if not isinstance(rec, dict) or not rec.get("ok"):
        return False
    when = _parse_at(rec.get("at"))
    if when is None:
        return False
    from datetime import datetime, timezone

    return (datetime.now(timezone.utc).timestamp() - when) <= grace * 86400


def pipeline_verdict(health: Optional[dict] = None) -> dict:
    """The pipeline's state **now** — not merely a transcript of the last nightly.

    The last nightly's failed stages, minus every stage a later at-least-as-strong
    run has since proven good, minus every stage still within its grace window
    (see ``_STAGE_GRACE_DAYS``).

    Without this, the *only* thing that can retire a fault is another full nightly
    (~1h, restore-drill dominated). An operator who fixes the cause and proves it
    fixed — at a stronger tier than the one that failed — still faces a board
    asserting the archive is unprotected until 04:00 comes around. That is a stale
    alarm on the one signal that says whether the operator's memory is recoverable, and a
    signal that keeps crying after the fire is out is one that stops being read.
    """
    health = read_health() if health is None else health
    nightly = health.get("nightly_last") or {}
    failed = [s for s in (nightly.get("failed_stages") or []) if isinstance(s, str)]
    recovered = [s for s in failed if _stage_recovered(s, nightly, health)]
    remaining = [s for s in failed if s not in recovered]
    tolerated = [s for s in remaining if _stage_tolerated(s, health)]
    unresolved = [s for s in remaining if s not in tolerated]
    return {
        "ran": bool(nightly),
        "ok": not unresolved,
        "failed_stages": unresolved,
        "recovered_stages": recovered,
        "tolerated_stages": tolerated,
        "nightly_at": nightly.get("at"),
        "dest": nightly.get("dest"),
    }


def heartbeat_path() -> Path:
    """The family-monitor heartbeat this archive stamps.

    ``~/.thread/logs`` is the thread family's shared heartbeat ground; the env
    override exists so tests never touch the real box's beat. Resolved per call,
    never frozen: it follows ``$HOME`` and the override the way every other
    location in the product does.
    """
    hb_dir = Path(
        os.environ.get("THREAD_ARCHIVE_HEARTBEAT_DIR")
        or Path.home() / ".thread" / "logs"
    )
    return hb_dir / "archive-nightly.heartbeat"


def stamp_heartbeat() -> None:
    """Publish the pipeline verdict to the family-monitor heartbeat.

    thread-monitor freshness-checks periodic jobs via ``~/.thread/logs`` (the
    shared heartbeat ground — it never reads sibling products' private stores, so
    health.json alone is invisible to it). This file is therefore the entire
    contract, and it carries the two independent facts the monitor needs:

    - ``nightly_at`` — when the nightly last *ran*. The monitor anchors its
      staleness check on this field, **not** on the file's mtime, because every
      out-of-band stage run below rewrites the file: mtime would report "a run
      happened" on a box whose nightly job has been dead for a week.
    - ``ok`` / ``failed_stages`` — the verdict with recovered stages retired (and
      grace-tolerated stages held off; see ``tolerated_stages``), so a proven
      out-of-band fix — or a transient failure of a stage with recent good
      evidence — clears the board without waiting out another pipeline.

    Stamped on every nightly completion whatever the outcome, and again whenever a
    stage is re-run on its own. Fail-soft, and skipped entirely when the dir does
    not exist (an install outside the thread family has no monitor to feed) or when
    no nightly has ever run (the monitor reads an absent heartbeat as "awaiting
    first run" — a lone stage run must not pre-empt that). The env override exists
    so tests never stamp the real box's heartbeat.
    """
    from datetime import datetime, timezone

    path = heartbeat_path()
    if not path.parent.is_dir():
        return
    verdict = pipeline_verdict()
    if not verdict["ran"]:
        return
    try:
        path.write_text(
            json.dumps({
                "at": datetime.now(timezone.utc).isoformat(),
                "nightly_at": verdict["nightly_at"],
                "ok": verdict["ok"],
                "failed_stages": verdict["failed_stages"],
                "recovered_stages": verdict["recovered_stages"],
                "tolerated_stages": verdict["tolerated_stages"],
                "dest": verdict["dest"],
            }) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
