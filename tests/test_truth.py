"""JSONL is truth, SQLite is a lossless rebuildable projection.

- write a thread + its events through the truth seam (one file per thread)
- the per-thread JSONL file exists and holds the metadata record + event lines
- delete index.db, run reindex, and the same thread + event set comes back
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from thread_archive._store import Event, Thread, _base, get_engine, get_session, init_db
from thread_archive._truth import jsonl_log

# archive_home fixture lives in tests/conftest.py


def _now() -> datetime:
    return datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _delete_index(home) -> None:
    for suffix in ("", "-wal", "-shm"):
        (home / f"index.db{suffix}").unlink(missing_ok=True)


def _thread_file(home, tid):
    """The thread's truth file under truth/threads/ (depth-agnostic lookup)."""
    matches = list((home / "truth" / "threads").rglob(f"{tid}.jsonl"))
    return matches[0] if matches else None


def test_jsonl_truth_and_lossless_reindex(archive_home) -> None:
    init_db()

    # Author a thread and record it to truth (the metadata-write seam) so its file
    # opens with the {"type":"thread"} record.
    with get_session() as s:
        t = Thread(name="sess-1", title="hi", source="claude-code", source_id="abc")
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        s.commit()
        tid = t.id

    # Append events through the truth seam — they hit the thread's file on commit.
    with get_session() as s:
        jsonl_log.write_events(
            s,
            [
                Event(
                    thread_id=tid,
                    stream_id="x",
                    event_type="user_message_sent",
                    payload={"text": "hi"},
                    occurred_at=_now(),
                    dedup_key="k1",
                ),
                Event(
                    thread_id=tid,
                    stream_id="x",
                    event_type="text_delta",
                    payload={"text": "yo"},
                    occurred_at=_now(),
                    dedup_key="k2",
                ),
            ],
        )
        s.commit()

    # One file per thread: threads/<id>.jsonl = the thread record + its 2 events.
    tf = _thread_file(archive_home, tid)
    assert tf is not None, "expected a per-thread truth file"
    recs = [json.loads(ln) for ln in tf.read_text().splitlines() if ln.strip()]
    kinds = [r["type"] for r in recs]
    assert kinds.count("thread") >= 1
    assert kinds.count("event") == 2

    # Capture the event set as the live store sees it.
    with get_session() as s:
        before = [
            (e.id, e.thread_id, e.event_type, e.payload["text"], e.dedup_key)
            for e in s.execute(select(Event).order_by(Event.id)).scalars()
        ]
    assert [b[3] for b in before] == ["hi", "yo"]

    # Nuke the index entirely — JSONL is the only surviving copy.
    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    _delete_index(archive_home)
    assert not (archive_home / "index.db").exists()

    # Rebuild from truth.
    counts = jsonl_log.reindex()
    assert counts["threads"] == 1
    assert counts["events"] == 2

    with get_session() as s:
        after = [
            (e.id, e.thread_id, e.event_type, e.payload["text"], e.dedup_key)
            for e in s.execute(select(Event).order_by(Event.id)).scalars()
        ]
        thread = s.execute(select(Thread).where(Thread.name == "sess-1")).scalar_one()

    assert after == before, "reindex from JSONL must reproduce the exact event set"
    assert thread.title == "hi"
    assert thread.source == "claude-code"
    assert thread.id == tid


def test_rollback_does_not_write_truth(archive_home) -> None:
    """A staged event whose transaction rolls back must not reach the truth."""
    init_db()
    with get_session() as s:
        t = Thread(name="sess-1")
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        s.commit()
        tid = t.id

    with get_session() as s:
        jsonl_log.write_events(
            s,
            [
                Event(
                    thread_id=tid,
                    stream_id="x",
                    event_type="user_message_sent",
                    payload={"text": "discard me"},
                    occurred_at=_now(),
                ),
            ],
        )
        s.rollback()

    # The thread's file holds its metadata record but never the rolled-back event.
    tf = _thread_file(archive_home, tid)
    assert tf is not None
    assert "discard me" not in tf.read_text()


def test_id_highwater_survives_delete(archive_home) -> None:
    """AUTOINCREMENT keeps the id high-water across a DELETE, so a writer that inserts
    during reindex's truncated window can never recycle a historical id. DELETE-then-
    insert is the minimal reproduction of that window — the regression for the
    id-collision cascade (a live watcher minting ids while reindex had emptied events).
    """
    init_db()
    with get_session() as s:
        t = Thread(name="hw")
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        s.commit()
        tid = t.id

    # A historical event at a high id sets the sequence high-water.
    with get_session() as s:
        s.add(
            Event(
                id=5000, thread_id=tid, stream_id="x", event_type="user_message_sent",
                payload={"text": "a"}, occurred_at=_now(),
            )
        )
        s.commit()

    # reindex's truncate: clear the table (without recreating it).
    with get_session() as s:
        s.execute(delete(Event))
        s.commit()

    # A fresh auto-id insert must continue *past* the high-water, never reuse 5000.
    with get_session() as s:
        e = Event(
            thread_id=tid, stream_id="y", event_type="text_delta",
            payload={"text": "b"}, occurred_at=_now(),
        )
        s.add(e)
        s.flush()
        new_id = e.id
        s.commit()

    assert new_id > 5000, f"id {new_id} recycled after DELETE — id high-water not preserved"


def test_reindex_collapses_duplicate_pk_last_wins(archive_home) -> None:
    """A duplicate primary key in the truth (legacy id-collision pollution) must
    collapse last-wins on reindex via INSERT OR REPLACE — not abort the rebuild."""
    init_db()
    with get_session() as s:
        t = Thread(name="dup")
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        s.commit()
        tid = t.id

    tf = _thread_file(archive_home, tid)
    assert tf is not None
    jsonl_log.reset_handles()  # close the seam's append handle before we append by hand

    # Two event lines sharing one id, newest last — what the collision bug produced.
    with open(tf, "a", encoding="utf-8") as fh:
        for text in ("first", "second"):
            fh.write(
                json.dumps({
                    "type": "event", "id": 777, "thread_id": tid, "stream_id": "x",
                    "event_type": "user_message_sent", "payload": {"text": text},
                    "occurred_at": _now().isoformat(),
                })
                + "\n"
            )

    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    _delete_index(archive_home)

    jsonl_log.reindex()  # must not raise on the duplicate id

    with get_session() as s:
        e = s.get(Event, 777)
    assert e is not None, "duplicate-id event vanished"
    assert e.payload["text"] == "second", "duplicate PK must collapse to the newest record"

def _author_thread_with_events(name: str, texts: list[str]) -> int:
    """Author a thread + events through the truth seam; returns the thread id."""
    with get_session() as s:
        t = Thread(name=name)
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        s.commit()
        tid = t.id
    with get_session() as s:
        jsonl_log.write_events(
            s,
            [
                Event(
                    thread_id=tid, stream_id="x", event_type="user_message_sent",
                    payload={"text": text}, occurred_at=_now(), dedup_key=f"{name}-{i}",
                )
                for i, text in enumerate(texts)
            ],
        )
        s.commit()
    return tid


def test_reindex_tolerates_torn_truth_line(archive_home) -> None:
    """A torn/truncated line in the truth (a crash mid-append) must not kill reindex —
    the recovery primitive recovers everything parseable and reports the skip count,
    the same tolerance `thread-archive index verify` already has."""
    init_db()
    tid = _author_thread_with_events("torn", ["hi", "yo"])

    tf = _thread_file(archive_home, tid)
    assert tf is not None
    jsonl_log.reset_handles()  # close the seam's append handle before we append by hand

    # A torn JSON line (truncated mid-record) and a garbage line, as a crash leaves them.
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "id": 999, "thread_id": ' + str(tid) + ', "payl\n')
        fh.write("not json at all\n")

    assert jsonl_log.scan_truth_counts()["parse_errors"] == 2

    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    _delete_index(archive_home)

    counts = jsonl_log.reindex()  # must not raise on the torn lines
    assert counts["parse_errors"] == 2
    assert counts["events"] == 2

    with get_session() as s:
        texts = [
            e.payload["text"]
            for e in s.execute(select(Event).order_by(Event.id)).scalars()
        ]
    assert texts == ["hi", "yo"], "the parseable events must all be recovered"


def test_failed_reindex_leaves_old_index_intact(archive_home) -> None:
    """Build-and-swap atomicity: a reindex that dies at any point must leave the old
    index answering exactly as before, with no build leftovers on disk."""
    init_db()
    _author_thread_with_events("atomic", ["keep me"])

    # A real mid-build death: the truth file the loader is about to read cannot
    # be opened, so the build raises partway through instead of finishing.
    truth_file = next((archive_home / "truth" / "threads").rglob("*.jsonl"))
    jsonl_log.reset_handles()
    truth_file.chmod(0o000)
    try:
        with pytest.raises(OSError):
            jsonl_log.reindex()
    finally:
        truth_file.chmod(0o600)

    # The old index still answers, untouched.
    with get_session() as s:
        texts = [e.payload["text"] for e in s.execute(select(Event)).scalars()]
    assert texts == ["keep me"]

    # No .rebuild leftovers (main, -wal, or -shm).
    leftovers = list(archive_home.glob("index.db.rebuild*"))
    assert leftovers == [], f"build leftovers survived a failed reindex: {leftovers}"


def test_ingest_lock_shared_vs_exclusive(archive_home) -> None:
    """The watcher's shared ingest lock must yield False (skip the pass) while a
    reindex holds the lock exclusive, and True once it's released. flock treats
    separate fds as independent holders, so both sides are testable in-process."""
    with jsonl_log._hold_reindex_lock():
        with jsonl_log.try_shared_ingest_lock() as acquired:
            assert acquired is False, "ingest must skip while reindex holds the lock"
    with jsonl_log.try_shared_ingest_lock() as acquired:
        assert acquired is True, "ingest must resume once the reindex lock is released"


# ── rebalance crash-safety ────────────────────────────────────────────────────
# The shard rebalance must be unable to lose truth: manifest-first (the new depth
# is durable before any file moves), merge-never-clobber (a twin recombines), and
# a straggler sweep (an interrupted migration is finished by the next checkpoint).


def _seed_flat_threads(d, n_threads: int, events_per: int = 3) -> None:
    """Hand-write ``n_threads`` flat truth files: a thread record + event lines."""
    threads_dir = d / jsonl_log.THREADS_SUBDIR
    threads_dir.mkdir(parents=True, exist_ok=True)
    for tid in range(1, n_threads + 1):
        with open(threads_dir / f"{tid}.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "thread", "id": tid, "name": f"t{tid}"}) + "\n")
            for i in range(events_per):
                fh.write(json.dumps({
                    "type": "event", "id": tid * 100 + i, "thread_id": tid,
                    "payload": {"n": i},
                }) + "\n")
    jsonl_log._write_manifest(d, {
        "version": jsonl_log.TRUTH_FORMAT_VERSION,
        "shard_depth": 0,
        "last_checkpoint_at": None,
    })


def _event_ids(path) -> set[int]:
    ids: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "event":
            ids.add(rec["id"])
    return ids


def test_rebalance_crash_then_twin_merges_without_loss(archive_home, monkeypatch) -> None:
    """A sweep killed mid-move leaves the manifest already at the new depth; a flat
    twin created by a racing writer is MERGED home by the next sweep — the failure
    that used to clobber a thread's whole history with its tail."""
    monkeypatch.setenv("THREAD_ARCHIVE_SHARDFLAT_MAX", "4")
    d = jsonl_log.log_dir()
    _seed_flat_threads(d, 6)
    threads_dir = d / jsonl_log.THREADS_SUBDIR

    # Stop the sweep partway on a real fault: the shard bucket one of the moves
    # lands in refuses writes, so os.replace fails there exactly the way a kill
    # would leave the sweep — some files moved, the rest still flat. The bucket
    # picked is the first that no earlier move also targets, so every move
    # before it succeeds and the stopping point is exact.
    plan = [(p, jsonl_log._thread_file(d, p.stem, 1))
            for p in threads_dir.rglob("*.jsonl")]
    earlier: list = []
    for stop, (_, dest) in enumerate(plan):
        if stop and dest.parent not in earlier:
            break
        earlier.append(dest.parent)
    else:  # pragma: no cover — six threads never all share one bucket
        raise AssertionError("every move targets the same bucket")
    blocked = plan[stop][1].parent
    blocked.mkdir(parents=True)
    blocked.chmod(0o500)
    try:
        with pytest.raises(OSError):
            jsonl_log._maybe_rebalance(d, 0)
    finally:
        blocked.chmod(0o700)

    # Manifest-first: the depth was durable BEFORE the moves, so post-crash writers
    # compute sharded paths and cannot start new flat files.
    assert jsonl_log._shard_depth(d) == 1
    sharded = list(threads_dir.rglob("*/*.jsonl"))
    assert len(sharded) == stop, "exactly the pre-fault moves happened"
    assert 0 < stop < len(plan), "the sweep stopped partway, not before or after"

    # A commit that raced the manifest bump left a flat twin of a MOVED thread.
    victim = int(sharded[0].stem)
    flat_twin = threads_dir / f"{victim}.jsonl"
    with open(flat_twin, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "event", "id": 9999, "thread_id": victim,
                             "payload": {"tail": True}}) + "\n")

    # Next checkpoint's sweep: finishes the migration and merges the twin.
    depth = jsonl_log._maybe_rebalance(d, jsonl_log._shard_depth(d))
    assert depth == 1
    assert list(threads_dir.glob("*.jsonl")) == [], "no flat stragglers remain"

    home = jsonl_log._thread_file(d, victim, 1)
    got = _event_ids(home)
    assert got == {victim * 100, victim * 100 + 1, victim * 100 + 2, 9999}, (
        f"history + tail must both survive the merge, got {got}"
    )
    # The thread record survived too (the old clobber destroyed it).
    recs = [json.loads(ln) for ln in home.read_text(encoding="utf-8").splitlines()]
    assert any(r.get("type") == "thread" for r in recs)


def test_rebalance_straggler_sweep_at_steady_depth(archive_home) -> None:
    """Once sharded, a misplaced flat file is re-homed even though no threshold is
    being crossed (target == depth)."""
    d = jsonl_log.log_dir()
    threads_dir = d / jsonl_log.THREADS_SUBDIR
    threads_dir.mkdir(parents=True, exist_ok=True)
    jsonl_log._write_manifest(d, {
        "version": jsonl_log.TRUTH_FORMAT_VERSION,
        "shard_depth": 1,
        "last_checkpoint_at": None,
    })

    home = jsonl_log._thread_file(d, 7, 1)
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text(json.dumps({"type": "event", "id": 701, "thread_id": 7}) + "\n")
    stray = threads_dir / "7.jsonl"
    stray.write_text(json.dumps({"type": "event", "id": 702, "thread_id": 7}) + "\n")

    assert jsonl_log._maybe_rebalance(d, 1) == 1
    assert not stray.exists()
    assert _event_ids(home) == {701, 702}


def test_rebalance_lock_loser_skips_and_checkpoint_keeps_depth(archive_home, monkeypatch) -> None:
    """While another process holds the rebalance lock, the sweep skips without
    moving anything — and checkpoint re-reads the manifest so it never writes a
    stale (lower) depth back over the winner's."""
    import fcntl as _fcntl
    import os as _os

    monkeypatch.setenv("THREAD_ARCHIVE_SHARDFLAT_MAX", "4")
    init_db()
    d = jsonl_log.log_dir()
    _seed_flat_threads(d, 6)
    # The "winner" already migrated the manifest to depth 1.
    jsonl_log._write_manifest(d, {
        "version": jsonl_log.TRUTH_FORMAT_VERSION,
        "shard_depth": 1,
        "last_checkpoint_at": None,
    })

    fd = _os.open(jsonl_log._rebalance_lock_path(), _os.O_RDWR | _os.O_CREAT, 0o644)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        # Loser passes its stale depth (0); it must skip and move nothing.
        assert jsonl_log._maybe_rebalance(d, 0) == 0
        assert list((d / jsonl_log.THREADS_SUBDIR).rglob("*/*.jsonl")) == []
        jsonl_log.checkpoint(snapshots=False)
    finally:
        _os.close(fd)
    assert jsonl_log._shard_depth(d) == 1, "checkpoint must not regress the manifest depth"


def test_merge_file_into_guards_torn_tail(tmp_path) -> None:
    """A torn last line in dest must not glue onto src's first record — only the
    fragment itself stays unparseable."""
    dest = tmp_path / "d.jsonl"
    src = tmp_path / "s.jsonl"
    dest.write_bytes(b'{"type":"event","id":1}\n{"type":"event","i')  # torn tail
    src.write_text(json.dumps({"type": "event", "id": 2}) + "\n", encoding="utf-8")

    jsonl_log._merge_file_into(src, dest)

    assert not src.exists()
    parsed = []
    for ln in dest.read_text(encoding="utf-8").splitlines():
        try:
            parsed.append(json.loads(ln))
        except ValueError:
            pass
    assert {p["id"] for p in parsed} == {1, 2}


def test_rebuild_truth_removes_stale_other_depth_twin(archive_home) -> None:
    """rebuild_truth_from_store re-emits every thread at the computed depth and
    removes a stale copy at another depth, so no duplicate lines survive to shadow
    repaired rows on a later reindex. Files for ids the store lacks are kept."""
    init_db()
    with get_session() as s:
        t = Thread(name="sess-r", title="r", source="claude-code", source_id="r1")
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        jsonl_log.write_events(s, [Event(
            thread_id=t.id, stream_id="x", event_type="user_message_sent",
            payload={"text": "hello"}, occurred_at=_now(), dedup_key="r-k1",
        )])
        s.commit()
        tid = t.id

    d = jsonl_log.log_dir()
    # A stale twin at depth 1 (as if left behind by an old layout change)...
    twin = jsonl_log._thread_file(d, tid, 1)
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_text(json.dumps({"type": "event", "id": 424242, "thread_id": tid}) + "\n")
    # ...and a file for an id the store does NOT hold, which must be preserved.
    ghost_id = "01GH0STGH0STGH0STGH0STGH0S"  # an id the store does not hold
    ghost = jsonl_log._thread_file(d, ghost_id, 1)
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_text(json.dumps({"type": "thread", "id": ghost_id, "name": "ghost"}) + "\n")

    # The twin's fabricated event id is content the store lacks, so the re-emit's
    # pre-flight (correctly) refuses without force — this test is about the
    # re-emit's file handling, so override deliberately.
    jsonl_log.rebuild_truth_from_store(force=True)

    assert not twin.exists(), "stale twin of a re-emitted thread must be removed"
    assert ghost.exists(), "a file for an id the store lacks must be left untouched"
    home = jsonl_log._thread_file(d, tid, jsonl_log._shard_depth(d))
    assert home.exists()
    assert 424242 not in _event_ids(home)


def test_discard_new_thread_leaves_no_ghost_truth_record(archive_home) -> None:
    """A thread created and discarded in the same transaction must leave NOTHING:
    no row, and no staged truth record for the drain to write — the ghost file
    that ``verify`` counts as drift and the next reindex resurrects as an empty
    thread. Staged rows for OTHER threads must survive the unstage untouched."""
    from thread_archive._importers._state import create_thread, discard_new_thread

    init_db()
    with get_session() as s:
        keep_id = create_thread(s, source="claude-code", source_id="keep")
        drop_id = create_thread(s, source="claude-code", source_id="drop")
        discard_new_thread(s, drop_id)
        s.commit()

    with get_session() as s:
        assert s.get(Thread, keep_id) is not None
        assert s.get(Thread, drop_id) is None
    assert _thread_file(archive_home, keep_id) is not None, "kept thread's record must land"
    assert _thread_file(archive_home, drop_id) is None, "discarded thread must leave no file"
