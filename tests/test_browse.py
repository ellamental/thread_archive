"""Query-less browse + the topic tree read.

An empty ``search()`` query lists threads (one row per thread, by last
activity) instead of matching events; ``read_thread('topics')`` renders the
curated topic hierarchy. These pin the browse row shape, the structural
filters (since/source/types/limit/sort), the hidden-by-default types, the
renderers, and the tree's indentation/budget behavior.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from thread_archive import _api as ta
from thread_archive._knowledge import create_topic, link_threads, topic_tree
from thread_archive._retrieval import format_results, index_events, read_thread, search
from thread_archive._store import Event, Thread, get_session, init_db

from .test_search import _cc_turn, _write_cc


def _import_cc(archive_home, name: str, user_text: str, asst_text: str, day: int) -> None:
    from thread_archive._importers import import_session_incremental

    f = archive_home / f"{name}.jsonl"
    _write_cc(f, _cc_turn(f"u-{name}", f"a-{name}", user_text, asst_text, day))
    import_session_incremental(f, f"proj:{name}")


def _seed_direct(source: str, title: str, day: int, thread_type: str = "conversation") -> int:
    """A thread from another provider, seeded at the store layer."""
    with get_session() as s:
        t = Thread(name=f"{source}:{title}", title=title, thread_type=thread_type,
                   source=source, source_id=title)
        s.add(t)
        s.flush()
        e = Event(
            thread_id=t.id, stream_id=title, event_type="user_message_sent",
            payload={"content": f"{title} content"},
            occurred_at=datetime(2026, 1, day, 10, 0, 0, tzinfo=timezone.utc),
        )
        s.add(e)
        s.flush()
        index_events(s, [e])
        tid = t.id
        s.commit()
    return tid


def _seed(archive_home) -> dict:
    init_db()
    _import_cc(archive_home, "auth", "how does authentication work", "Auth uses tokens.", 1)
    _import_cc(archive_home, "db", "what database does get_session use", "Postgres.", 2)
    cursor_tid = _seed_direct("cursor", "cursor-session", 3)
    return {"cursor_tid": cursor_tid}


def test_empty_query_browses_threads_recency_first(archive_home) -> None:
    _seed(archive_home)

    rows = search("")
    assert rows, "browse must list threads"
    assert all(r.get("_browse") for r in rows)
    # one row per thread, newest activity first
    assert len({r["thread_id"] for r in rows}) == len(rows) == 3
    dates = [r["occurred_at"] for r in rows]
    assert dates == sorted(dates, reverse=True)
    # the anchor is the thread's newest event
    assert all(r["event_id"] for r in rows)
    assert all(r.get("n_events", 0) >= 1 for r in rows)

    oldest = search("", sort="oldest")
    assert [r["thread_id"] for r in oldest] == [r["thread_id"] for r in rows][::-1]


def test_browse_honors_structural_filters(archive_home) -> None:
    seeded = _seed(archive_home)

    assert [r["thread_id"] for r in search("", source=["cursor"])] == [seeded["cursor_tid"]]
    # the window keeps only the day-3 cursor thread
    late = search("", since="2026-01-02T12:00:00+00:00")
    assert [r["thread_id"] for r in late] == [seeded["cursor_tid"]]
    early = search("", until="2026-01-01T12:00:00+00:00")
    assert seeded["cursor_tid"] not in {r["thread_id"] for r in early}
    assert len(search("", limit=2)) == 2


def test_browse_hides_topics_unless_typed(archive_home) -> None:
    _seed(archive_home)
    ta.open_archive()
    topic_id = create_topic("Retrieval")["topic_id"]

    assert topic_id not in {r["thread_id"] for r in search("")}
    topics = search("", types=["topic"])
    assert [r["thread_id"] for r in topics] == [topic_id]
    assert topics[0]["content_type"] == "topic"


def test_types_filter_scopes_keyword_search(archive_home) -> None:
    _seed(archive_home)

    hits = search("authentication", types=["conversation"])
    assert hits and all(h["content_type"] != "topic" for h in hits)
    assert search("authentication", types=["topic"]) == []


def test_browse_render_and_linkable(archive_home) -> None:
    _seed(archive_home)

    rows = search("")
    text = format_results(rows, "")
    assert "browse (no query)" in text
    assert "thread_read('topics')" in text
    for r in rows:
        assert f"[{r['thread_id']}/{r['event_id']}]" in text

    linkable = json.loads(format_results(rows, "", output="linkable"))
    assert {e["thread_id"] for e in linkable} == {r["thread_id"] for r in rows}

    assert "No threads matched the browse filters" in format_results([], "")


def test_topic_tree_read(archive_home) -> None:
    init_db()
    ta.open_archive()
    root = create_topic("Infrastructure")["topic_id"]
    child = create_topic("Search")["topic_id"]
    grandchild = create_topic("Ranking")["topic_id"]
    lone = create_topic("Unparented")["topic_id"]
    link_threads(child, root, "part-of")
    link_threads(root, grandchild, "contains")  # contains under root, sibling of child

    tree = topic_tree()
    assert tree["topics_total"] == 4
    assert tree["topics_in_hierarchy"] == 3
    assert [r["id"] for r in tree["roots"]] == [root]
    assert {c["id"] for c in tree["roots"][0]["children"]} == {child, grandchild}

    page = read_thread("topics")
    assert "# Topic tree" in page
    assert f"- Infrastructure [topic {root}]" in page
    assert f"  - Search [topic {child}]" in page
    assert "3 of 4 live topics" in page
    assert f"[topic {lone}]" not in page  # unparented topics list via browse, not the tree

    # ref is case/space-insensitive and beats uuid resolution
    assert "# Topic tree" in read_thread(" Topics ")

    # a tiny budget truncates cleanly with a pointer, not mid-line garbage
    tiny = read_thread("topics", max_chars=len("# Topic tree") + 220)
    assert "truncated" in tiny


def test_topic_tree_empty(archive_home) -> None:
    init_db()
    assert "No hierarchy yet" in read_thread("topics")
