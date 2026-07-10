"""Tests for the drain-intent frame — crash-window transaction framing for
append batches.

The drain writes an intent (files + baselines + record ids) durably before its
first data append and empties it after the last data fsync, inside the
truth-write mutex. Recovery runs on every acquisition of that mutex:

1. A normal drain leaves the intent empty (the happy path never exposes it).
2. An uncommitted partial batch (crash mid-drain) is rolled back to baseline —
   including its torn final fragment — and the next import proceeds cleanly.
3. A batch whose COMMIT landed (crash between the data fsync and the intent
   clear) keeps its records: truncating committed truth is the forbidden
   direction.
4. A tail holding records outside the intent (another writer appended after
   the crash) is left in place rather than cut.
5. A torn intent write (crash before any data append) is discarded and the
   truth is untouched.
6. A file the crashed batch *created* (baseline null) is unlinked, not
   truncated — no ghost thread file.
"""

from __future__ import annotations

import json

import thread_archive as ta
from thread_archive.truth import jsonl_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello intent"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _import_session(tmp_path, name="sess"):
    f = tmp_path / f"{name}.jsonl"
    user = {**USER, "uuid": f"u-{name}",
            "message": {"role": "user", "content": f"hello intent {name}"}}
    asst = {**ASSISTANT, "uuid": f"a-{name}"}
    _write_cc(f, [user, asst])
    ta.import_path(f)


def _one_thread_file(archive_home):
    return next((archive_home / "truth" / "threads").rglob("*.jsonl"))


def _intent_path(archive_home):
    return archive_home / jsonl_log.DRAIN_INTENT_FILE


def _fake_event(ev_id, extra=""):
    """A parseable event line whose id the index does not hold."""
    return json.dumps({"type": "event", "id": ev_id, "thread_id": 1,
                       "stream_id": "crashed", "event_type": "user_message_sent",
                       "payload": {"content": f"never committed {extra}"},
                       "occurred_at": "2026-01-02T00:00:00+00:00",
                       "dedup_key": f"crash:{ev_id}"})


def _plant_crash(archive_home, tf, *, torn=True):
    """Fabricate a crashed drain: framed uncommitted records (+ optionally the
    torn final fragment of the crash itself) past the file's baseline, with the
    surviving intent that describes them. Returns the baseline size."""
    jsonl_log.reset_handles()
    baseline = tf.stat().st_size
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write(_fake_event(999001) + "\n")
        fh.write(_fake_event(999002) + "\n")
        if torn:
            fh.write('{"type": "event", "id": 999003, "torn')  # no newline — the crash
    rel = str(tf.relative_to(archive_home / "truth"))
    intent = {"txn": "crashed-txn", "at": "2026-01-02T00:00:00+00:00",
              "files": [{"path": rel, "baseline": baseline,
                         "ids": [["event", 999001], ["event", 999002], ["event", 999003]]}]}
    _intent_path(archive_home).write_text(json.dumps(intent), encoding="utf-8")
    return baseline


def test_normal_drain_leaves_intent_empty(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    p = _intent_path(archive_home)
    assert p.exists()
    assert p.read_text(encoding="utf-8") == ""


def test_recovery_rolls_back_uncommitted_partial_batch(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    baseline = _plant_crash(archive_home, tf)

    # The next import's drain acquires the truth-write mutex → recovery first.
    _import_session(tmp_path, "after-crash")
    assert _intent_path(archive_home).read_text(encoding="utf-8") == ""
    content = tf.read_text(encoding="utf-8")
    assert "never committed" not in content and "torn" not in content
    assert tf.stat().st_size == baseline
    assert ta.verify(deep=True)["ok"] is True


def test_recovery_keeps_committed_batch(archive_home, tmp_path) -> None:
    """Crash after COMMIT but before the intent clear: the batch's ids are in the
    index, so its records must be kept — truncating them would put the index
    ahead of the truth."""
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    raw = tf.read_bytes()
    lines = raw.splitlines(keepends=True)
    # Frame the committed tail of the file: every record from the last event
    # line onward (the trailing thread-metadata record rides along in the frame).
    idx = max(i for i, ln in enumerate(lines) if json.loads(ln)["type"] == "event")
    baseline = sum(len(ln) for ln in lines[:idx])
    ids = [[json.loads(ln)["type"], json.loads(ln)["id"]] for ln in lines[idx:]]
    rel = str(tf.relative_to(archive_home / "truth"))
    intent = {"txn": "cleared-too-late", "at": "2026-01-02T00:00:00+00:00",
              "files": [{"path": rel, "baseline": baseline, "ids": ids}]}
    _intent_path(archive_home).write_text(json.dumps(intent), encoding="utf-8")

    with jsonl_log._truth_write_lock():
        pass
    assert tf.read_bytes() == raw
    assert _intent_path(archive_home).read_text(encoding="utf-8") == ""
    assert ta.verify(deep=True)["ok"] is True


def test_recovery_refuses_tail_with_foreign_records(archive_home, tmp_path) -> None:
    """Records past the baseline that the intent doesn't claim belong to another
    writer — recovery must leave the file alone (and still clear the intent)."""
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    _plant_crash(archive_home, tf, torn=False)
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write(_fake_event(888777, "foreign") + "\n")  # not in the intent's ids
    size = tf.stat().st_size

    with jsonl_log._truth_write_lock():
        pass
    assert tf.stat().st_size == size, "a mismatched tail must never be cut"
    assert _intent_path(archive_home).read_text(encoding="utf-8") == ""


def test_torn_intent_is_discarded_and_truth_untouched(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    size = tf.stat().st_size
    _intent_path(archive_home).write_text('{"txn": "torn-mid-wri', encoding="utf-8")

    with jsonl_log._truth_write_lock():
        pass
    assert _intent_path(archive_home).read_text(encoding="utf-8") == ""
    assert tf.stat().st_size == size
    assert ta.verify(deep=True)["ok"] is True


def test_recovery_unlinks_file_the_crashed_batch_created(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    ghost = archive_home / "truth" / "threads" / "424242.jsonl"
    ghost.write_text(_fake_event(999101) + "\n", encoding="utf-8")
    intent = {"txn": "created-then-crashed", "at": "2026-01-02T00:00:00+00:00",
              "files": [{"path": "threads/424242.jsonl", "baseline": None,
                         "ids": [["thread", 424242], ["event", 999101]]}]}
    _intent_path(archive_home).write_text(json.dumps(intent), encoding="utf-8")

    with jsonl_log._truth_write_lock():
        pass
    assert not ghost.exists()
    assert _intent_path(archive_home).read_text(encoding="utf-8") == ""
    assert ta.verify(deep=True)["ok"] is True


def test_reindex_resolves_crashed_drain_before_reading(archive_home, tmp_path) -> None:
    """A reindex that runs before any writer must not materialize the partial
    batch: it resolves the intent first."""
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    baseline = _plant_crash(archive_home, tf)

    counts = ta.reindex()
    assert counts["parse_errors"] == 0  # the torn fragment was rolled back, not read
    assert tf.stat().st_size == baseline
    assert ta.verify(deep=True)["ok"] is True
