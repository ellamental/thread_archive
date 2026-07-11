"""Fail-closed publication: reindex and truth re-emit never destroy committed data.

* Reindex refuses to publish a rebuild that would lose committed records the
  current index holds (the old index stays live); ``salvage=True`` publishes
  the lossy rebuild; crash artifacts — torn lines that never committed —
  never block.
* Dedup collapse can discard a cited event id; reindex repoints the citation
  to the surviving same-content twin instead of leaving it dangling, and
  realigns a citation whose recorded ``thread_id`` disagrees with the cited
  event's actual thread (a stale/unvalidated snapshot seed).
* ``rebuild_truth_from_store`` refuses a store that holds fewer events than
  the truth, and gates on per-unit content containment so a missing event
  can't hide behind an index-only one keeping counts equal (``force=True`` is
  the deliberate override).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

import thread_archive as ta
from thread_archive.knowledge.write import add_topic_evidence, create_topic
from thread_archive.store import get_session
from thread_archive.truth import jsonl_log, rebuild_truth_from_store

from .helpers import event_count, import_cc_session, one_thread_file


# ── fail-closed publication ───────────────────────────────────────────────────
def test_reindex_refuses_interior_corruption_and_salvage_overrides(archive_home, tmp_path):
    import_cc_session(tmp_path)
    before = event_count()

    tf = one_thread_file(archive_home)
    lines = tf.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3  # thread record + 2 events
    lines[1] = '{"type": "event", "id": corrupted beyond'
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    with pytest.raises(RuntimeError, match="would lose"):
        ta.reindex()
    # The old index was left in place — nothing lost.
    assert event_count() == before

    counts = ta.reindex(salvage=True)
    assert counts["parse_errors_interior"] == 1
    assert counts["parse_errors_torn_tail"] == 0
    assert event_count() == before - 1  # the corrupted line's event is the loss


def test_reindex_tolerates_torn_final_line(archive_home, tmp_path):
    import_cc_session(tmp_path)
    before = event_count()

    tf = one_thread_file(archive_home)
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "id": 99')  # torn mid-append, no newline
    jsonl_log.reset_handles()

    counts = ta.reindex()
    assert counts["parse_errors_torn_tail"] == 1
    assert counts["parse_errors_interior"] == 0
    assert event_count() == before


# ── citation survives dedup collapse ──────────────────────────────────────────
def test_reindex_repoints_citation_to_surviving_twin(archive_home, tmp_path):
    import_cc_session(tmp_path)
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


# ── citation thread realignment ───────────────────────────────────────────────
def test_reindex_aligns_citation_thread_with_event(archive_home, tmp_path):
    import_cc_session(tmp_path)
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


# ── truth re-emit gates ───────────────────────────────────────────────────────
def test_rebuild_truth_from_store_refuses_a_partial_store(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    with get_session() as s:
        s.execute(text(
            "DELETE FROM events WHERE id = (SELECT max(id) FROM events)"))
        s.commit()

    with pytest.raises(RuntimeError, match="destroy truth content"):
        jsonl_log.rebuild_truth_from_store()

    # force=True is the deliberate override; afterwards truth matches the store.
    jsonl_log.rebuild_truth_from_store(force=True)
    assert ta.verify()["ok"] is True


def test_rebuild_truth_gate_catches_compensated_loss(archive_home, tmp_path):
    import_cc_session(tmp_path)
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
