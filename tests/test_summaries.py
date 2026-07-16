"""Stored summaries — the librarian's summary write path and the merged queue.

``set_thread_summary`` is the one non-graph curation write: thread metadata (like the
title), durable via the thread's re-staged truth record rather than a ``KgEvent``, and
immediately synced into the thread-meta search docs. These tests prove the write lands
in all three stores (column, truth record, FTS doc), that it overwrites rather than
stacks, that the validation refuses garbage, that ``review_queue`` treats the summary
as half the per-thread commit (cited-but-unsummarized threads stay queued; a
still-ingesting thread is held back), and that a summary survives ``rm index.db &&
reindex`` — the durability the no-kg-event design leans on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sa_text

from thread_archive import _api as ta
from thread_archive._knowledge import (
    add_topic_evidence,
    create_topic,
    review_queue,
    set_thread_summary,
)
from thread_archive._knowledge.write import INDEXED_SUMMARY_MAX_CHARS, SUMMARY_MAX_CHARS
from thread_archive._store import Event, Thread, get_session

from .helpers import import_cc_session, one_thread_file

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
LONG_AGO = datetime(2026, 1, 1, 12, 0, 0)  # naive UTC, matching recorded_at's form


def _seed(source_id: str = "sess-1", *, quiet: bool = True) -> tuple[int, int]:
    """A conversation thread with one user event; returns (thread_id, event_id).
    ``quiet=True`` backdates the event's ``recorded_at`` so the thread clears the
    queue's quiet window; ``quiet=False`` leaves the server default (now) — a
    still-ingesting thread."""
    ta.open_archive()
    with get_session() as s:
        t = Thread(
            name=f"claude-code:{source_id}", title=source_id, thread_type="conversation",
            source="claude-code", source_id=source_id,
        )
        s.add(t)
        s.flush()
        e = Event(
            thread_id=t.id, stream_id=source_id, event_type="user_message_sent",
            payload={"content": "hello"}, occurred_at=NOW,
        )
        if quiet:
            e.recorded_at = LONG_AGO
        s.add(e)
        s.flush()
        ids = (t.id, e.id)
        s.commit()
    return ids


def _summary_docs(tid: int) -> list[str]:
    with get_session() as s:
        return list(s.execute(
            sa_text("SELECT content FROM events_fts WHERE thread_id = :tid AND content_type = 'summary'"),
            {"tid": tid},
        ).scalars())


# ── the write ─────────────────────────────────────────────────────────────────
def test_set_summary_lands_in_column_truth_and_search_doc(archive_home, tmp_path):
    """One call, three stores: the thread row, the thread's truth record
    (latest-wins — the durability), and the thread-meta FTS doc."""
    import_cc_session(tmp_path)
    with get_session() as s:
        tid = s.execute(sa_text("SELECT id FROM threads")).scalar()

    r = set_thread_summary(tid, "A dense summary about widget frobnication.")
    assert r == {"thread_id": tid, "fields": ["summary"]}

    with get_session() as s:
        assert s.get(Thread, tid).summary == "A dense summary about widget frobnication."
    # the latest thread record in truth carries it
    records = [
        json.loads(ln) for ln in one_thread_file(archive_home).read_text().splitlines()
    ]
    thread_records = [r for r in records if r.get("type") == "thread"]
    assert thread_records[-1]["summary"] == "A dense summary about widget frobnication."
    # and it's a search doc immediately
    assert _summary_docs(tid) == ["A dense summary about widget frobnication."]


def test_set_summary_overwrites_and_sets_fields_independently(archive_home, tmp_path):
    import_cc_session(tmp_path)
    with get_session() as s:
        tid = s.execute(sa_text("SELECT id FROM threads")).scalar()

    set_thread_summary(tid, "first version")
    r = set_thread_summary(tid, indexed_summary="## part one (event 1)\ndetail")
    assert r["fields"] == ["indexed_summary"]
    with get_session() as s:
        t = s.get(Thread, tid)
        # indexed-only write left the short summary alone
        assert t.summary == "first version"
        assert t.indexed_summary.startswith("## part one")

    set_thread_summary(tid, "second version")
    with get_session() as s:
        assert s.get(Thread, tid).summary == "second version"
    # overwrite, not stack: still exactly one summary doc, the new content
    assert _summary_docs(tid) == ["second version"]


def test_set_summary_validation(archive_home):
    tid, _ = _seed()
    with pytest.raises(ValueError, match="nothing to set"):
        set_thread_summary(tid)
    with pytest.raises(ValueError, match="nothing to set"):
        set_thread_summary(tid, "   ")
    with pytest.raises(ValueError, match="no thread"):
        set_thread_summary(99_999_999, "s")
    topic = create_topic("A Topic")["topic_id"]
    with pytest.raises(ValueError, match="is a topic"):
        set_thread_summary(topic, "s")
    with pytest.raises(ValueError, match="max"):
        set_thread_summary(tid, "x" * (SUMMARY_MAX_CHARS + 1))
    with pytest.raises(ValueError, match="max"):
        set_thread_summary(tid, indexed_summary="x" * (INDEXED_SUMMARY_MAX_CHARS + 1))


def test_summary_survives_reindex(archive_home, tmp_path):
    """The no-kg-event design's load-bearing guarantee: the thread truth record
    restores the summary on a from-scratch rebuild, and rebuild_fts regenerates
    its search doc from the restored column."""
    import_cc_session(tmp_path)
    with get_session() as s:
        tid = s.execute(sa_text("SELECT id FROM threads")).scalar()
    set_thread_summary(tid, "survives the rebuild", indexed_summary="## all of it (event 1)\nx")

    ta.reindex()

    with get_session() as s:
        t = s.get(Thread, tid)
        assert t.summary == "survives the rebuild"
        assert t.indexed_summary.startswith("## all of it")
    assert _summary_docs(tid) == ["survives the rebuild"]


# ── the merged queue: done = cited AND summarized ─────────────────────────────
def test_review_queue_requires_both_citation_and_summary(archive_home):
    tid, eid = _seed("rq-both")
    assert [r["id"] for r in review_queue()] == [tid]

    # summary alone doesn't finish the thread…
    set_thread_summary(tid, "summarized but never cited")
    assert [r["id"] for r in review_queue()] == [tid]

    # …the citation completes the pair and the thread leaves
    topic = create_topic("T")["topic_id"]
    add_topic_evidence(topic, eid, tid, "q")
    assert review_queue() == []


def test_cited_but_unsummarized_threads_requeue(archive_home):
    """The backlog case this redefinition exists for: threads the librarian cited
    under the citations-only contract re-enter the queue until summarized."""
    tid, eid = _seed("rq-legacy")
    topic = create_topic("T2")["topic_id"]
    add_topic_evidence(topic, eid, tid, "q")
    assert [r["id"] for r in review_queue()] == [tid]  # cited, yet still queued

    set_thread_summary(tid, "now summarized")
    assert review_queue() == []


def test_review_queue_holds_back_still_ingesting_threads(archive_home):
    quiet, _ = _seed("rq-quiet", quiet=True)
    fresh, _ = _seed("rq-fresh", quiet=False)  # recorded_at = now → inside the window

    assert [r["id"] for r in review_queue()] == [quiet]
    # the window is the only thing holding the fresh one back
    assert {r["id"] for r in review_queue(quiet_minutes=0)} == {quiet, fresh}


def test_review_queue_exclusions_still_hold(archive_home):
    tid, _ = _seed("rq-mine")
    # own session excluded
    assert review_queue(exclude_source_id="rq-mine") == []
    # archived threads are nobody's backlog
    with get_session() as s:
        s.get(Thread, tid).archived = True
        s.commit()
    assert review_queue() == []
    # topics and event-less stubs never appear
    create_topic("Not A Conversation")
    with get_session() as s:
        s.add(Thread(name="claude-code:stub", thread_type="conversation",
                     source="claude-code", source_id="stub"))
        s.commit()
    assert review_queue() == []
