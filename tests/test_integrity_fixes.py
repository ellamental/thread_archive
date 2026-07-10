"""Regression tests for the truth-store integrity guarantees:

1. Source-import watermarks survive reindex (carried from the previous index and
   seeded from the ``import_state.jsonl`` checkpoint snapshot), so a rebuild never
   makes the importer adopt an active source at EOF and skip its unimported tail.
2. A torn truth tail (crash mid-append) is newline-repaired before the next append,
   so the fragment can't consume a later valid event.
3. The before-commit truth drain is all-or-nothing: a failure partway rolls the
   touched files back to their pre-drain size, so a failed batch leaves no partial
   records for reindex to resurrect.
4. ``backup`` is a true mirror: destination files with no source counterpart are
   deleted, so a shard rebalance can't leave a stale layout that shadows current
   records on restore.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

import thread_archive as ta
from thread_archive.store import Event, ImportState, Thread, get_session, init_db
from thread_archive.truth import jsonl_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello integrity"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back"}]}}
LATER_USER = {"type": "user", "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
              "cwd": "/proj", "message": {"role": "user", "content": "the appended tail line"}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _append_cc(path, lines):
    with open(path, "a", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_reindex_preserves_import_state_so_source_tails_still_import(archive_home, tmp_path) -> None:
    """The critical loss path: source grows → reindex (wipes nothing now) → the tail
    must still import instead of being adopted-away at EOF."""
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    # The source grows before anyone polls it again.
    _append_cc(f, [LATER_USER])

    ta.reindex()

    # The watermark survived the rebuild...
    with get_session() as s:
        state = s.execute(select(ImportState)).scalars().one()
        assert state.last_line_count == 2

    # ...so the next import picks up the appended tail rather than skipping it.
    res = ta.import_path(f)
    assert res.events_created > 0
    assert ta.search("appended tail")


def test_reindex_restores_import_state_from_checkpoint_snapshot(archive_home, tmp_path) -> None:
    """With the previous index gone entirely (``rm index.db``), the checkpoint's
    ``import_state.jsonl`` snapshot seeds the cursors."""
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    ta.checkpoint()  # writes truth/import_state.jsonl
    assert (archive_home / "truth" / "import_state.jsonl").exists()

    _append_cc(f, [LATER_USER])

    ta.close()
    (archive_home / "index.db").unlink()
    ta.reindex()

    with get_session() as s:
        state = s.execute(select(ImportState)).scalars().one()
        assert state.last_line_count == 2

    res = ta.import_path(f)
    assert res.events_created > 0
    assert ta.search("appended tail")


def test_torn_tail_is_repaired_and_cannot_consume_the_next_event(archive_home, tmp_path) -> None:
    """A crash mid-append leaves a torn (newline-less) fragment; the next append must
    isolate it rather than gluing a valid record onto it."""
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)
    before = ta.status()["events"]

    truth_files = list((archive_home / "truth" / "threads").rglob("*.jsonl"))
    assert len(truth_files) == 1
    thread_file = truth_files[0]

    # Simulate the crash: a torn fragment with no trailing newline, and the writer's
    # cached append handle gone with the process.
    jsonl_log.reset_handles()
    with open(thread_file, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "id": 99999, "torn": tr')

    # The next commit appends a valid event through the normal staged path.
    with get_session() as s:
        tid = s.execute(select(Thread.id)).scalars().first()
        jsonl_log.write_events(s, [Event(
            thread_id=tid, stream_id="x2", event_type="user_message_sent",
            payload={"content": "survives the torn tail"}, occurred_at=_now(), dedup_key="k-torn",
        )])
        s.commit()

    # The fragment is its own line; the new record parses.
    lines = thread_file.read_text(encoding="utf-8").splitlines()
    assert lines[-2].endswith('"torn": tr')
    assert json.loads(lines[-1])["payload"]["content"] == "survives the torn tail"

    # Reindex loses only the fragment (which never committed), not the valid event.
    counts = ta.reindex()
    assert counts["parse_errors"] == 1
    assert ta.status()["events"] == before + 1
    assert ta.search("survives the torn")


def test_drain_failure_rolls_back_every_appended_record(archive_home, monkeypatch) -> None:
    """A mid-drain write failure must leave the truth file exactly as it was — no
    partial batch for a later reindex to resurrect."""
    init_db()
    with get_session() as s:
        s.add(Thread(id=1, name="t1"))
        s.commit()
        jsonl_log.write_events(s, [Event(
            thread_id=1, stream_id="s0", event_type="user_message_sent",
            payload={"content": "committed baseline"}, occurred_at=_now(), dedup_key="k0",
        )])
        s.commit()

    truth_file = next((archive_home / "truth" / "threads").rglob("*.jsonl"))
    baseline = truth_file.read_bytes()

    real_append = jsonl_log._append_line
    calls = {"n": 0}

    def _fail_second(path, rec):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated disk-full mid-batch")
        real_append(path, rec)

    monkeypatch.setattr(jsonl_log, "_append_line", _fail_second)

    with get_session() as s:
        jsonl_log.write_events(s, [
            Event(thread_id=1, stream_id="s1", event_type="user_message_sent",
                  payload={"content": "first of failed batch"}, occurred_at=_now(), dedup_key="k1"),
            Event(thread_id=1, stream_id="s2", event_type="user_message_sent",
                  payload={"content": "second of failed batch"}, occurred_at=_now(), dedup_key="k2"),
        ])
        with pytest.raises(OSError):
            s.commit()

    # Record 1 of the failed batch was truncated away with record 2's failure.
    assert truth_file.read_bytes() == baseline
    with get_session() as s:
        contents = [e.payload["content"] for e in s.execute(select(Event)).scalars()]
    assert contents == ["committed baseline"]


def test_backup_deletes_files_the_source_no_longer_has(archive_home, tmp_path) -> None:
    """The mirror must drop stale destination files (e.g. a pre-rebalance layout), or
    a restore-reindex loads them last and they shadow the current records."""
    f = tmp_path / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    dest = tmp_path / "bk"
    ta.backup(str(dest))

    # A stale twin left from an old shard layout (absent from the live truth dir).
    stale = dest / "threads" / "999999.jsonl"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"type": "thread", "id": 999999, "name": "stale"}\n', encoding="utf-8")

    res = ta.backup(str(dest))
    assert res["files_deleted"] >= 1
    assert not stale.exists()
    # Everything the source has is still mirrored.
    src_files = {p.relative_to(archive_home / "truth") for p in (archive_home / "truth").rglob("*") if p.is_file()}
    dest_files = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}
    assert src_files == dest_files
