"""Thread-meta search docs, the search blacklist, canonical time bounds, and the
two-pass code/pipe-OR federation.

- ``index_thread_meta`` derives title/summary docs (diff-based sync, anchored to
  the thread's first indexed event) and search surfaces them
- ``exclude_from_search`` actually drops a thread's hits (and its meta docs)
- since/until bounds resolve to the store's canonical timestamp form
- code-identifier / pipe-OR queries reach old hits through the MATCH pass
- ``match_window`` centres the reranker's span on the match
"""

from __future__ import annotations

import json

from sqlalchemy import update

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import index_thread_meta, rebuild_fts, search
from thread_archive._retrieval._classify import resolve_relative_date
from thread_archive._retrieval.rank import match_window
from thread_archive._store import Thread, init_db, use_session


def _write_cc(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _cc_turn(uid, aid, user_text, asst_text, day=1):
    return [
        {"type": "user", "uuid": uid, "timestamp": f"2026-01-0{day}T10:00:00Z",
         "sessionId": "s", "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": aid, "timestamp": f"2026-01-0{day}T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": asst_text}]}},
    ]


def _seed(archive_home) -> int:
    """One thread; returns its id."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_cc(f, _cc_turn("u1", "a1", "please fix the flaky importer test",
                          "The importer test raced the watcher; pinned the clock."))
    import_session_incremental(f, "proj:x")
    return search("importer")[0]["thread_id"]


def _set_thread(tid: int, **values) -> None:
    with use_session() as s:
        s.execute(update(Thread).where(Thread.id == tid).values(**values))
        s.commit()


def test_thread_meta_docs_searchable(archive_home) -> None:
    tid = _seed(archive_home)
    # Vocabulary that appears ONLY in the title / summary, never in a message.
    _set_thread(tid, title="Kazoo orchestra migration",
                summary="Moving the kazoo orchestra to the new concert hall.")
    assert index_thread_meta() == 2

    title_hits = search("kazoo orchestra", content_types=["title"])
    assert title_hits and title_hits[0]["thread_id"] == tid
    assert title_hits[0]["content_type"] == "title"

    summary_hits = search("concert hall", content_types=["summary"])
    assert summary_hits and summary_hits[0]["thread_id"] == tid

    # Diff-based: a second sync with nothing changed writes nothing.
    assert index_thread_meta() == 0

    # A changed title replaces its doc.
    _set_thread(tid, title="Tuba ensemble migration")
    assert index_thread_meta() == 1
    assert not search("kazoo", content_types=["title"])
    assert search("tuba", content_types=["title"])


def test_thread_meta_survives_rebuild(archive_home) -> None:
    tid = _seed(archive_home)
    _set_thread(tid, title="Kazoo orchestra migration", summary=None)
    index_thread_meta()
    rebuild_fts()
    hits = search("kazoo", content_types=["title"])
    assert hits and hits[0]["thread_id"] == tid


def test_exclude_from_search_honored(archive_home) -> None:
    tid = _seed(archive_home)
    _set_thread(tid, title="Kazoo orchestra migration")
    index_thread_meta()
    assert search("importer")

    _set_thread(tid, exclude_from_search=True)
    assert not search("importer"), "blacklisted thread's events still surfaced"
    # An explicit thread scope is deliberate and bypasses the blacklist.
    assert search("importer", thread_id=tid)
    # The meta sync drops the excluded thread's docs too.
    index_thread_meta()
    assert not search("kazoo", content_types=["title"])


def test_time_bounds_canonical_form() -> None:
    # Relative and ISO inputs both resolve to naive-UTC, space-separated — the
    # form stored occurred_at strings collate against.
    assert "T" not in resolve_relative_date("7d")
    assert resolve_relative_date("2026-07-03T05:00:00+00:00") == "2026-07-03 05:00:00.000000"
    assert resolve_relative_date("2026-07-03") == "2026-07-03 00:00:00.000000"
    # Aware non-UTC converts to UTC.
    assert resolve_relative_date("2026-07-03T00:00:00-05:00") == "2026-07-03 05:00:00.000000"
    # Garbage passes through untouched.
    assert resolve_relative_date("not-a-date") == "not-a-date"


def test_since_filter_includes_boundary_day(archive_home) -> None:
    _seed(archive_home)
    # The fixture events occurred 2026-01-01; a since bound earlier that same day
    # must include them (the old 'T' bound lexicographically excluded the whole day).
    assert search("importer", since="2026-01-01T05:00:00+00:00")
    assert not search("importer", since="2026-01-02")


def test_code_query_reaches_old_hits_past_recency_cap(archive_home) -> None:
    """The MATCH pass keeps old identifier hits reachable when >limit newer
    matches exist (a single recency-ordered LIKE pass would cap them out)."""
    init_db()
    # The canonical old discussion: identifier-dense (bm25 ranks it top).
    old = archive_home / "old.jsonl"
    _write_cc(old, _cc_turn("u-old", "a-old",
                            "deep dive: frobnicate_widget internals — frobnicate_widget "
                            "state machine and frobnicate_widget failure modes",
                            "frobnicate_widget assumed a non-empty list."))
    import_session_incremental(old, "proj:old")

    # >limit newer passing mentions, one per message — these fill any
    # recency-ordered pass wall to wall.
    lines = []
    for i in range(60):
        lines += _cc_turn(f"u{i}", f"a{i}",
                          f"note {i}: frobnicate_widget tweak {i}",
                          f"ack {i}: adjusted frobnicate_widget again", day=2)
    new = archive_home / "new.jsonl"
    _write_cc(new, lines)
    import_session_incremental(new, "proj:new")

    old_tid = search("state machine")[0]["thread_id"]
    hits = search("frobnicate_widget", limit=10)
    assert any(h["thread_id"] == old_tid for h in hits), \
        "old identifier hit unreachable — recency cap regressed"


def test_pipe_or_uses_match_pass(archive_home) -> None:
    _seed(archive_home)
    assert search("importer | nonexistentzzz")


def test_match_window_centres_on_match() -> None:
    doc = ("intro filler. " * 50) + "the needle sits here" + (" trailing filler." * 50)
    win = match_window(doc, ["needle"], 200)
    assert "needle" in win and len(win) <= 200
    # No match → head of doc; short docs pass through whole.
    assert match_window(doc, ["absent"], 200) == doc[:200]
    assert match_window("short", ["needle"], 200) == "short"
