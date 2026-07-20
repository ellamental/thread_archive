"""Edge/error-branch coverage for the backup kit (``_ops``) and the truth log
(``_truth``): verify, redact, backup, reindex/rebuild, the drain, and repair.

These modules already carry the happy paths well; this file drives the remaining
error and edge branches — missing/empty stores, crash-recovery corners, crypto
lifecycle errors, mirror guards, and the drain's low-level handle plumbing —
asserting on real outcomes (raised errors, file state, returned counts).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import types

import pytest
from sqlalchemy import text

from thread_archive import _api as ta
from thread_archive._ops import backup as bk
from thread_archive._ops import redact as rd
from thread_archive._store import get_session
from thread_archive._truth import drain, jsonl_log, rebuild, scan_truth_counts

from .helpers import (
    append_jsonl,
    cc_assistant,
    cc_user,
    corrupt_event_line,
    event_count,
    import_cc_session,
    one_thread_file,
    write_jsonl,
)

SECRET = "xyzzy-hunter2-4a6772f0-super-secret"


def _import_secret_session(tmp_path, name="sess"):
    f = tmp_path / f"{name}.jsonl"
    write_jsonl(f, [cc_user(name, content=f"my api key is {SECRET}"), cc_assistant(name)])
    ta.import_path(f)
    with get_session() as s:
        u = s.execute(text(
            "SELECT id, thread_id FROM events WHERE event_type = 'user_message_sent'"
        )).first()
        a = s.execute(text(
            "SELECT id FROM events WHERE event_type != 'user_message_sent' "
            "ORDER BY id LIMIT 1"
        )).first()
    return f, int(u[0]), u[1], int(a[0])


# ══════════════════════════════════════════════════════════════════════════════
# verify.py
# ══════════════════════════════════════════════════════════════════════════════
def test_verify_empty_archive_is_green(archive_home) -> None:
    """An archive with no events (watermark 0) takes the no-bound branches: no
    id watermark, no FTS parity check, and still reports ok."""
    ta.open_archive(str(archive_home))
    res = ta.verify()
    assert res["ok"] is True
    assert res["index"]["events"] == 0
    assert res["drift"] == {"threads": 0, "events": 0, "kg_events": 0}
    # No watermark → the FTS parity gate is skipped (not a false "missing").
    assert res["fts"]["missing"] is False
    # deep + hashes on an empty store must also stay green (empty-side branches).
    deep = ta.verify(deep=True, hashes=True)
    assert deep["ok"] is True
    assert deep["deep"]["events_index_only"] == 0
    assert deep["hashes"]["truth"]["checked"] == 0


def test_verify_failure_ledger_write_error_is_soft(archive_home, tmp_path) -> None:
    """A failing verify whose evidence ledger can't be written degrades soft: no
    ``failure_log`` key, but ``ok`` still reports the failure."""
    import_cc_session(tmp_path)
    # Occupy the ledger path with a directory so open(..., "a") raises OSError.
    (archive_home / "verify-failures.jsonl").mkdir()

    corrupt_event_line(one_thread_file(archive_home))
    v = ta.verify()
    assert v["ok"] is False
    assert "parse_errors" in v["failed_components"]
    assert "failure_log" not in v  # the write was swallowed, not raised


def test_verify_hashes_tolerates_unparseable_index_payload(archive_home, tmp_path) -> None:
    """A stored index payload that isn't valid JSON is treated as damaged content,
    not a crash: the hashes pass parses defensively and counts it mismatched."""
    import_cc_session(tmp_path)
    with get_session() as s:
        eid = s.execute(text(
            "SELECT min(id) FROM events WHERE dedup_key IS NOT NULL")).scalar()
        # Raw non-JSON text in the payload column.
        s.execute(text("UPDATE events SET payload = 'not-valid-json{' WHERE id = :e"),
                  {"e": eid})
        s.commit()

    v = ta.verify(hashes=True)
    assert v["hashes"]["index"]["mismatched"] >= 1
    assert eid in v["hashes"]["index"]["mismatch_sample"]


# ══════════════════════════════════════════════════════════════════════════════
# redact.py — error paths + edge branches
# ══════════════════════════════════════════════════════════════════════════════
def test_redact_missing_thread_raises(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        ta.redact(987654, [1])


def test_redact_event_not_in_thread_raises(archive_home, tmp_path) -> None:
    _, eid, tid, _ = _import_secret_session(tmp_path)
    with pytest.raises(ValueError, match="events not in thread"):
        ta.redact(tid, [eid, 999999])


def test_unredact_unknown_key_raises(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    with pytest.raises(ValueError, match="no redaction record"):
        ta.unredact("deadbeefdeadbeef")


def test_show_key_unknown_raises(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    with pytest.raises(ValueError, match="not in the keyring"):
        ta.redact_show_key("nope")


def test_forget_key_unknown_raises(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    with pytest.raises(ValueError, match="not in the keyring"):
        ta.redact_forget_key("nope")


def test_restore_key_unknown_record_raises(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    import base64
    with pytest.raises(ValueError, match="no redaction record"):
        ta.redact_restore_key("nope", base64.b64encode(b"\x00" * 32).decode())


def test_redact_passes_through_blank_and_unparseable_truth_lines(archive_home, tmp_path):
    """The truth rewrite keeps unparseable and blank lines byte-exact — those are
    repair's jurisdiction, not redaction's."""
    _, eid, tid, _ = _import_secret_session(tmp_path)
    tf = one_thread_file(archive_home)
    jsonl_log.reset_handles()
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write("\n")                       # blank line
        fh.write("{not json at all\n")       # unparseable line

    res = ta.redact(tid, [eid])
    assert res["events_redacted"] == 1
    # Both the blank and the garbage line survived the rewrite untouched.
    raw = tf.read_text(encoding="utf-8")
    assert "{not json at all" in raw
    assert SECRET.encode() not in tf.read_bytes()


def test_redact_scrubs_topic_message_snapshot(archive_home, tmp_path):
    """When a topic_messages.jsonl snapshot exists, its quote line is rewritten
    too (the citation-snapshot rewrite path)."""
    _, eid, tid, _ = _import_secret_session(tmp_path)
    pytest.importorskip("thread_librarian")
    from thread_librarian import add_topic_evidence, create_topic

    topic = create_topic("Secrets", "t")["topic_id"]
    add_topic_evidence(topic, eid, tid, f"my api key is {SECRET}")
    ta.checkpoint()  # materialize truth/topic_messages.jsonl

    snap = archive_home / "truth" / rd.TOPIC_MESSAGES_FILE
    assert snap.exists() and SECRET.encode() in snap.read_bytes()

    res = ta.redact(tid, [eid])
    assert res["topic_quotes_scrubbed"] == 1
    # The snapshot's quote is now the placeholder — no plaintext left in it.
    assert SECRET.encode() not in snap.read_bytes()
    assert "[redacted]" in snap.read_text(encoding="utf-8")


def test_redact_partial_keeps_unrelated_title(archive_home, tmp_path):
    """Redacting only the assistant turn leaves the (user-derived) title in place
    and surfaces the keep-it note rather than scrubbing it."""
    _, _uid, tid, aid = _import_secret_session(tmp_path)
    res = ta.redact(tid, [aid])  # assistant event only; title derives from user msg
    assert res["events_redacted"] == 1
    assert res["thread_meta_scrubbed"] == []
    assert any("kept" in n for n in res["notes"])
    with get_session() as s:
        title = s.execute(text("SELECT title FROM threads WHERE id = :t"), {"t": tid}).scalar()
    assert title and "[redacted]" not in title


def test_redact_purges_vector_sidecar(archive_home, tmp_path):
    """Redaction deletes the event's embedding from both the live table and the
    durable ``vectors.sqlite`` sidecar (an embedding of a secret is the secret)."""
    _, eid, tid, _ = _import_secret_session(tmp_path)
    d = archive_home / "truth"
    vec = b"\x00" * (768 * 4)
    with get_session() as s:
        s.execute(text(
            "CREATE TABLE IF NOT EXISTS event_vectors ("
            "event_id INTEGER NOT NULL, content_type TEXT NOT NULL, "
            "chunk INTEGER NOT NULL DEFAULT 0, dim INTEGER NOT NULL, "
            "vec BLOB NOT NULL, PRIMARY KEY (event_id, content_type, chunk))"))
        s.execute(text("INSERT INTO event_vectors (event_id, content_type, chunk, dim, vec) "
                       "VALUES (:e, 'text', 0, 768, :v)"), {"e": eid, "v": vec})
        s.commit()
    # A sidecar carrying the same event's vector.
    side = d / "vectors.sqlite"
    con = sqlite3.connect(side)
    con.execute("CREATE TABLE event_vectors (event_id INTEGER, content_type TEXT, "
                "chunk INTEGER, dim INTEGER, vec BLOB)")
    con.execute("INSERT INTO event_vectors VALUES (?, 'text', 0, 768, ?)", (eid, vec))
    con.commit()
    con.close()

    ta.redact(tid, [eid])

    with get_session() as s:
        live = s.execute(text("SELECT count(*) FROM event_vectors WHERE event_id = :e"),
                         {"e": eid}).scalar()
    con = sqlite3.connect(side)
    sidecar = con.execute("SELECT count(*) FROM event_vectors WHERE event_id = ?", (eid,)).fetchone()[0]
    con.close()
    assert live == 0 and sidecar == 0


def test_redaction_statuses_skips_keyless_and_unmatched(archive_home, tmp_path):
    """A record with no key_id is skipped, and an unredaction whose key has no
    matching redaction row doesn't crash the status roll-up."""
    import_cc_session(tmp_path)
    log = archive_home / "truth" / rd.REDACTIONS_FILE
    log.write_text(
        json.dumps({"type": "redaction", "thread_id": 1, "event_ids": [1]}) + "\n"
        + json.dumps({"type": "unredaction", "key_id": "ghost", "at": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    assert rd.redaction_statuses() == []


def test_marker_key_id_non_dict_envelope():
    assert rd._marker_key_id({"_redacted": "not-a-dict"}) is None
    assert rd._marker_key_id({"content": "hi"}) is None
    assert rd._marker_key_id({"_redacted": {"key_id": "abc"}}) == "abc"


# ══════════════════════════════════════════════════════════════════════════════
# backup.py
# ══════════════════════════════════════════════════════════════════════════════
def test_atomic_copy_trims_source_with_no_newline(tmp_path):
    """An append-only copy whose whole source lacks a newline scans back to the
    start and publishes an empty (clean-prefix) file."""
    src = tmp_path / "s.jsonl"
    src.write_bytes(b'{"type": "event", "id": 1, "partial')  # no newline anywhere
    dst = tmp_path / "d.jsonl"
    bk._atomic_copy(src, dst, trim_to_newline=True)
    assert dst.read_bytes() == b""  # trimmed to the last newline (none → empty)


def test_atomic_copy_cleans_tmp_on_error(tmp_path):
    """A copy that fails mid-flight leaves no temp residue and re-raises."""
    dst = tmp_path / "d.jsonl"
    with pytest.raises(OSError):
        bk._atomic_copy(tmp_path / "missing.jsonl", dst, trim_to_newline=False)
    assert list(tmp_path.glob(".*.tmp-*")) == []
    assert not dst.exists()


def test_split_rehomed_twins_classifies_edge_files(tmp_path):
    """Non-int stems, non-threads files, and a missing canonical dest copy all
    fall through to 'rest' (kept under the deletion cap), never mis-flagged as
    rebalance twins."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "threads").mkdir(parents=True)
    (dest / "threads").mkdir(parents=True)
    # depth 1 so a flat threads/<id>.jsonl is non-canonical.
    jsonl_log._write_manifest(src, {"version": 1, "shard_depth": 1, "last_checkpoint_at": None})

    non_int = dest / "threads" / "notanumber.jsonl"
    non_int.write_text("{}\n")
    non_threads = dest / "topic_messages.jsonl"
    non_threads.write_text("{}\n")
    # A flat twin whose canonical source file exists but whose canonical dest copy
    # is missing → the twin stat raises OSError → stays capped (rest).
    tid = 5
    canonical_rel = jsonl_log._thread_relpath(tid, 1)
    (src / canonical_rel).parent.mkdir(parents=True, exist_ok=True)
    (src / canonical_rel).write_text('{"type":"event","id":501}\n')
    flat_twin = dest / "threads" / f"{tid}.jsonl"
    flat_twin.write_text('{"type":"event","id":501}\n')

    doomed = [non_int, non_threads, flat_twin]
    twins, rest = bk._split_rehomed_twins(src, dest, doomed)
    assert twins == []
    assert set(rest) == set(doomed)


def test_backup_cleans_stale_tmp_generation(archive_home, tmp_path):
    """A half-built generation (a killed snapshot's ``.tmp-*`` dir) is swept before
    the next snapshot builds."""
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    ta.backup(str(dest))  # creates .generations
    stale = dest / bk._GENERATIONS_SUBDIR / ".tmp-leftover"
    stale.mkdir(parents=True)
    (stale / "junk").write_text("x")

    ta.backup(str(dest))
    assert not stale.exists()


def test_backup_generation_falls_back_to_copyfile_without_hardlinks(
    archive_home, tmp_path, monkeypatch
):
    """On a filesystem without hardlinks the generation is still taken, via a
    plain copy."""
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))

    def _no_hardlinks(src, dst):
        raise OSError("cross-device link not permitted")

    monkeypatch.setattr(os, "link", _no_hardlinks)
    res = ta.backup(str(dest))
    assert res["generation_created"]  # snapshot happened despite no os.link
    gen = dest / bk._GENERATIONS_SUBDIR / res["generation_created"]
    assert list(gen.rglob("*.jsonl"))  # real copied files, not links


def test_backup_generation_snapshot_error_never_blocks_mirror(archive_home, tmp_path):
    """A snapshot failure records ``generation_error`` and the mirror still
    completes."""
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    ta.backup(str(dest))  # second run: the generations subtree now exists
    gens = dest / bk._GENERATIONS_SUBDIR
    assert gens.is_dir()

    # A real unwritable generations dir (the degraded-destination case): the
    # snapshot cannot build its tree, but the mirror itself is unaffected.
    gens.chmod(0o500)
    try:
        res = ta.backup(str(dest))
    finally:
        gens.chmod(0o700)
    assert res["generation_error"]
    assert res["generation_created"] is None
    assert res["mirror_complete"] is True
    assert list(gens.iterdir()), "the earlier generation is untouched"


def test_backup_reports_missing_dest_files(archive_home, tmp_path, monkeypatch):
    """The structural completeness check flags source .jsonl files with no
    destination copy."""
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"

    def _no_copy(src, dst, *, delete=False, allow_shrink=False):
        return {
            "files_copied": 0, "bytes_copied": 0, "files_deleted": 0,
            "rehomed_twins_deleted": 0, "deletions_skipped": 0,
            "shrinks_skipped": 0, "shrink_sample": [],
        }

    monkeypatch.setattr(bk, "mirror_dir", _no_copy)
    res = ta.backup(str(dest))
    assert res["dest_missing_files"] > 0
    assert res["mirror_complete"] is False


def test_restore_drill_reports_reindex_failure(archive_home, tmp_path):
    """A rebuild that refuses/fails during the drill is the drill's finding —
    reported, not raised."""
    import json

    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))

    # Damage the mirror the way a rebuild cannot publish through: a second
    # thread file claiming the first thread's (unique) name, so the OR REPLACE
    # load drops the original parent row and orphans its events. The real
    # relational gate refuses to publish that build.
    mirrored = next((dest / "threads").rglob("*.jsonl"))
    meta = next(
        json.loads(ln) for ln in mirrored.read_text(encoding="utf-8").splitlines()
        if json.loads(ln)["type"] == "thread"
    )
    twin = mirrored.with_name("424242.jsonl")
    twin.write_text(json.dumps({**meta, "id": 424242}) + "\n", encoding="utf-8")

    res = ta.restore_drill(str(dest))
    assert res["ok"] is False
    assert "refusing to publish" in res["error"]
    # The live archive is reopened and answers normally afterwards.
    assert event_count() > 0


def test_restore_drill_keep_home_retains_rebuilt_index(archive_home, tmp_path):
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))

    res = ta.restore_drill(str(dest), keep_home=True)
    from pathlib import Path

    assert "drill_home" in res
    assert (Path(res["drill_home"]) / "index.db").exists()
    shutil.rmtree(res["drill_home"], ignore_errors=True)


def test_drill_smoke_skips_when_no_content_expected():
    """With no content to sample, the smoke pass short-circuits green."""
    out = bk._drill_smoke("/nonexistent-home", expect_content=False)
    assert out == {"ok": True, "read_ok": False, "search_ok": False}


def test_restore_drill_smoke_reports_read_error(archive_home, tmp_path):
    """A crash in the rebuilt archive's read path is the drill's finding, captured
    as an error rather than propagated."""
    import_cc_session(tmp_path)

    # A real unreadable restored archive: the FTS sample comes off the live
    # archive, but the home the smoke pass reads from has an index that is not a
    # database at all — exactly what a restore onto damaged bytes would leave.
    broken = tmp_path / "broken-home"
    broken.mkdir()
    (broken / "index.db").write_bytes(b"not a sqlite database at all" * 64)
    try:
        res = bk._drill_smoke(str(broken), expect_content=True)
    finally:
        ta.open_archive(str(archive_home))

    assert res["ok"] is False
    assert res["read_ok"] is False and res["search_ok"] is False
    assert "DatabaseError" in res["error"]
    assert event_count() > 0  # the live archive still answers


# ══════════════════════════════════════════════════════════════════════════════
# drain.py — handle plumbing, intent, unstage
# ══════════════════════════════════════════════════════════════════════════════
def test_repair_torn_tail_noops_on_empty_and_missing(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    drain._repair_torn_tail(empty)  # size 0 → early return
    assert empty.read_bytes() == b""
    drain._repair_torn_tail(tmp_path / "gone.jsonl")  # missing → early return


def test_same_inode_false_for_broken_handle(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text("x\n")
    fh = open(p, "a", encoding="utf-8")
    fh.close()  # fileno() now raises ValueError
    assert drain._same_inode(fh, p) is False


def test_fsync_handle_reopens_uncached_path(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text("line\n")
    drain.reset_handles()  # ensure not cached
    drain._fsync_handle(p)  # must reopen by fd and fsync without error


def test_handle_evicts_lru_beyond_cap(tmp_path):
    """A batch touching more files than the cache holds evicts (and closes) the
    least-recently-used handles, keeping the open-fd count bounded."""
    drain.reset_handles()
    try:
        paths = [tmp_path / f"t{i}.jsonl" for i in range(drain.MAX_OPEN_HANDLES + 1)]
        first, rest = paths[0], paths[1:]
        fh1 = drain._handle(first)
        drain.append_line(first, {"type": "event", "id": 1})
        for p in rest:
            drain._handle(p)  # the last one pushes past the cap → evicts first
        assert len(drain._handles) == drain.MAX_OPEN_HANDLES
        assert str(first) not in drain._handles, "the LRU entry was evicted"
        assert str(rest[-1]) in drain._handles
        assert fh1.closed  # the evicted handle was closed
        assert not drain._handles[str(rest[-1])].closed
        # Eviction closes but never truncates: the line it flushed is still there.
        assert first.read_text(encoding="utf-8") == '{"type": "event", "id": 1}\n'
    finally:
        drain.reset_handles()


def test_intent_committed_none_without_insert_ids():
    # Only a thread-kind id → no fresh-insert table to probe → undecidable.
    assert drain._intent_committed([{"ids": [["thread", 1]]}]) is None


def test_tail_is_intent_only_false_on_unopenable_path(tmp_path):
    assert drain._tail_is_intent_only(tmp_path / "nope" / "x.jsonl", 0, set()) is False


def test_unstage_thread_noop_without_pending(archive_home):
    from thread_archive._store import init_db

    init_db()
    with get_session() as s:
        jsonl_log.unstage_thread(s, 1)  # nothing staged → returns cleanly
        s.rollback()


def test_recover_crashed_drain_skips_missing_and_untouched_files(archive_home, tmp_path):
    """Recovery tolerates a framed file that never landed (stat fails) and one
    whose size is at/below its baseline (nothing of the batch there)."""
    import_cc_session(tmp_path)
    tf = one_thread_file(archive_home)
    jsonl_log.reset_handles()
    real_size = tf.stat().st_size
    rel = str(tf.relative_to(archive_home / "truth"))
    intent = {
        "txn": "partial-crash",
        "at": "2026-01-02T00:00:00+00:00",
        "files": [
            # never created — its stat raises, recovery continues.
            {"path": "threads/nonexistent.jsonl", "baseline": 0,
             "ids": [["event", 999998]]},
            # exists but size == baseline — nothing of the batch is past it.
            {"path": rel, "baseline": real_size, "ids": [["event", 999999]]},
        ],
    }
    (archive_home / jsonl_log.DRAIN_INTENT_FILE).write_text(
        json.dumps(intent), encoding="utf-8")

    with jsonl_log._truth_write_lock():
        pass  # acquisition triggers _recover_crashed_drain

    assert (archive_home / jsonl_log.DRAIN_INTENT_FILE).read_text(encoding="utf-8") == ""
    assert tf.stat().st_size == real_size  # the real file was left untouched
    assert ta.verify(deep=True)["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# rebuild.py
# ══════════════════════════════════════════════════════════════════════════════
def test_parsed_equal_direct():
    assert rebuild._parsed_equal("x", "x") is True             # cheap raw equality
    assert rebuild._parsed_equal('{"a": 1}', '{"a":1}') is True  # parsed equality
    assert rebuild._parsed_equal("{bad", "{also-bad") is False   # both unparseable


def test_scan_truth_counts_bounds_ids_above_watermark(archive_home, tmp_path):
    """A verify racing live ingest bounds its scan by watermark: truth lines above
    it (threads, events, kg) are excluded from the counts."""
    import_cc_session(tmp_path)
    pytest.importorskip("thread_librarian")
    from thread_librarian.write import create_topic

    create_topic("Auth", "authentication concerns")
    base = scan_truth_counts()

    d = archive_home / "truth"
    with get_session() as s:
        ev_max = s.execute(text("SELECT max(id) FROM events")).scalar()
        th_max = s.execute(text("SELECT max(id) FROM threads")).scalar()
        kg_max = s.execute(text("SELECT max(id) FROM kg_events")).scalar()

    # A high-id event line appended to an existing (in-watermark) thread file, and
    # a high kg line — captured before the high thread file exists so it's a real,
    # scannable file.
    tf = one_thread_file(archive_home)
    append_jsonl(tf, [{"type": "event", "id": ev_max + 6000, "thread_id": th_max,
                       "payload": {"content": "future"}}])
    # A truth thread file whose id is above the watermark (skipped by the stem
    # bound — ULIDs compare lexicographically, so this stem sorts after th_max).
    high_tid = "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
    assert high_tid > th_max
    high_thread = jsonl_log._thread_file(d, high_tid, jsonl_log._shard_depth(d))
    high_thread.parent.mkdir(parents=True, exist_ok=True)
    high_thread.write_text(
        json.dumps({"type": "thread", "id": high_tid, "name": "future"}) + "\n"
        + json.dumps({"type": "event", "id": ev_max + 5000, "thread_id": high_tid,
                      "payload": {"content": "future"}}) + "\n",
        encoding="utf-8",
    )
    append_jsonl(d / jsonl_log.KG_EVENTS_FILE,
                 [{"type": "kg_event", "id": kg_max + 5000, "event_type": "x"}])

    bounded = scan_truth_counts(event_id_max=ev_max, thread_id_max=th_max,
                                kg_event_id_max=kg_max)
    assert bounded["threads"] == base["threads"]      # high thread file excluded
    assert bounded["events"] == base["events"]        # high event line excluded
    assert bounded["kg_events"] == base["kg_events"]  # high kg line excluded


def test_reindex_synthesizes_missing_thread_record(archive_home, tmp_path):
    """A truth file with events but no ``type:thread`` record gets a synthesized
    minimal thread so its events aren't dropped."""
    import_cc_session(tmp_path)
    before = event_count()
    tf = one_thread_file(archive_home)
    tid = tf.stem
    # Strip the metadata record: the file now carries only event lines.
    kept = [
        ln for ln in tf.read_text(encoding="utf-8").splitlines()
        if json.loads(ln).get("type", "event") != "thread"
    ]
    tf.write_text("\n".join(kept) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    ta.reindex()  # must synthesize a minimal thread record, not drop the events
    with get_session() as s:
        name = s.execute(text("SELECT name FROM threads WHERE id = :t"), {"t": tid}).scalar()
    assert name == f"thread:{tid}"      # the synthesized stub's name
    assert event_count() == before      # every event survived


def test_reindex_mixes_full_and_event_only_thread_files(archive_home, tmp_path):
    """A load batch that mixes a full ``type:thread`` record with an event-only
    file (metadata stripped) must not crash on the heterogeneous key-sets of the
    ``INSERT OR REPLACE`` — the synthesized stub carries only ``{id, name}`` while
    the full record carries every column. Both threads and all events survive."""
    import_cc_session(tmp_path, "full")
    import_cc_session(tmp_path, "stub")
    before = event_count()

    files = sorted((archive_home / "truth" / jsonl_log.THREADS_SUBDIR).rglob("*.jsonl"))
    assert len(files) == 2  # two distinct threads land in one reindex batch
    stub_file = files[-1]
    stub_tid = stub_file.stem
    kept = [
        ln for ln in stub_file.read_text(encoding="utf-8").splitlines()
        if json.loads(ln).get("type", "event") != "thread"
    ]
    stub_file.write_text("\n".join(kept) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    ta.reindex()  # heterogeneous thread_buf: a full record next to a {id, name} stub

    with get_session() as s:
        names = dict(s.execute(text("SELECT id, name FROM threads")).all())
    assert len(names) == 2                          # neither thread dropped
    assert names[stub_tid] == f"thread:{stub_tid}"  # event-only file → synthesized stub
    assert event_count() == before                  # every event survived


def test_reindex_with_vectors_flag_is_noop_without_embeddings(archive_home, tmp_path):
    """``reindex(vectors=True)`` runs the embed/cache arm; with the model gate off
    it reports zero rather than cold-loading torch."""
    import_cc_session(tmp_path)
    counts = ta.reindex(vectors=True)
    assert counts["vectors_embedded"] == 0
    assert counts["vectors_cached"] == 0


def test_reindex_refuses_when_disk_is_short(archive_home, tmp_path, monkeypatch):
    """The pre-flight refuses to build a second index copy when the disk can't
    hold it."""
    import_cc_session(tmp_path)

    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=0, used=0, free=0))
    with pytest.raises(RuntimeError, match="free"):
        ta.reindex()


def test_reindex_tolerates_stray_non_int_thread_file(archive_home, tmp_path):
    """A stray ``threads/<non-int>.jsonl`` is ignored by the load-order canonical
    check rather than crashing the rebuild."""
    import_cc_session(tmp_path)
    before = event_count()
    d = archive_home / "truth"
    stray = d / jsonl_log.THREADS_SUBDIR / "stray.jsonl"
    stray.write_text('{"type": "event", "id": 42}\n', encoding="utf-8")

    order = rebuild.thread_file_load_order(d)
    assert stray in order  # picked up, but its non-int stem is treated as non-canonical
    ta.reindex()
    assert event_count() == before  # the stray's fabricated id isn't a real event


def test_rebuild_truth_removes_old_monolith_files(archive_home, tmp_path):
    """Re-emitting per-thread truth drops the legacy monolithic files it replaces."""
    import_cc_session(tmp_path)
    d = archive_home / "truth"
    (d / "events.jsonl").write_text('{"legacy": true}\n', encoding="utf-8")
    (d / "threads.jsonl").write_text('{"legacy": true}\n', encoding="utf-8")

    jsonl_log.rebuild_truth_from_store(force=True)
    assert not (d / "events.jsonl").exists()
    assert not (d / "threads.jsonl").exists()
    assert ta.verify()["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# repair.py
# ══════════════════════════════════════════════════════════════════════════════
def test_repair_restores_missing_kg_event_from_index(archive_home, tmp_path):
    """A kg-event the index holds but the truth log lost is re-emitted from the
    index (the kg half of the containment restore)."""
    import_cc_session(tmp_path)
    pytest.importorskip("thread_librarian")
    from thread_librarian.write import create_topic

    create_topic("Auth", "authentication concerns")
    kg_path = archive_home / "truth" / jsonl_log.KG_EVENTS_FILE
    assert kg_path.exists()
    with get_session() as s:
        kg_id = s.execute(text("SELECT id FROM kg_events LIMIT 1")).scalar()
    # Drop the kg line from the truth log (index still holds it).
    kg_path.write_text("", encoding="utf-8")
    jsonl_log.reset_handles()

    assert ta.verify()["ok"] is False  # index ⊃ truth on kg
    res = ta.repair()
    assert res["kg_events_restored"] == 1
    restored = [json.loads(ln) for ln in kg_path.read_text(encoding="utf-8").splitlines()]
    assert any(r.get("id") == kg_id for r in restored)
    assert ta.verify()["ok"] is True


def test_repair_quarantine_appends_to_existing_ledger(archive_home, tmp_path):
    """A second repair with fresh damage appends to the existing fragments ledger
    rather than treating it as new."""
    import_cc_session(tmp_path, name="one")
    import_cc_session(tmp_path, name="two")
    pytest.importorskip("thread_librarian")
    from thread_librarian.write import create_topic

    from thread_archive._truth.repair import FRAGMENTS_FILE, QUARANTINE_SUBDIR

    create_topic("Auth", "authentication concerns")  # a healthy kg log to scan
    files = sorted((archive_home / "truth" / "threads").rglob("*.jsonl"))
    corrupt_event_line(files[0])
    ta.repair()
    ledger = archive_home / "truth" / QUARANTINE_SUBDIR / FRAGMENTS_FILE
    assert ledger.exists()
    n_first = len(ledger.read_text(encoding="utf-8").splitlines())

    corrupt_event_line(files[1])
    ta.repair()
    n_second = len(ledger.read_text(encoding="utf-8").splitlines())
    assert n_second > n_first  # appended, not overwritten
    assert ta.verify()["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# verify.py — deep/hashes internal branches
# ══════════════════════════════════════════════════════════════════════════════
def _author_keyless_event(tid: int, content: str) -> int:
    """Append an event with NO dedup_key through the truth seam; return its id."""
    from datetime import datetime, timezone

    from thread_archive._store import Event

    with get_session() as s:
        [ev] = jsonl_log.write_events(s, [Event(
            thread_id=tid, stream_id="nk", event_type="user_message_sent",
            payload={"content": content}, occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            dedup_key=None,
        )])
        s.commit()
        return ev.id


def test_verify_hashes_counts_keyless_events(archive_home, tmp_path):
    """An event with no dedup_key carries nothing to self-validate; the hashes
    pass counts it (``no_key``) on both stores rather than checking it."""
    import_cc_session(tmp_path)
    with get_session() as s:
        tid = s.execute(text("SELECT id FROM threads LIMIT 1")).scalar()
    _author_keyless_event(tid, "no key here")

    h = ta.verify(hashes=True)["hashes"]
    assert h["truth"]["no_key"] >= 1     # the keyless truth line was counted
    assert h["index"]["no_key"] >= 1     # and the keyless index row
    assert h["cross"]["compared"] > 0    # still cross-compared for parity


def test_deep_verify_tolerates_unparseable_kg_payload(archive_home, tmp_path):
    """A kg row whose payload isn't valid JSON is treated as content, not a crash,
    in the deep kg content comparison."""
    import_cc_session(tmp_path)
    pytest.importorskip("thread_librarian")
    from thread_librarian.write import create_topic

    create_topic("Auth", "authentication concerns")
    with get_session() as s:
        s.execute(text("UPDATE kg_events SET payload = 'not-json{'"))
        s.commit()

    v = ta.verify(deep=True)
    # The unparseable index payload can't match the truth fingerprint → mismatch,
    # but the pass completes and reports it rather than raising.
    assert v["deep"]["kg"]["content_mismatch"] >= 1


def test_verify_hashes_upgrades_index_selfcheck_to_integrity_check(archive_home, tmp_path):
    """On the hashes cadence a corrupt index page is caught by the fuller
    ``integrity_check`` (not just ``quick_check``), and named as such."""
    from thread_archive._config import resolve_paths

    import_cc_session(tmp_path)
    with get_session() as s:
        rootpage = s.execute(text(
            "SELECT rootpage FROM sqlite_master "
            "WHERE name='idx_events_caused_by' AND type='index'")).fetchone()[0]
        page_size = s.execute(text("PRAGMA page_size")).fetchone()[0]
    index_file = resolve_paths().index_path
    ta.close()  # drop pooled connections; WAL folds into the main file
    with open(index_file, "r+b") as f:
        f.seek((rootpage - 1) * page_size)
        f.write(b"\x00" * 32)

    v = ta.verify(hashes=True)
    assert v["index"]["check"] == "integrity_check"
    assert v["index"]["quick_check"] != "ok"
    assert "integrity_check" in v["failed_components"]


def test_verify_hashes_scans_keyless_and_unhashed_in_backup_mirror(archive_home, tmp_path):
    """The backup-mirror hash scan classifies mirror event lines by key: no id,
    no key, and a non-hash key are each counted, not checked."""
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    # Craft mirror lines the hash scan must classify (no watermark bounds a mirror).
    bf = next((dest / "threads").rglob("*.jsonl"))
    append_jsonl(bf, [
        {"type": "event", "payload": {"c": "x"}},                       # no id → skipped
        {"type": "event", "id": 900001, "payload": {"c": "y"}},          # no key → no_key
        {"type": "event", "id": 900002, "dedup_key": "plain-key-no-hash",
         "payload": {"c": "z"}},                                         # non-hash key
    ])

    res = ta.verify(hashes=True, backup=str(dest))
    bh = res["backup"]["hashes"]
    assert bh["no_key"] >= 1        # the keyless mirror line
    assert bh["unhashed_keys"] >= 1  # the non-hash-key mirror line


# ══════════════════════════════════════════════════════════════════════════════
# rebuild.py — helpers + re-emit pre-flight branches
# ══════════════════════════════════════════════════════════════════════════════
def test_emit_thread_file_without_metadata_record(archive_home):
    """``emit_thread_file`` writes only event lines when handed no thread record."""
    from thread_archive._store import init_db

    init_db()
    d = jsonl_log.log_dir()
    n = rebuild.emit_thread_file(d, 4242, 0, None, [
        {"id": 1, "thread_id": 4242, "payload": {"c": "a"}},
        {"id": 2, "thread_id": 4242, "payload": {"c": "b"}},
    ])
    assert n == 2
    path = jsonl_log._thread_file(d, 4242, 0)
    recs = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert all(r["type"] == "event" for r in recs)  # no thread record emitted


def test_hash_key_check_non_dict_payload():
    key = "stream:user_message_sent:blk=0:" + "0" * 16
    assert rebuild._hash_key_check("not-a-dict", key) is False   # non-dict, hashed key
    assert rebuild._hash_key_check({"c": "x"}, "no:hash:tail") is None  # no hash tail


def test_rebuild_truth_refuses_unparseable_store_payload(archive_home, tmp_path):
    """A store row whose payload isn't valid JSON fails the key-hash pre-flight
    (it can't re-hash to its key), so the re-emit refuses without force."""
    import_cc_session(tmp_path)
    with get_session() as s:
        eid = s.execute(text(
            "SELECT id FROM events WHERE dedup_key IS NOT NULL LIMIT 1")).scalar()
        s.execute(text("UPDATE events SET payload = 'not-valid-json{' WHERE id = :e"),
                  {"e": eid})
        s.commit()

    with pytest.raises(RuntimeError, match="dedup-key content hash"):
        jsonl_log.rebuild_truth_from_store()


def test_rebuild_truth_scans_kg_log_and_stray_and_empty_thread(archive_home, tmp_path):
    """The re-emit containment pre-flight tolerates a stray non-int thread file, an
    empty (event-less) thread file, and folds the kg log into its unknown-field
    scan — all without a spurious refusal on a store that holds the truth's units."""
    import_cc_session(tmp_path)
    pytest.importorskip("thread_librarian")
    from thread_librarian.write import create_topic

    create_topic("Auth", "authentication concerns")  # writes kg_events.jsonl
    d = archive_home / "truth"
    (d / jsonl_log.THREADS_SUBDIR / "stray.jsonl").write_text('{"type":"event"}\n')
    # An event-less thread file (only a metadata record) → its unit set is empty.
    empty = jsonl_log._thread_file(d, 90909, jsonl_log._shard_depth(d))
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text('{"type":"thread","id":90909,"name":"empty"}\n', encoding="utf-8")

    # Every real content unit is in the store, so the pre-flight passes and re-emits.
    res = jsonl_log.rebuild_truth_from_store()
    assert res["events"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# redact.py — whole-thread meta scrub + unredact restore
# ══════════════════════════════════════════════════════════════════════════════
def test_whole_thread_redact_scrubs_and_unredact_restores_title(archive_home, tmp_path):
    """A whole-thread redaction scrubs the (content-derived) title; unredacting
    restores the exact original title through the truth + index meta paths."""
    _, _uid, tid, _aid = _import_secret_session(tmp_path)
    with get_session() as s:
        original_title = s.execute(text("SELECT title FROM threads WHERE id = :t"),
                                   {"t": tid}).scalar()
    assert SECRET in original_title  # the auto-title carries the secret

    res = ta.redact(tid)  # whole thread → meta is derived content, scrubbed
    assert "title" in res["thread_meta_scrubbed"]
    with get_session() as s:
        scrubbed = s.execute(text("SELECT title FROM threads WHERE id = :t"),
                             {"t": tid}).scalar()
    assert scrubbed == "[redacted]"

    ta.unredact(res["key_id"])
    with get_session() as s:
        restored = s.execute(text("SELECT title FROM threads WHERE id = :t"),
                             {"t": tid}).scalar()
    assert restored == original_title
    # The truth thread record was restored too, not just the index projection.
    tf = one_thread_file(archive_home)
    metas = [json.loads(ln) for ln in tf.read_text(encoding="utf-8").splitlines()
             if json.loads(ln).get("type") == "thread"]
    assert metas and metas[-1].get("title") == original_title
