"""Stored summaries — thread metadata the archive stores, indexes, and rebuilds.

A summary write is thread metadata (like the title), durable via the thread's
re-staged truth record rather than a ``KgEvent``, and immediately synced into
the thread-meta search docs. These tests prove a write lands in all three
stores (column, truth record, FTS doc), that it overwrites rather than stacks,
and that a summary survives ``rm index.db && reindex`` — the durability the
no-kg-event design leans on. The production writer is external; the seeding
here (:mod:`tests.kg_seed`) leaves the same three-store shape behind.
"""

from __future__ import annotations

import json

from sqlalchemy import text as sa_text

from thread_archive import _api as ta
from thread_archive._store import Thread, get_session

from .helpers import import_cc_session, one_thread_file
from .kg_seed import set_thread_summary


def _summary_docs(tid: int) -> list[str]:
    with get_session() as s:
        return list(s.execute(
            sa_text("SELECT content FROM events_fts WHERE thread_id = :tid AND content_type = 'summary'"),
            {"tid": tid},
        ).scalars())


# ── the write ─────────────────────────────────────────────────────────────────
def test_set_summary_lands_in_column_truth_and_search_doc(archive_home, tmp_path):
    """One write, three stores: the thread row, the thread's truth record
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
