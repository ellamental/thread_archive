"""The line-stream cursor must not assume its source is append-only.

Every provider file watcher re-reads a transcript and imports the slice past its
watermark. That slice is only meaningful if the lines under the watermark are still
the same lines — and the sources can be repaired, truncated, or rewritten in place,
sometimes back to the *same byte length*. These are the ways content used to go
missing forever, each of which now rewinds the cursor and re-imports (the dedup_key
check collapses what's already held, so a rewind costs work, never duplicates).

The final case is the mirror image: a torn tail line, which looks like a rewrite but
is a legitimate append caught mid-write and must NOT force a rewind.
"""

from __future__ import annotations

import json

from sqlalchemy import select

import thread_archive as ta
from thread_archive.store import Event, ImportState, get_session

from .helpers import append_jsonl, cc_assistant, cc_user, event_count, write_jsonl


def _state() -> ImportState:
    with get_session() as s:
        return s.execute(select(ImportState)).scalars().one()


def test_same_size_rewrite_is_caught(archive_home, tmp_path) -> None:
    """A line edited to the same serialized length leaves size identical — the old
    size-equality early-out declared the file unchanged and never looked again."""
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user(content="the original question aaa"), cc_assistant()])
    ta.import_path(f)
    before = event_count()

    # Same byte length, different content.
    write_jsonl(f, [cc_user(content="the original question bbb"), cc_assistant()])
    assert len(f.read_bytes()) == _state().last_file_size

    res = ta.import_path(f)

    assert res.events_created > 0
    assert event_count() > before
    assert ta.search("question bbb")  # the rewritten turn landed
    assert ta.search("question aaa")  # and the version it replaced is still held


def test_repaired_interior_line_is_not_skipped(archive_home, tmp_path) -> None:
    """The line cursor counts *parsed* lines, so a malformed line in the middle of a
    file leaves it one short of the physical position. Repair that line and a naive
    tail slice steps over the repaired content for good."""
    f = tmp_path / "sess.jsonl"
    good_user = json.dumps(cc_user())
    good_asst = json.dumps(cc_assistant(text_content="the trailing answer"))
    f.write_text(f"{good_user}\n{{\"type\": \"user\", BROKEN\n{good_asst}\n", encoding="utf-8")

    ta.import_path(f)
    assert _state().last_line_count == 2  # parsed 2 of 3 physical lines

    # The middle line is repaired in place — every later line shifts by one.
    repaired = json.dumps(cc_user(content="the repaired middle turn"))
    f.write_text(f"{good_user}\n{repaired}\n{good_asst}\n", encoding="utf-8")

    ta.import_path(f)

    assert ta.search("repaired middle turn")
    assert ta.search("trailing answer")


def test_truncate_and_regrow_reimports(archive_home, tmp_path) -> None:
    """Truncated to nothing and rewritten from scratch: the cursor points into content
    that no longer exists."""
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user(), cc_assistant()])
    ta.import_path(f)

    f.write_bytes(b"")
    ta.import_path(f)  # empty file: watermark rewinds to 0 lines

    write_jsonl(f, [cc_user(content="the rewritten conversation"), cc_assistant()])
    ta.import_path(f)

    assert ta.search("rewritten conversation")


def test_torn_tail_line_completes_without_a_rewind(archive_home, tmp_path) -> None:
    """A poll catching the writer mid-append sees a half-written final line. Its bytes
    are a *prefix* of the completed line, so this is an append, not a rewrite: the
    unparsed line simply imports on the next poll, exactly once."""
    f = tmp_path / "sess.jsonl"
    user_line = json.dumps(cc_user())
    asst_line = json.dumps(cc_assistant(text_content="the answer that was mid-write"))

    f.write_text(f"{user_line}\n{asst_line[:20]}", encoding="utf-8")  # torn, no newline
    ta.import_path(f)
    assert _state().last_line_count == 1  # only the user line parsed
    after_torn = event_count()

    f.write_text(f"{user_line}\n{asst_line}\n", encoding="utf-8")  # writer finishes
    res = ta.import_path(f)

    assert res.events_created > 0
    assert ta.search("mid-write")
    # The user turn was not re-imported as a duplicate: the cursor held, it didn't rewind.
    with get_session() as s:
        users = s.execute(
            select(Event).where(Event.event_type == "user_message_sent")
        ).scalars().all()
    assert len(users) == 1
    assert event_count() > after_torn


def test_unchanged_file_is_a_no_op(archive_home, tmp_path) -> None:
    """The digest early-out still skips an untouched file (and stamps the watermark)."""
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user(), cc_assistant()])
    ta.import_path(f)
    before = event_count()

    res = ta.import_path(f)

    assert res.events_created == 0
    assert event_count() == before
    assert _state().last_content_hash


def test_watermark_without_a_digest_is_backfilled(archive_home, tmp_path) -> None:
    """Watermarks written before the digest existed can't be verified. They keep the
    old size-only behavior for one poll — no spurious rewind, no re-import — and pick
    up a digest as they go, so the poll after that is provable."""
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user(), cc_assistant()])
    ta.import_path(f)
    before = event_count()

    with get_session() as s:
        s.execute(select(ImportState)).scalars().one().last_content_hash = None
        s.commit()

    res = ta.import_path(f)  # unchanged file, unverifiable watermark

    assert res.events_created == 0
    assert event_count() == before
    assert _state().last_content_hash  # ...and now it can be proved

    append_jsonl(f, [cc_user(name="second", content="the appended tail")])
    assert ta.import_path(f).events_created > 0
    assert ta.search("appended tail")


def test_a_turn_split_across_polls_stays_one_stream(archive_home, tmp_path) -> None:
    """The user line lands in one poll, its assistant reply in the next. The reply
    belongs to the turn it answers — a fresh stream id would orphan it."""
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user()])
    ta.import_path(f)

    append_jsonl(f, [cc_assistant()])
    ta.import_path(f)

    with get_session() as s:
        user = s.execute(
            select(Event).where(Event.event_type == "user_message_sent")
        ).scalars().one()
        reply = s.execute(
            select(Event).where(Event.event_type == "api_request_completed")
        ).scalars().first()

    assert reply is not None
    assert reply.stream_id == user.stream_id
