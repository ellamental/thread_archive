"""Tests for the fourth-pass integrity hardening:

1. A plain ``reindex`` (no ``--vectors``) restores the vector sidecar into the
   new index and prunes rows for events the rebuild collapsed away — the
   semantic index survives the swap.
2. ``save_vectors_sidecar`` never replaces a populated sidecar with an empty
   table, and ``backup`` refreshes the sidecar so live-embedded vectors ride
   the mirror.
3. ``verify(deep=True)`` flags a dedup_key disagreement between truth and index
   for the same event id (an index-only mutation / one-sided corruption).
4. ``verify(hashes=True)`` re-hashes stored payloads against the content hash
   embedded in their own dedup_key, detecting silent payload corruption on
   either store (report-only).
5. ``rebuild_truth_from_store`` refuses to re-emit truth from a store that
   holds fewer events than the truth (``force=True`` overrides).
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import text

import thread_archive as ta
from thread_archive.store import get_session
from thread_archive.truth import jsonl_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello integrity"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _import_session(tmp_path):
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)


def _event_ids() -> list[int]:
    with get_session() as s:
        return [r[0] for r in s.execute(text("SELECT id FROM events ORDER BY id"))]


def _index_fake_vectors(event_ids: list[int]) -> None:
    from thread_archive.retrieval.vectors import index_vectors

    assert index_vectors(
        (eid, "user", [float(eid % 7 + 1)] * 768) for eid in event_ids
    ) == len(event_ids)


def _vector_count() -> int:
    with get_session() as s:
        return s.execute(text("SELECT count(*) FROM event_vectors")).scalar() or 0


def test_plain_reindex_restores_sidecar_and_prunes_orphans(archive_home, tmp_path) -> None:
    from thread_archive.retrieval.vectors import save_vectors_sidecar

    _import_session(tmp_path)
    ids = _event_ids()
    _index_fake_vectors(ids)
    # A vector for an event the rebuild won't hold must be pruned, not scored.
    _index_fake_vectors([999_999])
    assert save_vectors_sidecar(archive_home / "truth") == len(ids) + 1

    counts = ta.reindex()  # NO vectors flag — restore must happen anyway
    assert counts["vectors_restored"] == len(ids) + 1
    assert counts["vectors_pruned"] == 1
    assert _vector_count() == len(ids)


def test_empty_save_never_clobbers_a_populated_sidecar(archive_home, tmp_path) -> None:
    from thread_archive.retrieval.vectors import save_vectors_sidecar

    _import_session(tmp_path)
    _index_fake_vectors(_event_ids())
    n = save_vectors_sidecar(archive_home / "truth")
    assert n > 0

    with get_session() as s:
        s.execute(text("DELETE FROM event_vectors"))
        s.commit()
    assert save_vectors_sidecar(archive_home / "truth") == 0  # no-op, not an empty save

    side = sqlite3.connect(archive_home / "truth" / "vectors.sqlite")
    try:
        assert side.execute("SELECT count(*) FROM event_vectors").fetchone()[0] == n
    finally:
        side.close()


def test_backup_refreshes_the_vector_sidecar(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    ids = _event_ids()
    _index_fake_vectors(ids)

    res = ta.backup(str(tmp_path / "dest"))
    assert res["vectors_cached"] == len(ids)
    assert (archive_home / "truth" / "vectors.sqlite").exists()
    assert (tmp_path / "dest" / "vectors.sqlite").exists()  # rides the mirror


def test_deep_verify_flags_dedup_key_mismatch(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    assert ta.verify(deep=True)["ok"] is True

    with get_session() as s:
        s.execute(text(
            "UPDATE events SET dedup_key = 'tampered:key:blk=0:deadbeefdeadbeef' "
            "WHERE id = (SELECT min(id) FROM events WHERE dedup_key IS NOT NULL)"
        ))
        s.commit()

    res = ta.verify(deep=True)
    assert res["ok"] is False
    assert res["deep"]["events_key_mismatch"] == 1
    assert len(res["deep"]["key_mismatch_sample"]) == 1


def test_verify_hashes_detects_payload_corruption_both_sides(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    base = ta.verify(hashes=True)["hashes"]
    assert base["truth"]["checked"] > 0 and base["truth"]["mismatched"] == 0
    assert base["index"]["checked"] > 0 and base["index"]["mismatched"] == 0

    # Index-side corruption: rewrite one payload without touching its key.
    with get_session() as s:
        s.execute(text(
            "UPDATE events SET payload = '{\"content\": \"tampered\"}' "
            "WHERE id = (SELECT min(id) FROM events WHERE dedup_key IS NOT NULL)"
        ))
        s.commit()
    # Truth-side corruption: flip a payload in the highest-id event's truth line.
    tf = next((archive_home / "truth" / "threads").rglob("*.jsonl"))
    lines = tf.read_text(encoding="utf-8").splitlines()
    events = [(i, json.loads(ln)) for i, ln in enumerate(lines)
              if '"type": "event"' in ln]
    idx, rec = next((i, r) for i, r in reversed(events) if r.get("dedup_key"))
    rec["payload"] = {"content": "rotted"}
    lines[idx] = json.dumps(rec)
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    res = ta.verify(hashes=True)["hashes"]
    assert res["index"]["mismatched"] == 1
    assert res["truth"]["mismatched"] == 1
    assert res["index"]["mismatch_sample"] != res["truth"]["mismatch_sample"]


def test_rebuild_truth_from_store_refuses_a_partial_store(archive_home, tmp_path) -> None:
    import pytest

    _import_session(tmp_path)
    with get_session() as s:
        s.execute(text(
            "DELETE FROM events WHERE id = (SELECT max(id) FROM events)"))
        s.commit()

    with pytest.raises(RuntimeError, match="destroy truth content"):
        jsonl_log.rebuild_truth_from_store()

    # force=True is the deliberate override; afterwards truth matches the store.
    jsonl_log.rebuild_truth_from_store(force=True)
    assert ta.verify()["ok"] is True
