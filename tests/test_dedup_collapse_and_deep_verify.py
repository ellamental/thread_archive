"""Tests for the DB-level dedup guarantee and the id-level integrity checks:

1. The partial UNIQUE index on ``(thread_id, dedup_key)`` makes reindex collapse
   same-content twins — the residue of a lost-commit re-import (truth holds the
   same content under two ids; only one row may materialize).
2. ``verify`` compares the index against the truth's *effective* (collapsed)
   count, so an archive whose truth legitimately carries superseded lines is
   clean, while genuinely missing content still shows as drift.
3. ``verify(deep=True)`` classifies truth-only ids: a superseded twin is benign,
   a missing event (no twin) fails, and an index-only id (the forbidden
   direction) fails.
4. A shrunk (rewritten) source file rewinds its import cursor instead of
   silently never importing again.
5. The backup deletion bound: a gutted source cannot strip the destination.
"""

from __future__ import annotations

import json

from sqlalchemy import select, text

import thread_archive as ta
from thread_archive.store import Event, get_session
from thread_archive.truth import jsonl_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello dedup"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back"}]}}
LATER_USER = {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
              "cwd": "/proj", "message": {"role": "user", "content": "a later turn"}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _truth_file(archive_home):
    return next((archive_home / "truth" / "threads").rglob("*.jsonl"))


def _lose_committed_turn(session, event_type: str) -> None:
    """Simulate a lost commit for every ``event_type`` event: the event row AND its
    same-transaction FTS rows vanish together (a commit is atomic — a loss that kept
    the search rows would be a different defect, one deep verify flags as orphans)."""
    sub = f"(SELECT id FROM events WHERE event_type = '{event_type}')"
    session.execute(text(f"DELETE FROM event_search WHERE event_id IN {sub}"))
    session.execute(text(f"DELETE FROM events_fts WHERE event_id IN {sub}"))
    session.execute(text(f"DELETE FROM events WHERE event_type = '{event_type}'"))


def _clone_line_with_fresh_id(truth_file, match: str) -> int:
    """Append a copy of the truth line containing ``match`` under a fresh id —
    the exact shape a lost-commit re-import leaves behind (same dedup_key + content,
    different id). Returns the cloned id."""
    lines = truth_file.read_text(encoding="utf-8").splitlines()
    src = next(json.loads(ln) for ln in lines if match in ln and '"type": "event"' in ln)
    max_id = max(json.loads(ln)["id"] for ln in lines if '"type": "event"' in ln)
    clone = dict(src)
    clone["id"] = max_id + 1000
    with open(truth_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(clone) + "\n")
    return clone["id"]


def test_reindex_collapses_same_content_twins(archive_home, tmp_path) -> None:
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    tf = _truth_file(archive_home)
    _clone_line_with_fresh_id(tf, "hello dedup")

    counts = ta.reindex()
    assert counts.get("events_collapsed", 0) == 1
    with get_session() as s:
        dups = s.execute(text(
            "SELECT count(*) FROM (SELECT 1 FROM events WHERE dedup_key IS NOT NULL "
            "GROUP BY thread_id, dedup_key HAVING count(*) > 1)"
        )).scalar()
    assert dups == 0


def test_live_insert_of_duplicate_dedup_key_is_rejected(archive_home, tmp_path) -> None:
    """On an index built with the unique index, a same-key insert can't slip past
    the advisory membership check."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    ta.reindex()  # rebuild so the unique index exists

    with get_session() as s:
        row = s.execute(select(Event).where(Event.dedup_key.is_not(None))).scalars().first()
        s.add(Event(thread_id=row.thread_id, stream_id="dup", event_type=row.event_type,
                    payload=row.payload, occurred_at=row.occurred_at, dedup_key=row.dedup_key))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_verify_treats_superseded_twins_as_clean(archive_home, tmp_path) -> None:
    """The production shape of a lost commit: the index row vanishes (truth keeps
    the line), the source watermark rewinds, and the re-poll re-imports the same
    content under a fresh id. Truth then holds two lines for one turn; verify must
    call that clean (superseded), not drift."""
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    with get_session() as s:
        _lose_committed_turn(s, "user_message_sent")
        s.execute(text("UPDATE import_state SET last_line_count = 0, last_file_size = 0"))
        s.commit()
    ta.import_path(f)  # re-imports only the lost turn, under a fresh id

    res = ta.verify(deep=True)
    assert res["ok"] is True
    assert res["truth"]["duplicate_content_lines"] == 1
    assert res["truth"]["events_effective"] == res["index"]["events"]
    assert res["deep"]["events_superseded_twins"] == 1
    assert res["deep"]["events_missing_from_index"] == 0

    # A reindex collapses the twin pair; the archive stays clean before and after.
    ta.reindex()
    res = ta.verify(deep=True)
    assert res["ok"] is True
    assert res["deep"]["duplicate_content_pairs_index"] == 0


def test_deep_verify_flags_missing_and_index_only_events(archive_home, tmp_path) -> None:
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    # Missing: the index loses a row (truth keeps the line, nothing re-imports it).
    with get_session() as s:
        _lose_committed_turn(s, "user_message_sent")
        s.commit()

    res = ta.verify(deep=True)
    assert res["ok"] is False
    assert res["deep"]["events_missing_from_index"] == 1

    # ... and a reindex recovers it, going clean again.
    ta.reindex()
    res = ta.verify(deep=True)
    assert res["ok"] is True

    # Index-only: delete a truth *event* line the index still holds (the thread
    # metadata record also carries the title text — keep it) → forbidden direction.
    tf = _truth_file(archive_home)
    kept = [ln for ln in tf.read_text(encoding="utf-8").splitlines()
            if not ('"type": "event"' in ln and "hello dedup" in ln)]
    tf.write_text("\n".join(kept) + "\n", encoding="utf-8")
    res = ta.verify(deep=True)
    assert res["ok"] is False
    assert res["deep"]["events_index_only"] >= 1


def test_shrunk_source_file_rewinds_cursor_and_reimports(archive_home, tmp_path) -> None:
    """A rewritten-shorter source must not be silently ignored forever."""
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT, LATER_USER])
    ta.import_path(f)
    with get_session() as s:
        before = s.execute(select(Event)).scalars().all()
        n_before = len(before)

    # Rewrite the file shorter: same first turns, the later turn replaced by a new one.
    replacement = {"type": "user", "uuid": "u3", "timestamp": "2026-01-01T10:02:00Z",
                   "cwd": "/proj", "message": {"role": "user", "content": "rewritten tail"}}
    _write_cc(f, [USER, replacement])

    ta.import_path(f)
    with get_session() as s:
        contents = [
            e.payload.get("content") for e in s.execute(
                select(Event).where(Event.event_type == "user_message_sent")
            ).scalars()
        ]
    # The rewritten content imported; the originals are retained (append-only log).
    assert "rewritten tail" in contents
    assert "hello dedup" in contents and "a later turn" in contents
    with get_session() as s:
        assert len(s.execute(select(Event)).scalars().all()) > n_before


def test_backup_deletion_bound_blocks_gutted_source_mirror(archive_home, tmp_path, monkeypatch) -> None:
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    dest = tmp_path / "bk"
    res = ta.backup(str(dest))
    assert res["mirror_complete"] is True

    # Lower the bound so this small fixture can trip it, then gut the source.
    from thread_archive import api
    monkeypatch.setattr(api, "_MIRROR_DELETE_FLOOR", 0)
    monkeypatch.setattr(api, "_MIRROR_DELETE_MAX_FRACTION", 0.0)
    for p in (archive_home / "truth" / "threads").rglob("*.jsonl"):
        p.unlink()

    res = ta.backup(str(dest))
    assert res["deletions_skipped"] >= 1
    assert res["files_deleted"] == 0
    # The destination still holds the thread files the source lost.
    assert any((dest / "threads").rglob("*.jsonl"))


def test_append_handles_survive_truth_reemit(archive_home, tmp_path) -> None:
    """``rebuild_truth_from_store`` replaces every thread file's inode; a cached
    append handle from before the re-emit must not keep writing to the dead file
    (lines would vanish from truth while their commits survive — the forbidden
    direction, and exactly what deep verify's index_only counts)."""
    from thread_archive.truth.jsonl_log import rebuild_truth_from_store

    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)          # caches an append handle for the thread's file

    rebuild_truth_from_store()  # replaces the file under the cached handle

    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(LATER_USER) + "\n")
    ta.import_path(f)          # must append to the NEW inode, not the dead one

    res = ta.verify(deep=True)
    assert res["deep"]["events_index_only"] == 0
    assert res["ok"] is True
