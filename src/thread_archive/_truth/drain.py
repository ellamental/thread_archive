"""The truth append path: per-thread handles, staged writes, the drain, and its
crash framing.

Rows are staged on the session and flushed — fsynced — to their thread files
*before* the SQLite COMMIT they belong to (the JSONL ⊇ SQLite invariant); each
batch is framed by a drain-intent undo journal so a power loss mid-batch rolls
back to the pre-batch baselines. The commit listeners are registered on
``ArchiveSession`` (the class every archive session is constructed as), so
foreign SQLAlchemy sessions in a host process never enter the drain.
Storage-model overview: :mod:`.jsonl_log`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Callable, TextIO

from sqlalchemy import event
from sqlalchemy.orm import Session

from .._store import ArchiveSession, Event, get_engine
from .layout import (
    KG_EVENTS_FILE,
    _fsync_dir,
    _json_default,
    _now_iso,
    _row_dict,
    _shard_depth,
    _thread_file,
    log_dir,
    require_current_format,
)
from .locks import _truth_write_lock

logger = logging.getLogger(__name__)

# Cap on simultaneously-open per-thread append handles (LRU-evicted). A bulk import
# usually touches one thread at a time, so the working set is tiny; the cap only
# bounds a pathological fan-out.
MAX_OPEN_HANDLES = 256
_handles: "OrderedDict[str, TextIO]" = OrderedDict()


# ── append handles (LRU, one per open thread file) ───────────────────────────
def _repair_torn_tail(path: Path) -> None:
    """Newline-terminate a file whose last append was torn (a crash mid-write).

    Without this, the next append glues its record onto the torn fragment and the
    combined line is unparseable — the torn fragment *consumes a valid event*, and
    reindex skips both. Adding the newline first isolates the fragment as its own
    (already unrecoverable) line so every later record stays intact. Same guard
    :func:`_merge_file_into` applies at its boundary."""
    try:
        if path.stat().st_size == 0:
            return
    except FileNotFoundError:
        return
    with open(path, "rb+") as fh:
        fh.seek(-1, os.SEEK_END)
        if fh.read(1) != b"\n":
            fh.write(b"\n")
            fh.flush()
            os.fsync(fh.fileno())
            logger.warning("truth: repaired torn tail (newline-terminated) %s", path)


def _same_inode(fh: TextIO, path: Path) -> bool:
    """True when the cached handle still writes the file at ``path``. A truth
    re-emit (``rebuild_truth_from_store``) atomically replaces thread files, so a
    handle cached across it points at the unlinked old inode — appends through it
    vanish while their SQLite commits survive, the one direction the invariant
    forbids. Two stats per append; the price of never writing to a dead file."""
    try:
        a = os.fstat(fh.fileno())
        b = os.stat(path)
    except (OSError, ValueError):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _handle(path: Path) -> TextIO:
    key = str(path)
    fh = _handles.get(key)
    if fh is not None and not fh.closed and _same_inode(fh, path):
        _handles.move_to_end(key)
        return fh
    if fh is not None and not fh.closed:
        try:
            fh.close()  # stale inode — the file was replaced under us
        except (OSError, ValueError):  # pragma: no cover
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_torn_tail(path)  # never append onto a torn fragment
    is_new = not path.exists()
    fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — long-lived, closed in reset()
    if is_new:
        _fsync_dir(path.parent)  # the new file's dirent must be as durable as its rows
    _handles[key] = fh
    _handles.move_to_end(key)
    while len(_handles) > MAX_OPEN_HANDLES:
        _, old = _handles.popitem(last=False)
        try:
            # Nothing buffered can be lost here — every append flushes its line
            # (see append_line) and durability comes from _fsync_handle, which
            # reopens an evicted path by fd. Still loud: a failing close() in the
            # truth layer is a disk telling us something.
            old.close()
        except (OSError, ValueError):  # pragma: no cover
            logger.warning("truth: error closing evicted append handle", exc_info=True)
    return fh


def append_line(path: Path, rec: dict) -> None:
    fh = _handle(path)
    fh.write(json.dumps(rec, default=_json_default, ensure_ascii=False))
    fh.write("\n")
    fh.flush()


def _fsync_handle(path: Path, *, fsync: Callable[[int], None] = os.fsync) -> None:
    """fsync a cached append handle so its flushed lines are durable on disk.

    Callers batch: write every line of a logical unit via :func:`append_line`
    (flush-only), then fsync each touched file once — one fsync per file per
    commit, not per line, keeps bulk imports fast while closing the power-loss
    window between the OS page cache and the platter.

    A batch touching more than ``MAX_OPEN_HANDLES`` files LRU-evicts its early
    handles before this runs; eviction close() flushes to the OS but does not
    fsync, so an evicted file is reopened here and fsynced by fd — the durability
    bar must not quietly drop for bulk batches. An OSError propagates (fail fast,
    same as an fsync failure on a live handle).

    ``fsync`` is the call this makes, injectable because it is the one durability
    step with no effect a caller can observe afterwards: a test that only proves
    this returns without raising passes just as well against a body that syncs
    nothing. Pass a recorder to assert the fd actually reached the syscall."""
    fh = _handles.get(str(path))
    if fh is not None and not fh.closed:
        fsync(fh.fileno())
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        fsync(fd)
    finally:
        os.close(fd)


def reset_handles() -> None:
    """Close cached append handles (tests / shutdown)."""
    for fh in _handles.values():
        try:
            fh.close()
        except (OSError, ValueError):  # pragma: no cover — already closed / broken handle
            pass
    _handles.clear()


# ── drain intent (crash framing for append batches) ──────────────────────────
# The all-or-nothing drain rollback handles *process* failure, but a power loss
# mid-batch leaves a partial batch in the truth with nothing to distinguish it
# from committed records. The intent file frames each batch as an undo journal:
# the drain durably records ``{txn, files: [{path, baseline, ids}]}`` BEFORE its
# first data write and empties the file after its last data fsync — both inside
# the truth-write mutex, so a non-empty intent is only ever observable after a
# crash. Recovery runs on every acquisition of the mutex (and at reindex start):
# it rolls the framed files back to their baselines, guarded two ways —
#   * the committed-ids check: any of the batch's freshly-inserted ids present in
#     the live index means the COMMIT landed (it was atomic), so a crash *after*
#     COMMIT can never truncate committed truth (index ⊃ truth is the forbidden
#     direction);
#   * the tail check: everything past a file's baseline must be the framed
#     batch's own records (a torn final fragment — the crash itself — is
#     allowed). Anything else means another writer appended after the crash, and
#     the file is left in place rather than risk cutting foreign records.
DRAIN_INTENT_FILE = ".drain.intent"

# Intent record kinds whose ids are fresh inserts in an index table — the basis
# of the committed-ids check. ``thread`` is deliberately absent: a metadata
# re-stage carries a thread id that pre-exists in the index whether or not the
# batch committed, so it can't witness the commit.
_INTENT_TABLES = {"event": "events", "kg_event": "kg_events"}


def _rollback_file(path: Path, baseline: int | None) -> None:
    """Roll one truth file back to its pre-batch baseline: drop the cached append
    handle (with any buffered partial write), then truncate to ``baseline`` — or
    unlink when the batch created the file (``baseline is None``), so no ghost
    thread file survives. The undo is fsynced (file or dirent), so it is at least
    as durable as the appends it removes. The one rollback primitive shared by
    the in-process drain rollback, crashed-drain recovery, and the failed-COMMIT
    compensation — their guards differ, the undo must not. Callers hold the
    truth-write lock; OSErrors propagate for each caller's own policy."""
    fh = _handles.pop(str(path), None)
    if fh is not None and not fh.closed:
        fh.close()
    if baseline is None:
        path.unlink(missing_ok=True)
        _fsync_dir(path.parent)
    else:
        with open(path, "rb+") as rb:
            rb.truncate(baseline)
            os.fsync(rb.fileno())


def _intent_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / DRAIN_INTENT_FILE


def _write_intent(files: list[dict]) -> None:
    """Durably frame the batch about to be appended: fsynced BEFORE the first
    data write, so a crash mid-batch always leaves the frame recovery needs to
    identify — and remove — the partial batch."""
    p = _intent_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(
            {"txn": uuid.uuid4().hex, "at": _now_iso(), "files": files},
            fh, default=_json_default,
        )
        fh.flush()
        os.fsync(fh.fileno())
    if not existed:
        _fsync_dir(p.parent)  # the frame's dirent must be as durable as the frame


def _clear_intent() -> None:
    """Empty the intent after the batch's last data fsync, still under the write
    mutex — an intent must never be visible outside its batch's lock hold. Not
    fsynced: if a crash resurrects a cleared-but-complete intent, the
    committed-ids check in recovery resolves it correctly."""
    try:
        with open(_intent_path(), "w", encoding="utf-8"):
            pass
    except OSError:  # pragma: no cover — best-effort; recovery re-resolves it
        logger.exception("truth: could not clear drain intent")


def _intent_committed(files: list[dict]) -> bool | None:
    """Whether the framed batch's COMMIT landed. Any of its freshly-inserted ids
    present in the live index means yes (the COMMIT was atomic — all or none).
    ``None`` = cannot tell (no insert ids in the batch, or no readable index),
    which callers treat as committed: keeping possibly-uncommitted records is the
    safe direction (dedup collapse bounds the damage), truncating possibly-
    committed ones is the forbidden one."""
    by_table: dict[str, list[int]] = {}
    for f in files:
        for kind, rid in f.get("ids") or []:
            table = _INTENT_TABLES.get(kind)
            if table is not None and rid is not None:
                by_table.setdefault(table, []).append(int(rid))
    if not by_table:
        return None
    db = get_engine().url.database
    if not db or db == ":memory:":
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        for table, ids in by_table.items():
            probe = ids[:32]  # one committed id proves the whole batch
            try:
                row = conn.execute(
                    f"SELECT 1 FROM {table} WHERE id IN ({','.join('?' * len(probe))}) LIMIT 1",  # noqa: S608 — table from _INTENT_TABLES
                    probe,
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is not None:
                return True
        return False
    finally:
        conn.close()


def _tail_is_intent_only(path: Path, baseline: int, allowed: set) -> bool:
    """True when everything past ``baseline`` is the framed batch's own records.
    An unparseable FINAL fragment — the torn write of the crash itself — is
    allowed; any other content means a writer appended after the crash and a
    truncate would destroy records that aren't the batch's."""
    try:
        with open(path, "rb") as fh:
            fh.seek(baseline)
            chunks = fh.read().split(b"\n")
    except OSError:
        return False
    for i, raw in enumerate(chunks):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            return i == len(chunks) - 1  # torn final fragment only
        if (rec.get("type", "event"), rec.get("id")) not in allowed:
            return False
    return True


def _recover_crashed_drain() -> None:
    """Resolve a leftover drain intent — the mark of a process that died (power
    loss, SIGKILL) mid-batch. Runs under the truth-write mutex on every
    acquisition: a live drain's intent never outlives its lock hold, so anything
    found here is a dead process's. Uncommitted framed files are rolled back to
    their baselines (the crash-durable twin of the drain's in-process rollback);
    a committed or undecidable batch keeps its records. Never raises — recovery
    is a repair pass, and a bug in it must not take writes down."""
    p = _intent_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return
    if not raw.strip():
        return
    try:
        intent = json.loads(raw)
        files = list(intent["files"])
    except (ValueError, KeyError, TypeError):
        # A torn intent write: the crash hit before any data append (the intent
        # is durable before the first write), so the truth is untouched.
        logger.warning("truth: discarding torn drain intent (no data was written)")
        _clear_intent()
        return
    try:
        committed = _intent_committed(files)
        if committed is not False:
            logger.warning(
                "truth: drain intent txn=%s survived a crash but its commit %s — keeping its records",
                intent.get("txn"), "landed" if committed else "cannot be determined",
            )
            _clear_intent()
            return
        d = log_dir()
        rolled_back = kept = 0
        for f in files:
            path = d / f["path"]
            baseline = f.get("baseline")
            allowed = {(k, i) for k, i in f.get("ids") or []}
            try:
                size = path.stat().st_size
            except OSError:
                continue  # never created (crash before its first append) or already gone
            if baseline is not None and size <= baseline:
                continue  # nothing of the batch landed here
            if not _tail_is_intent_only(path, baseline or 0, allowed):
                kept += 1
                logger.error(
                    "truth: crashed drain tail of %s holds records outside its intent — left in place",
                    path,
                )
                continue
            _rollback_file(path, baseline)
            rolled_back += 1
        logger.warning(
            "truth: recovered crashed drain txn=%s — %d file(s) rolled back to baseline%s",
            intent.get("txn"), rolled_back,
            f", {kept} left for inspection" if kept else "",
        )
    except Exception:  # pragma: no cover — recovery must never block writes
        logger.exception("truth: crashed-drain recovery failed; intent cleared")
    _clear_intent()



# ── staged writes (durable per-thread append BEFORE the SQLite commit) ───────
# "JSONL is truth" requires the invariant **JSONL ⊇ SQLite**: anything the
# projection has must already be in the truth, never the reverse. A write doesn't
# touch the file directly — it stages (kind, thread_id, row) on the session, and a
# ``before_commit`` listener (registered on ``ArchiveSession``, the class every
# archive session is constructed as, so foreign SQLAlchemy sessions in a host
# process never enter it) flushes each staged record to its thread file
# *before* the COMMIT. A raise there aborts the commit (fail fast — never commit a
# projection we couldn't make durable as truth). If the COMMIT then fails after the
# flush, the truth holds a row SQLite lacks — the safe direction: reindex rebuilds
# it, and dedup_key collapses any re-import. ``after_rollback`` discards the buffer.
_PENDING = "_jsonl_pending"


def _stage(session: Session, kind: str, thread_id: str, row: dict) -> None:
    session.info.setdefault(_PENDING, []).append((kind, str(thread_id), row))


def append_event_row(session: Session, event_row: object) -> None:
    """Stage an event for its thread's truth file. Written only if the session commits."""
    _stage(session, "event", event_row.thread_id, _row_dict(event_row))  # type: ignore[attr-defined]


def record_thread(session: Session, thread: object) -> None:
    """Stage a thread's metadata record for its truth file (written on commit).

    The metadata-write seam: called at thread creation so each thread file opens
    with its ``{"type": "thread", ...}`` record. A later metadata change can re-stage
    (latest wins on reindex); :func:`checkpoint` is the backstop for updates."""
    _stage(session, "thread", thread.id, _row_dict(thread))  # type: ignore[attr-defined]


def unstage_thread(session: Session, thread_id: str) -> None:
    """Discard any staged truth rows for ``thread_id`` (its metadata record and any
    events) from the session's pending buffer — the complement of the staging seam
    for the discard-an-empty-thread path. A thread row deleted before its commit
    must ALSO drop its staged record: otherwise the drain still writes a
    ``threads/<id>.jsonl`` the projection no longer holds, ``verify`` drifts, and
    the next reindex resurrects the thread as an empty ghost. Cross-thread rows
    (kind ``kg_event``) are untouched."""
    pending = session.info.get(_PENDING)
    if not pending:
        return
    tid = str(thread_id)
    session.info[_PENDING] = [
        (kind, t, row) for kind, t, row in pending
        if not (t == tid and kind in ("thread", "event"))
    ]


def append_kg_event(session: Session, kg_event: object) -> None:
    """Stage a topic-graph event for the append-only ``kg_events.jsonl`` truth log.

    The knowledge-layer write seam (mirrors :func:`append_event_row` for the
    conversation log): a topic-graph write flushes the ``KgEvent`` (so ``id`` /
    ``recorded_at`` are populated), stages it here, and the row is appended to the
    single ``kg_events.jsonl`` file before the COMMIT it belongs to — keeping the
    JSONL ⊇ SQLite invariant for the topic graph. ``thread_id`` is irrelevant for the
    cross-thread log (it routes to one file, not a per-thread file), so pass ``"0"``."""
    _stage(session, "kg_event", "0", _row_dict(kg_event))


def write_events(session: Session, events: list[Event]) -> list[Event]:
    """Insert events into SQLite *and* stage them for their threads' truth files.

    The single seam so the projection and the truth never diverge: each row is
    flushed to its thread file before the COMMIT it belongs to. ``flush`` populates
    the autoincrement id and the server-default ``recorded_at`` before the row dict
    is snapshotted, so the truth line is faithful. The caller owns the commit.

    Base64 image/document content is extracted into the content-addressed blob
    store here (see :mod:`.blobs`), before the row reaches SQLite, FTS, or the
    truth file — this seam carries exactly the imported batches (amendment
    rewrites use their own path and must not re-extract), so it is the one
    place that keeps new truth free of inline binary. The blob file is durable
    before the payload referencing it is staged."""
    from .blobs import extract_blobs

    for ev in events:
        if isinstance(ev.payload, dict):
            new_payload, n = extract_blobs(ev.payload)
            if n:
                ev.payload = new_payload
    session.add_all(events)
    session.flush()
    for ev in events:
        append_event_row(session, ev)
    return events


@event.listens_for(ArchiveSession, "before_commit")
def _drain_before_commit(session: Session) -> None:
    # Flush staged truth rows to their thread files *before* the COMMIT, so a row can
    # never land in SQLite without already being in the truth (a raise here aborts
    # the commit). pop() so a second commit on the same session doesn't re-append.
    # Each touched file is fsynced once after its lines are written: the COMMIT that
    # follows is itself fsynced by SQLite, so without this the projection could
    # survive a power loss that the truth doesn't — the one direction the invariant
    # forbids.
    #
    # The drain is all-or-nothing per commit: each file's pre-drain size is recorded
    # and a failure partway (write error, unserializable row, failed fsync) truncates
    # every touched file back before re-raising. Without the rollback, records 1…N−1
    # of a failed batch would stay in the truth while SQLite rolls back — a later
    # reindex would resurrect the partial batch, and a retried import would re-append
    # the same content under fresh ids (the dedup check reads the rolled-back SQLite),
    # doubling events. The same guarantee against a *power loss* mid-drain comes from
    # the intent frame (see the drain-intent section): the batch's files, baselines
    # and ids are durable before its first data write, so recovery can roll a partial
    # batch back to the same baselines the in-process rollback uses.
    pending = session.info.pop(_PENDING, None)
    if not pending:
        return
    d = log_dir()
    require_current_format(d)
    with _truth_write_lock():
        # Depth read under the lock, so a rebalance that bumped it between our
        # staging and this drain can't leave us appending at a stale layout.
        depth = _shard_depth(d)
        staged: list[tuple[Path, dict]] = []
        for kind, thread_id, row in pending:
            path = d / KG_EVENTS_FILE if kind == "kg_event" else _thread_file(d, thread_id, depth)
            staged.append((path, {"type": kind, **row}))
        # Baselines (and open handles) for every touched file BEFORE any append,
        # so the intent frame describes the whole batch up front.
        baselines: dict[Path, int | None] = {}  # pre-drain size; None = file didn't exist
        ids_by_path: dict[Path, list] = {}
        for path, rec in staged:
            if path not in baselines:
                existed = path.exists()
                fh = _handle(path)  # may create the file, and may newline-repair a torn tail
                fh.flush()
                # Post-repair size, so a rollback keeps the repair in place.
                baselines[path] = path.stat().st_size if existed else None
            ids_by_path.setdefault(path, []).append([rec["type"], rec.get("id")])
        _write_intent([
            {"path": str(path.relative_to(d)), "baseline": size, "ids": ids_by_path[path]}
            for path, size in baselines.items()
        ])  # durable BEFORE the first data write
        try:
            for path, rec in staged:
                append_line(path, rec)
            for path in baselines:
                _fsync_handle(path)
        except BaseException:
            # Truncate-to-baseline is safe under the write lock: no other writer
            # can have appended to these files since the baseline was taken.
            # Each undo is fsynced: the intent clear below is not, so an
            # un-durable truncate surviving a power loss alongside a durably
            # cleared intent would resurrect the partial batch with no frame
            # left to roll it back.
            for path, size in baselines.items():
                try:
                    _rollback_file(path, size)
                except OSError:  # pragma: no cover — rollback is best-effort
                    logger.exception("truth: could not roll back partial append to %s", path)
            _clear_intent()  # the batch is undone; the frame must not outlive it
            raise
        _clear_intent()  # inside the lock: an intent is never visible outside its batch
        # Remember what this drain appended (per file: baseline, post-drain size,
        # and the batch's (kind, id) pairs) so a COMMIT failure *after* the drain
        # can compensate (see _undo_drain). The ids let the compensation probe the
        # live index for the commit, exactly as crashed-drain recovery does.
        # Cleared on successful commit.
        session.info[_DRAINED] = {
            path: (size, path.stat().st_size, ids_by_path[path])
            for path, size in baselines.items()
        }


# A successful drain's footprint, kept on the session until its COMMIT lands:
# {path: (baseline_size_or_None, post_drain_size, [(kind, id), …])}. The
# compensation seam for
# the one atomicity gap the drain leaves open — truth is written and fsynced
# before the SQLite COMMIT, so a COMMIT that then fails (disk full, a
# constraint enforced at the final flush, busy timeout) leaves the batch in the
# truth with no projection row. That is the *safe* direction (JSONL ⊇ SQLite),
# but it isn't atomic: a later reindex materializes the uncommitted batch.
_DRAINED = "_jsonl_drained"


@event.listens_for(ArchiveSession, "after_commit")
def _forget_drain_on_commit(session: Session) -> None:
    session.info.pop(_DRAINED, None)


@event.listens_for(ArchiveSession, "after_rollback")
def _discard_on_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


@event.listens_for(ArchiveSession, "after_transaction_end")
def _compensate_failed_commit(session: Session, transaction) -> None:
    # The root transaction ending with the drain footprint still present means
    # the COMMIT never landed (a successful one pops it in after_commit, which
    # fires first). after_transaction_end — not after_rollback — is the hook
    # that actually fires on every such path: a commit that raises mid-flight
    # ends via the session context manager's close(), which never emits
    # after_rollback.
    if transaction.parent is None and _DRAINED in session.info:
        _undo_drain(session)


def _undo_drain(session: Session) -> None:
    """Best-effort compensation when a COMMIT failed after its drain succeeded:
    truncate each touched truth file back to its pre-drain baseline, so the
    truth doesn't keep a transaction SQLite rejected (which reindex would
    otherwise resurrect, and which — for metadata records — carries no fresh
    event id for recovery to reconcile against).

    A raised COMMIT is not proof the transaction didn't land: an I/O error can
    surface after the WAL frames are already durable, leaving the commit
    effective while the driver reports failure. Truncating then would delete
    truth the index holds — index ⊃ truth, the forbidden direction — so the
    undo first probes the batch's freshly-inserted ids against the live index,
    exactly as :func:`_recover_crashed_drain` does. Only a provably-uncommitted
    batch is rolled back; a committed or undecidable one keeps its records (the
    safe direction — dedup collapses a re-import, and checkpoint re-emits
    metadata).

    Only provably-safe undos run: under the exclusive truth-write lock, a file
    is rolled back only if its current size still equals the drain's post-drain
    size — any other writer's append since (sizes differ) leaves the file
    alone, degrading to the documented JSONL ⊇ SQLite behavior. Failure here is
    logged, never raised: the transaction is already over and the leftover
    truth rows are the benign direction (dedup collapses a re-import)."""
    drained: dict[Path, tuple[int | None, int, list]] | None = session.info.pop(_DRAINED, None)
    if not drained:
        return
    committed = _intent_committed([{"ids": ids} for _, _, ids in drained.values()])
    if committed is not False:
        logger.warning(
            "truth: COMMIT failed after its drain but %s — keeping the drained records",
            "the transaction landed in the index"
            if committed
            else "the outcome cannot be determined",
        )
        return
    try:
        with _truth_write_lock():
            for path, (baseline, post_size, _ids) in drained.items():
                try:
                    if not path.exists() or path.stat().st_size != post_size:
                        continue  # someone else appended (or repaired) — leave it
                    _rollback_file(path, baseline)
                except OSError:  # pragma: no cover — compensation is best-effort
                    logger.exception(
                        "truth: could not undo drained batch in %s after rollback", path
                    )
    except OSError:  # pragma: no cover — lock unavailable; leave the safe direction
        logger.exception("truth: post-rollback drain compensation skipped")
