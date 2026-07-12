"""Truth repair: quarantine unparseable lines, restore committed rows the truth lacks.

The sanctioned path from a red ``archive verify`` back to green. An unparseable
line in a truth file is one of two things: the torn final append of a crash
(never committed — the line's fsync never completed, so the COMMIT that follows
it in the write seam never ran), or a damaged formerly-good line. Either way the
line contributes nothing to a reindex (the loaders skip it), but it keeps
``verify`` failing on every run — and an integrity signal that can't be cleared
trains its operator to ignore it, which is worse than the damage.

:func:`repair_truth` makes the state actionable:

* **Quarantine, never delete.** Every unparseable line is appended — bytes
  preserved — to the ledger ``truth/quarantine/fragments.jsonl`` (itself valid
  JSONL: each record wraps the raw fragment as a string with its file, line
  number, and timestamp), fsynced durable *before* the source file is atomically
  rewritten without it. The ledger lives inside the truth directory, so backups
  mirror it; no byte is ever discarded.
* **Containment restore.** After the excision, every committed row the truth
  lacks is re-emitted from the live index: an ``events`` row with no truth line
  (by id, per thread), a ``kg_events`` row with no log line, and a thread with
  no ``type:thread`` metadata record. This is the one operation where writing
  truth *from* the projection is exactly right — the index provably holds what
  the truth lost, and the alternative is a reindex regression gate that
  (correctly) refuses to publish. A fragment that was never committed has no
  index row and restores nothing. Restored event payloads are self-validated
  against the content hash in their own ``dedup_key``: a failing payload is
  still restored (the index copy is the only copy left) but counted and logged
  (``restored_hash_mismatches``), and the next ``verify --hashes`` reports it.

Runs under the exclusive reindex lock — every writer holds it shared around its
truth append + commit, so the tree is quiescent — and resolves any crashed
drain's intent first, so a torn tail the drain rollback would remove isn't
quarantined as damage. Idempotent: a clean truth repairs to zero actions.

After a repair that excised fragments, the next ``archive backup`` may need
``--allow-shrink``: the rewrite shrinks the repaired file, and the mirror's
shrink guard (correctly) flags shrinking truth files.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .._store import Event, KgEvent, Thread, get_session
from .jsonl_log import (
    KG_EVENTS_FILE,
    THREADS_SUBDIR,
    _fsync_dir,
    _hash_key_check,
    _hold_reindex_lock,
    _json_default,
    _row_dict,
    _shard_depth,
    _thread_file,
    _truth_write_lock,
    log_dir,
    reset_handles,
)

logger = logging.getLogger(__name__)

QUARANTINE_SUBDIR = "quarantine"
FRAGMENTS_FILE = "fragments.jsonl"


def _damaged_lines(path: Path) -> list[tuple[int, bytes]]:
    """``(lineno, raw)`` of every unparseable non-empty line. Decodes with
    ``errors="replace"`` before parsing — the exact tolerance every truth reader
    (scan, reindex loader) applies — so repair never excises a line the readers
    accept."""
    bad: list[tuple[int, bytes]] = []
    with open(path, "rb") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                bad.append((lineno, raw.rstrip(b"\n")))
    return bad


def _quarantine(d: Path, records: list[dict]) -> None:
    """Append fragment records to the ledger, fsynced durable — the bytes must
    survive a crash before the rewrite discards them from the source file."""
    qdir = d / QUARANTINE_SUBDIR
    qdir.mkdir(parents=True, exist_ok=True)
    qpath = qdir / FRAGMENTS_FILE
    existed = qpath.exists()
    with open(qpath, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, default=_json_default))
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if not existed:
        _fsync_dir(qdir)


def _rewrite_without(path: Path, bad_linenos: set[int]) -> None:
    """Atomically rewrite ``path`` minus the damaged lines, byte-exact for every
    surviving line (an unterminated final good line gains its newline)."""
    tmp = path.with_name(path.name + ".repair")
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        for lineno, raw in enumerate(src, 1):
            if lineno in bad_linenos:
                continue
            dst.write(raw if raw.endswith(b"\n") else raw + b"\n")
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _append_records(path: Path, records: list[dict]) -> None:
    """Append restored records to a truth file, fsynced (dir too when created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, default=_json_default, ensure_ascii=False))
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if is_new:
        _fsync_dir(path.parent)


def repair_truth(*, dry_run: bool = False) -> dict:
    """Quarantine unparseable truth lines and restore committed rows from the index.

    ``dry_run=True`` reports what would happen without touching anything.
    Returns counts: files damaged, fragments quarantined, events / kg-events /
    thread records restored from the index.
    """
    d = log_dir()
    with _hold_reindex_lock():
        # Resolve any crashed drain first — its rollback removes a partial batch
        # (torn tail included) that this scan would otherwise quarantine as damage.
        with _truth_write_lock():
            pass
        return _repair_locked(d, dry_run=dry_run)


def _repair_locked(d: Path, *, dry_run: bool) -> dict:
    threads_dir = d / THREADS_SUBDIR
    targets: list[Path] = sorted(threads_dir.rglob("*.jsonl")) if threads_dir.exists() else []
    if (d / KG_EVENTS_FILE).exists():
        targets.append(d / KG_EVENTS_FILE)

    damaged: dict[Path, list[tuple[int, bytes]]] = {}
    for path in targets:
        bad = _damaged_lines(path)
        if bad:
            damaged[path] = bad

    result: dict = {
        "dry_run": dry_run,
        "files_damaged": len(damaged),
        "fragments_quarantined": sum(len(v) for v in damaged.values()),
        "events_restored_from_index": 0,
        "kg_events_restored": 0,
        "thread_records_restored": 0,
    }
    if damaged:
        result["quarantine_file"] = str(d / QUARANTINE_SUBDIR / FRAGMENTS_FILE)
        result["damaged_sample"] = [
            f"{path.relative_to(d)}:{lineno}"
            for path, bad in list(damaged.items())[:10]
            for lineno, _ in bad[:2]
        ][:10]

    if not dry_run and damaged:
        now = datetime.now(timezone.utc).isoformat()
        _quarantine(d, [
            {
                "file": str(path.relative_to(d)),
                "lineno": lineno,
                "raw": raw.decode("utf-8", "backslashreplace"),
                "quarantined_at": now,
            }
            for path, bad in damaged.items()
            for lineno, raw in bad
        ])
        reset_handles()  # cached appenders must not span the rewrites
        for path, bad in damaged.items():
            _rewrite_without(path, {lineno for lineno, _ in bad})
            logger.warning("repair: quarantined %d unparseable line(s) from %s", len(bad), path)

    # Containment pass over the whole archive (not just the files touched above):
    # a repair killed between its rewrite and this restore, or any other source of
    # index ⊃ truth drift, is healed by the next run — repair is the inverse of
    # verify, so it must converge on verify-green regardless of how the drift arose.
    truth_event_ids: dict[int, set[int]] = {}
    tids_with_meta: set[int] = set()
    for path in (sorted(threads_dir.rglob("*.jsonl")) if threads_dir.exists() else []):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # freshly-quarantined already; dry-run tolerates
                kind = rec.get("type", "event")
                if kind == "thread":
                    if rec.get("id") is not None:
                        tids_with_meta.add(int(rec["id"]))
                elif kind == "event":
                    ev_id, tid = rec.get("id"), rec.get("thread_id")
                    if ev_id is not None and tid is not None:
                        truth_event_ids.setdefault(int(tid), set()).add(int(ev_id))
    kg_line_ids: set[int] = set()
    if (d / KG_EVENTS_FILE).exists():
        with open(d / KG_EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("id") is not None:
                    kg_line_ids.add(int(rec["id"]))

    depth = _shard_depth(d)
    empty: set[int] = set()
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        missing_by_tid: dict[int, list[int]] = {}
        for ev_id, tid in conn.execute("SELECT id, thread_id FROM events"):
            if int(ev_id) not in truth_event_ids.get(int(tid), empty):
                missing_by_tid.setdefault(int(tid), []).append(int(ev_id))
        missing_kg = sorted(
            int(r[0]) for r in conn.execute("SELECT id FROM kg_events")
            if int(r[0]) not in kg_line_ids
        )
        # A thread the index holds whose truth carries no metadata record — the
        # damaged-thread-line case (reindex would synthesize a stub otherwise).
        # Only threads that have (or are about to get) a truth file qualify.
        meta_missing = {
            int(r[0]) for r in conn.execute("SELECT id FROM threads")
            if int(r[0]) not in tids_with_meta
            and (int(r[0]) in truth_event_ids or int(r[0]) in missing_by_tid)
        }

        result["events_restored_from_index"] = sum(len(v) for v in missing_by_tid.values())
        result["kg_events_restored"] = len(missing_kg)
        result["thread_records_restored"] = len(meta_missing)
        if dry_run:
            return result

        # Restored payloads are self-validated against the content hash in their
        # own dedup_key. A failing row is still restored — the index copy is the
        # only copy left, and quarantining data is not this tool's job — but the
        # count and sample make the suspect content *seen*: the next
        # ``verify --hashes`` will go red on it as a new truth-side mismatch.
        hash_mismatched = 0
        mismatch_sample: list[int] = []

        def _validated(ev) -> dict:
            nonlocal hash_mismatched
            rec = _row_dict(ev)
            key = rec.get("dedup_key")
            if key and _hash_key_check(rec.get("payload"), key) is False:
                hash_mismatched += 1
                if len(mismatch_sample) < 10:
                    mismatch_sample.append(int(rec["id"]))
            return rec

        for tid in sorted(set(missing_by_tid) | meta_missing):
            records: list[dict] = []
            if tid in meta_missing:
                t = s.get(Thread, tid)
                if t is not None:
                    records.append({"type": "thread", **_row_dict(t)})
            ids = missing_by_tid.get(tid, [])
            for start in range(0, len(ids), 500):
                rows = s.execute(
                    select(Event).where(Event.id.in_(ids[start:start + 500])).order_by(Event.id)
                ).scalars()
                records.extend({"type": "event", **_validated(ev)} for ev in rows)
            if records:
                _append_records(_thread_file(d, tid, depth), records)
                logger.warning(
                    "repair: restored %d record(s) to thread %d from the index", len(records), tid
                )
        if missing_kg:
            records = []
            for start in range(0, len(missing_kg), 500):
                kg_rows = s.execute(
                    select(KgEvent)
                    .where(KgEvent.id.in_(missing_kg[start:start + 500]))
                    .order_by(KgEvent.id)
                ).scalars()
                records.extend({"type": "kg_event", **_row_dict(ev)} for ev in kg_rows)
            _append_records(d / KG_EVENTS_FILE, records)
            logger.warning("repair: restored %d kg event(s) from the index", len(records))

        result["restored_hash_mismatches"] = hash_mismatched
        if hash_mismatched:
            result["restored_hash_mismatch_sample"] = mismatch_sample
            logger.warning(
                "repair: %d restored payload(s) fail their own dedup-key content hash "
                "(sample: %s) — restored anyway (the index copy is the only copy); "
                "the next `verify --hashes` will report them",
                hash_mismatched, mismatch_sample,
            )

    if result["files_damaged"] or result["events_restored_from_index"] or result["kg_events_restored"]:
        logger.warning("repair: %s", result)
    return result
