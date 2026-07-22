"""The scheduled protection pipeline: source mirror → backup → verify
(escalated) → restore drill → capture coverage."""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path
from typing import Optional

from .backup import backup, restore_drill
from .coverage import check_coverage
from .health import read_health, record_health, stamp_heartbeat
from .source_mirror import mirror_sources
from .verify import verify

# Age gates for the escalated verify tiers `nightly` folds in on top of its
# nightly backup + shallow verify + restore drill.
_DEEP_EVERY_DAYS = 7
_HASHES_EVERY_DAYS = 30


def _health_is_due(key: str, every_days: float) -> bool:
    """True when health record ``key`` is missing, unparseable, stale, or was
    not ok — the age gate that replaces weekday/day-of-month schedule math: a
    missed (machine off) or failed escalated pass makes the *next* nightly run
    pick it up, instead of waiting for the calendar to come around again."""
    from datetime import datetime, timezone

    rec = read_health().get(key)
    if not isinstance(rec, dict):
        return True
    try:
        at = datetime.fromisoformat(rec["at"])
    except (TypeError, KeyError, ValueError):
        return True
    if not rec.get("ok"):
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() >= every_days * 86400


def _stage_error(e: Exception) -> str:
    """Format a stage exception, naming macOS TCC when the shape matches.

    EPERM (errno 1 — distinct from a unix permission denial's EACCES 13) from
    plain file operations on macOS is the signature of a TCC privacy denial:
    a launchd job without its own grant gets a blanket EPERM on gated paths
    (network volumes especially) while the identical operation succeeds from
    an interactive shell, whose terminal app holds the grant. Without the
    hint, the failure reads as filesystem breakage and gets debugged at the
    wrong layer."""
    msg = f"{type(e).__name__}: {e}"
    if (
        sys.platform == "darwin"
        and isinstance(e, PermissionError)
        and e.errno == errno.EPERM
    ):
        msg += (
            " [EPERM on macOS is usually a TCC privacy denial for this process"
            " context: background (launchd) jobs need their own grant — for a"
            " network-volume dest, System Settings → Privacy & Security →"
            " Files & Folders → Network Volumes for the job's interpreter."
            " The same operation succeeding in an interactive shell confirms"
            " the diagnosis.]"
        )
    return msg


def _notify(url: str, message: str) -> Optional[str]:
    """POST a notification (lab's ``/api/notify`` shape: ``{title, message}``).
    Fail-soft — returns an error string instead of raising: health.json and the
    job log are the durable record; the push is best-effort."""
    import urllib.request

    body = json.dumps({"title": "thread-archive", "message": message}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def nightly(
    dest: str,
    *,
    home: Optional[str] = None,
    notify_url: Optional[str] = None,
    allow_shrink: bool = False,
    drill: bool = True,
) -> dict:
    """The scheduled protection pipeline, as one command: source ``mirror`` →
    ``backup`` → ``verify`` (with age-gated escalation) → ``restore_drill`` →
    capture ``coverage`` — every night.

    Replaces a shell chain of the three commands. The differences that matter:

    - **Every stage runs** (no ``&&`` short-circuit): a failed backup must not
      also cost the night's integrity check and drill — each stage's outcome is
      recorded separately (its own health.json record plus ``nightly_last``),
      so an alert can say *which* stage broke while the others' green stays
      visible.
    - **Escalation is age-gated, not calendar-gated**: the deep verify (+ the
      mirror parse-scan) folds in when ``verify_deep_last`` is missing, older
      than ``_DEEP_EVERY_DAYS``, or failed; ``--hashes`` likewise on
      ``_HASHES_EVERY_DAYS``. A machine that was off on the scheduled day runs
      the escalated pass on its next nightly instead of a month later.
    - **The drill is nightly.** The restore path is code and the code changes
      daily; a restore-path regression must surface the next morning, not up
      to a month later. Roughly an hour of nice'd 4 a.m. work at current size
      (the drill's full index rebuild dominates — ~50 min on the live archive).
    - **Failure notifies** (``notify_url``, lab's ``/api/notify`` shape) with
      the failed stage names. The "never ran at all" case is the monitor's to
      catch, from the staleness of the health.json records this writes.

    Returns per-stage results plus ``ok`` / ``failed_stages``.
    """
    from .._api import open_archive

    open_archive(home)
    failed: list[str] = []
    result: dict = {"dest": str(Path(dest).expanduser())}

    # Source mirror first: raw harness stores prune on their own clocks
    # (Claude Code at ~30 days), so their capture is the most time-sensitive
    # stage — and it must not be forfeited to a failure later in the night.
    try:
        m = mirror_sources(home=home)
        mirror_ok = bool(m.get("ok"))
    except Exception as e:
        m, mirror_ok = {"error": _stage_error(e)}, False
    result["source_mirror"] = m
    if not mirror_ok:
        failed.append("source-mirror")

    try:
        b = backup(dest, home=home, allow_shrink=allow_shrink)
        backup_ok = bool(
            b["verify_ok"] and b["mirror_complete"] and not b["deletions_skipped"]
        )
    except Exception as e:
        b, backup_ok = {"error": _stage_error(e)}, False
    result["backup"] = b
    if not backup_ok:
        failed.append("backup")

    deep_due = _health_is_due("verify_deep_last", _DEEP_EVERY_DAYS)
    hashes_due = _health_is_due("verify_hashes_last", _HASHES_EVERY_DAYS)
    result["escalations"] = {"deep": deep_due, "hashes": hashes_due}
    try:
        v = verify(
            home=home, deep=deep_due, hashes=hashes_due,
            backup=str(dest) if deep_due else None,
        )
        verify_ok = bool(v["ok"])
    except Exception as e:
        v, verify_ok = {"error": _stage_error(e)}, False
    result["verify"] = v
    if not verify_ok:
        failed.append("verify")

    if drill:
        try:
            d = restore_drill(dest, home=home)
            drill_ok = bool(d.get("ok"))
        except Exception as e:
            d, drill_ok = {"error": _stage_error(e)}, False
        result["drill"] = d
        if not drill_ok:
            failed.append("restore-drill")

    # Capture coverage: the stores reconciled against the archive (see
    # _ops.coverage). The other stages protect what was captured; this one
    # asserts capture itself is still whole — a source gone dark or ingest
    # gone stale fails the night like any integrity break.
    try:
        c = check_coverage(home=home)
        coverage_ok = bool(c.get("ok"))
    except Exception as e:
        c, coverage_ok = {"error": _stage_error(e)}, False
    result["coverage"] = c
    if not coverage_ok:
        failed.append("coverage")

    result["ok"] = not failed
    result["failed_stages"] = failed
    result["drift_alert"] = _drift_alert()
    record_health("nightly_last", {
        "dest": result["dest"],
        "ok": result["ok"],
        "failed_stages": failed,
        "deep": deep_due,
        "hashes": hashes_due,
        "drill": drill,
    })
    # Publish the verdict to the family-monitor heartbeat (see stamp_heartbeat):
    # stamped on every completion whatever the outcome, and the `nightly_last`
    # record written just above is what it anchors its freshness field on.
    stamp_heartbeat()
    if failed and notify_url:
        result["notify_error"] = _notify(
            notify_url,
            f"nightly backup pipeline FAILED at: {', '.join(failed)} — "
            "see `thread_archive status`, health.json, and ~/.thread/archive/logs/backup-*.log",
        )
    # Drift is warn-never-red in coverage (one benign record must not fail the
    # night), but the ledgers exist to be READ — records written in the last
    # day mean a parser is flagging live imports right now, and the recovery
    # window is bounded by the harness's retention. Push once per nightly while
    # it lasts; goes quiet on its own the day after the ledger does.
    if result["drift_alert"] and notify_url:
        result["drift_notify_error"] = _notify(notify_url, result["drift_alert"])
    return result


def _drift_alert() -> Optional[str]:
    """One alert line when either capture ledger took records in the last 24h,
    else None. Fail-soft: an unreadable ledger is the coverage check's problem,
    never this escalation's."""
    try:
        from .._importers._skip_ledger import summarize_skips
        from .._importers._validation_ledger import summarize_drift

        drift = summarize_drift(days=1.0)
        skips = summarize_skips(days=1.0)
        parts = []
        if drift["recent"]:
            parts.append(
                f"{drift['recent']} validation-drift record(s) "
                f"({drift['recent_findings']} finding(s))"
            )
        if skips["recent_substantive"]:
            parts.append(
                f"{skips['recent_substantive']} substantive capture-skip record(s)"
            )
        if not parts:
            return None
        return (
            "format drift active: " + " and ".join(parts) + " in the last 24h "
            "— a parser no longer fully understands a source's format; "
            "see `thread_archive coverage` and the ledgers in ~/.thread/archive/"
        )
    except Exception:  # noqa: BLE001
        return None


