"""Tests for the seventh-pass integrity hardening — fail-closed publication.

1. Reindex refuses to publish a rebuild that would lose committed records the
   current index holds (the old index stays live); ``salvage=True`` publishes
   the lossy rebuild; crash artifacts — torn lines that never committed —
   never block.
2. Dedup collapse can discard a cited event id; reindex repoints the citation
   to the surviving same-content twin instead of leaving it dangling.
3. Reindex realigns a citation whose recorded ``thread_id`` disagrees with the
   cited event's actual thread (a stale/unvalidated snapshot seed).
4. ``rebuild_truth_from_store`` gates on per-unit content containment, so a
   missing event can't hide behind an index-only one keeping counts equal.
5. Backup copies publish atomically and trim an unterminated final fragment,
   so every mirror file is a clean, parseable prefix of its source.
6. ``archive watch --once`` holds the shared ingest lock like every other
   truth writer.
7. Knowledge writes validate their references: citations need a real topic, a
   real event, and the event's actual thread; links need existing endpoints.
"""

from __future__ import annotations

import fcntl
import json
import os

import pytest
from sqlalchemy import text

import thread_archive as ta
from thread_archive.knowledge.write import add_topic_evidence, create_topic, link_threads
from thread_archive.store import get_session
from thread_archive.truth import jsonl_log, rebuild_truth_from_store, scan_truth_counts

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello integrity seven"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back lucky seven"}]}}


def _import_session(tmp_path, name="sess"):
    f = tmp_path / f"{name}.jsonl"
    user = {**USER, "uuid": f"u-{name}",
            "message": {"role": "user", "content": f"hello integrity seven {name}"}}
    asst = {**ASSISTANT, "uuid": f"a-{name}"}
    f.write_text("\n".join(json.dumps(ln) for ln in (user, asst)) + "\n", encoding="utf-8")
    ta.import_path(f)


def _one_thread_file(archive_home):
    return next((archive_home / "truth" / "threads").rglob("*.jsonl"))


def _event_count() -> int:
    with get_session() as s:
        return s.execute(text("SELECT count(*) FROM events")).scalar()


# ── 1. fail-closed publication ────────────────────────────────────────────────
def test_reindex_refuses_interior_corruption_and_salvage_overrides(archive_home, tmp_path):
    _import_session(tmp_path)
    before = _event_count()

    tf = _one_thread_file(archive_home)
    lines = tf.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3  # thread record + 2 events
    lines[1] = '{"type": "event", "id": corrupted beyond'
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    with pytest.raises(RuntimeError, match="would lose"):
        ta.reindex()
    # The old index was left in place — nothing lost.
    assert _event_count() == before

    counts = ta.reindex(salvage=True)
    assert counts["parse_errors_interior"] == 1
    assert counts["parse_errors_torn_tail"] == 0
    assert _event_count() == before - 1  # the corrupted line's event is the loss


def test_reindex_tolerates_torn_final_line(archive_home, tmp_path):
    _import_session(tmp_path)
    before = _event_count()

    tf = _one_thread_file(archive_home)
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "id": 99')  # torn mid-append, no newline
    jsonl_log.reset_handles()

    counts = ta.reindex()
    assert counts["parse_errors_torn_tail"] == 1
    assert counts["parse_errors_interior"] == 0
    assert _event_count() == before


# ── 2. citation survives dedup collapse ───────────────────────────────────────
def test_reindex_repoints_citation_to_surviving_twin(archive_home, tmp_path):
    _import_session(tmp_path)
    with get_session() as s:
        ev_id, tid, key = s.execute(text(
            "SELECT id, thread_id, dedup_key FROM events "
            "WHERE dedup_key IS NOT NULL ORDER BY id LIMIT 1")).one()
    topic = create_topic("Repoint Test")["topic_id"]
    add_topic_evidence(topic, ev_id, tid, "the cited quote")

    # A same-content twin under a fresh id: the residue of a lost-commit
    # re-import. Appended after the original, it wins the OR REPLACE collapse
    # and the cited id is discarded.
    d = archive_home / "truth"
    path = next(p for p in (d / "threads").rglob(f"{tid}.jsonl"))
    twin_id = ev_id + 1000
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("type") == "event" and rec.get("id") == ev_id:
            rec["id"] = twin_id
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            break
    jsonl_log.reset_handles()

    counts = ta.reindex()
    assert counts.get("citations_repointed") == 1
    with get_session() as s:
        cited = s.execute(text(
            "SELECT event_id FROM topic_messages WHERE topic_id = :t"), {"t": topic}).scalar()
        survivor = s.execute(text(
            "SELECT id FROM events WHERE thread_id = :tid AND dedup_key = :k"),
            {"tid": tid, "k": key}).scalar()
    assert cited == survivor == twin_id
    assert ta.verify(deep=True)["deep"]["dangling"]["citation_events"] == 0


# ── 3. citation thread realignment ────────────────────────────────────────────
def test_reindex_aligns_citation_thread_with_event(archive_home, tmp_path):
    _import_session(tmp_path)
    with get_session() as s:
        ev_id, tid = s.execute(text("SELECT id, thread_id FROM events LIMIT 1")).one()
    topic = create_topic("Alignment Test")["topic_id"]
    add_topic_evidence(topic, ev_id, tid, "quote")
    ta.checkpoint()  # snapshot topic_messages.jsonl

    # A stale seed with a wrong thread_id and no kg log to correct it on
    # replay — the shape of a pre-log archive whose only citation record is the
    # snapshot seed. The kg rows leave both the truth and the index (a raw
    # delete stages nothing), so the committed-regression gate sees no loss.
    d = archive_home / "truth"
    snap = d / "topic_messages.jsonl"
    rows = [json.loads(ln) for ln in snap.read_text(encoding="utf-8").splitlines()]
    rows[0]["thread_id"] = tid + 12345
    snap.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (d / jsonl_log.KG_EVENTS_FILE).unlink()
    with get_session() as s:
        s.execute(text("DELETE FROM kg_events"))
        s.commit()
    jsonl_log.reset_handles()

    counts = ta.reindex()
    assert counts.get("citations_thread_aligned") == 1
    with get_session() as s:
        assert s.execute(text(
            "SELECT thread_id FROM topic_messages WHERE topic_id = :t"),
            {"t": topic}).scalar() == tid


# ── 4. re-emit containment gate ───────────────────────────────────────────────
def test_rebuild_truth_gate_catches_compensated_loss(archive_home, tmp_path):
    _import_session(tmp_path)
    with get_session() as s:
        ev_id, tid = s.execute(text("SELECT id, thread_id FROM events LIMIT 1")).one()
        # Drop one real event and add an index-only fake: counts stay equal, so
        # the old count-parity gate would wave the re-emit through and the real
        # event's truth line would be destroyed.
        s.execute(text("DELETE FROM events WHERE id = :i"), {"i": ev_id})
        s.execute(text(
            "INSERT INTO events (thread_id, stream_id, event_type, payload, occurred_at, dedup_key) "
            "VALUES (:t, 'fake', 'user_message_sent', '{\"content\": \"fake\"}', "
            "'2026-01-01T10:00:00+00:00', 'fake:key:0000000000000000')"), {"t": tid})
        s.commit()

    with pytest.raises(RuntimeError, match="lacks"):
        rebuild_truth_from_store()
    rebuild_truth_from_store(force=True)  # the deliberate override still works


# ── 5. atomic, prefix-clean backup copies ─────────────────────────────────────
def test_backup_trims_torn_tail_and_leaves_no_tmp(archive_home, tmp_path):
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    good = tf.read_bytes()
    with open(tf, "ab") as fh:
        fh.write(b'{"type": "event", "id": 99')  # a live append caught mid-line

    dest = tmp_path / "bak"
    ta.backup(str(dest), verify_first=False)

    copy = dest / tf.relative_to(archive_home / "truth")
    assert copy.read_bytes() == good  # clean prefix: the torn fragment is trimmed
    assert scan_truth_counts(truth_dir=dest)["parse_errors"] == 0
    assert not list(dest.rglob("*.tmp-*"))  # atomic publish leaves no residue


# ── 6. one-shot watch holds the ingest lock ───────────────────────────────────
def test_watch_once_holds_shared_ingest_lock(archive_home, monkeypatch, capsys):
    from thread_archive import cli
    from thread_archive.truth.jsonl_log import _reindex_lock_path
    from thread_archive.watcher import Watcher
    from thread_archive.watcher.base import WatchResult

    seen = {}

    def fake_poll(self):
        # A shared holder must block an exclusive probe (distinct fd = distinct
        # flock owner, even in-process).
        fd = os.open(_reindex_lock_path(), os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["locked"] = False
            except OSError:
                seen["locked"] = True
        finally:
            os.close(fd)
        return WatchResult()

    monkeypatch.setattr(Watcher, "poll_once", fake_poll)
    assert cli.main(["watch", "--once"]) == 0
    assert seen["locked"] is True


# ── 7. knowledge writes validate their references ─────────────────────────────
def test_kg_writes_reject_bad_references(archive_home, tmp_path):
    _import_session(tmp_path)
    with get_session() as s:
        ev_id, tid = s.execute(text("SELECT id, thread_id FROM events LIMIT 1")).one()
    topic = create_topic("Validation Test")["topic_id"]

    add_topic_evidence(topic, ev_id, tid, "valid cite")  # the happy path still works

    with pytest.raises(ValueError, match="no event"):
        add_topic_evidence(topic, 99_999_999, tid, "q")
    with pytest.raises(ValueError, match="belongs to thread"):
        add_topic_evidence(topic, ev_id, tid + 1, "q")
    with pytest.raises(ValueError, match="no topic"):
        add_topic_evidence(99_999_999, ev_id, tid, "q")
    with pytest.raises(ValueError, match="link endpoints"):
        link_threads(topic, 99_999_999)
