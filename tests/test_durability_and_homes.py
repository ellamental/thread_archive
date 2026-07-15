"""Regression tests for three real bugs:

1. Home switching within a process must not split the archive (SQLite writing one
   store while JSONL appends resolve to another).
2. "JSONL is truth" must be durable: a failed JSONL write aborts the commit, so an
   event can never reach SQLite without already being in the truth log.
3. The public ``import_path`` must leave a complete truth set (checkpoint threads),
   so a library user can import → delete index.db → reindex without losing metadata.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from thread_archive import _api as ta
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._truth import jsonl_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello durability"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _all_truth_text(home):
    """Concatenated text of every per-thread truth file under a home."""
    return "".join(p.read_text() for p in (home / "truth" / "threads").rglob("*.jsonl"))


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_home_is_private_and_self_heals_to_0700(tmp_path) -> None:
    # Conversation content is personal data: the home and truth dirs must be
    # owner-only, and a loosened mode must heal on the next open.
    import stat

    from thread_archive._config import resolve_paths

    home = tmp_path / "private"
    paths = resolve_paths(str(home)).ensure()
    assert stat.S_IMODE(paths.home.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.truth_dir.stat().st_mode) == 0o700

    paths.home.chmod(0o755)
    resolve_paths(str(home)).ensure()
    assert stat.S_IMODE(paths.home.stat().st_mode) == 0o700


def test_home_switch_does_not_split_the_archive(tmp_path) -> None:
    home_a = tmp_path / "A"
    home_b = tmp_path / "B"
    fa = tmp_path / "a.jsonl"
    fb = tmp_path / "b.jsonl"
    _write_cc(fa, [USER, ASSISTANT])
    _write_cc(fb, [{**USER, "message": {"role": "user", "content": "different B content"}}, ASSISTANT])

    ta.import_path(fa, home=str(home_a))
    ta.import_path(fb, home=str(home_b))  # switch home mid-process

    # Each home is a complete, separate archive — neither bled into the other.
    sa = ta.status(home=str(home_a))
    sb = ta.status(home=str(home_b))
    assert sa["events"] > 0 and sb["events"] > 0
    assert sa["index_path"] != sb["index_path"]

    # The truth dirs are separate too — A's per-thread files never got B's content.
    a_truth = _all_truth_text(home_a)
    b_truth = _all_truth_text(home_b)
    assert "hello durability" in a_truth and "different B content" not in a_truth
    assert "different B content" in b_truth and "hello durability" not in b_truth

    # Each rebuilds losslessly from its own truth.
    assert ta.search("durability", home=str(home_a))
    assert ta.search("different", home=str(home_b))


def test_jsonl_write_failure_aborts_the_commit(archive_home, monkeypatch) -> None:
    """If the truth log can't be written, the projection must not commit — so SQLite
    never holds an event the JSONL lacks."""
    init_db()
    with get_session() as s:
        s.add(Thread(id=1, name="t1"))
        s.commit()

    def _boom(*_a, **_k):
        raise OSError("simulated disk-full while writing truth")

    monkeypatch.setattr(jsonl_log.drain, "_append_line", _boom)

    with get_session() as s:
        jsonl_log.write_events(s, [Event(
            thread_id=1, stream_id="x", event_type="user_message_sent",
            payload={"content": "must not survive"}, occurred_at=_now(), dedup_key="k1",
        )])
        with pytest.raises(OSError):
            s.commit()  # before_commit drain raises → commit aborts

    # The event is in neither store: not committed to SQLite, never written to JSONL.
    with get_session() as s:
        assert s.execute(select(Event)).scalars().all() == []


def test_verify_passes_on_a_clean_archive_and_detects_drift(archive_home) -> None:
    """``verify`` confirms the truth parses + matches the index, and flags drift when
    the index diverges from the truth."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    ta.checkpoint()

    res = ta.verify()
    assert res["ok"] is True
    assert res["truth"]["threads"] == 1 and res["truth"]["parse_errors"] == 0
    assert res["drift"] == {"threads": 0, "events": 0, "kg_events": 0}

    # Delete an event from the index only (truth untouched) → positive-ish drift surfaces.
    with get_session() as s:
        ev = s.execute(select(Event)).scalars().first()
        s.delete(ev)
        s.commit()
    res2 = ta.verify()
    assert res2["ok"] is False
    assert res2["drift"]["events"] != 0


def test_backup_mirrors_truth_and_is_restorable(archive_home, tmp_path) -> None:
    """``backup`` mirrors the truth dir to a destination from which a fresh archive
    reindexes losslessly (truth is the backup; index.db is rebuildable)."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    dest = tmp_path / "backup"
    res = ta.backup(str(dest))
    assert res["files_copied"] > 0
    assert (dest / "threads").exists()

    # Re-run is incremental: nothing changed → nothing recopied.
    assert ta.backup(str(dest))["files_copied"] == 0

    # The backup is a complete restore set: point a fresh archive at it and reindex.
    ta.close()
    restored_home = tmp_path / "restored"
    restored_home.mkdir()
    import shutil
    shutil.copytree(dest, restored_home / "truth")
    counts = ta.reindex(home=str(restored_home))
    assert counts["threads"] == 1 and counts["events"] > 0
    assert ta.search("durability", home=str(restored_home))


def test_import_path_leaves_a_complete_truth_set(archive_home) -> None:
    """Library import → delete index.db → reindex must keep the thread (its metadata
    record is written to the thread's own truth file at creation — no manual call)."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])

    ta.import_path(f)  # no explicit checkpoint() by the caller
    assert list((archive_home / "truth" / "threads").rglob("*.jsonl")), "expected a per-thread truth file"

    ta.close()
    for suffix in ("", "-wal", "-shm"):
        (archive_home / f"index.db{suffix}").unlink(missing_ok=True)

    ta.reindex()
    assert ta.status()["threads"] == 1
    assert ta.search("durability")  # and the rebuilt index is searchable
