"""The knowledge-graph read surface: getting curated topics back OUT.

``_knowledge.read`` is the library layer (topic_get / topic_members /
topic_thread_ids); on top of it sit the librarian MCP tools of the same names, the
real topic render in ``thread_read``, and search's ``topic_id`` scope. These tests
pin all four: the detail/citation shapes, the JSON tool contract, the rendered topic
page, and that a topic-scoped search only surfaces the topic's member conversations.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from thread_archive import _api as ta
from thread_archive._knowledge import (
    topic_get,
    topic_members,
    topic_thread_ids,
    topic_tree,
)

pytest.importorskip("thread_librarian")  # seeds the curated data plane
from thread_librarian import add_topic_evidence, create_topic, link_threads  # noqa: E402

from thread_archive._retrieval import index_events, read_thread, search
from thread_archive._store import Event, Thread, get_session

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed_conversation(source_id: str, content: str) -> tuple[int, int]:
    """A conversation thread with one user message; returns (thread_id, event_id)."""
    with get_session() as s:
        t = Thread(
            name=f"claude-code:{source_id}", title=source_id, thread_type="conversation",
            source="claude-code", source_id=source_id,
        )
        s.add(t)
        s.flush()
        e = Event(
            thread_id=t.id, stream_id=source_id, event_type="user_message_sent",
            payload={"content": content}, occurred_at=NOW,
        )
        s.add(e)
        s.flush()
        index_events(s, [e])  # the import seam would do this; direct seeding must too
        ids = (t.id, e.id)
        s.commit()
    return ids


def _seed_topic_with_evidence():
    """A topic citing two conversations, linked to a third topic and a conversation."""
    ta.open_archive()
    conv_a, ev_a = _seed_conversation("sess-a", "the retrieval pipeline uses rrf fusion")
    conv_b, ev_b = _seed_conversation("sess-b", "vectors arm degrades to lexical search")
    conv_c, _ = _seed_conversation("sess-c", "an unrelated conversation about lunch")
    topic = create_topic("Retrieval", "how search works")["topic_id"]
    peer = create_topic("Indexing")["topic_id"]
    add_topic_evidence(topic, ev_a, conv_a, "rrf fusion is the merge step")
    add_topic_evidence(topic, ev_b, conv_b, "the vector arm is optional")
    link_threads(topic, peer, "related")
    link_threads(conv_c, topic, "works_on")  # inbound link from a conversation
    return topic, peer, conv_a, conv_b, conv_c, ev_a, ev_b


# ── library layer ─────────────────────────────────────────────────────────────
def test_topic_get_returns_the_whole_page(archive_home) -> None:
    topic, peer, conv_a, conv_b, conv_c, *_ = _seed_topic_with_evidence()

    d = topic_get(topic)
    assert d["id"] == topic and d["title"] == "Retrieval"
    assert d["description"] == "how search works"
    assert d["citation_count"] == 2
    assert {m["thread_id"] for m in d["member_threads"]} == {conv_a, conv_b}
    assert all(m["citations"] == 1 for m in d["member_threads"])
    link_others = {(lk["direction"], lk["other_id"]) for lk in d["links"]}
    assert ("out", peer) in link_others and ("in", conv_c) in link_others


def test_topic_get_rejects_non_topics(archive_home) -> None:
    ta.open_archive()
    conv, _ = _seed_conversation("sess-x", "hi")
    with pytest.raises(ValueError):
        topic_get(conv)
    with pytest.raises(ValueError):
        topic_get(99_999_999)


def test_topic_members_returns_quotes_with_anchors(archive_home) -> None:
    topic, _, conv_a, conv_b, _, ev_a, ev_b = _seed_topic_with_evidence()

    members = topic_members(topic)
    assert [(m["event_id"], m["thread_id"]) for m in members] == [
        (ev_a, conv_a), (ev_b, conv_b)]
    assert members[0]["quote"] == "rrf fusion is the merge step"
    assert members[0]["thread_title"] == "sess-a"
    assert len(topic_members(topic, limit=1)) == 1


def test_topic_thread_ids_covers_cited_and_linked(archive_home) -> None:
    topic, peer, conv_a, conv_b, conv_c, *_ = _seed_topic_with_evidence()

    # Cited threads + the linked conversation; the linked *topic* is not a member.
    assert topic_thread_ids(topic) == sorted([conv_a, conv_b, conv_c])
    assert topic_thread_ids(peer) == []


# ── thread_read renders a topic as its curated page ───────────────────────────
def test_thread_read_on_a_topic_renders_citations(archive_home) -> None:
    topic, _, conv_a, _, _, ev_a, _ = _seed_topic_with_evidence()

    out = read_thread(topic)
    assert f"# Topic {topic}: Retrieval" in out
    assert "how search works" in out
    assert "Citations (2)" in out
    assert f"[event:{ev_a}] rrf fusion is the merge step" in out
    assert f"Thread {conv_a}: sess-a" in out
    assert "around_event" in out  # the how-to-open footer


def test_thread_read_on_an_empty_topic_says_so(archive_home) -> None:
    ta.open_archive()
    topic = create_topic("Bare")["topic_id"]
    out = read_thread(topic)
    assert "No live citations yet." in out


# ── search topic scope ────────────────────────────────────────────────────────
def test_search_topic_scope_restricts_to_members(archive_home) -> None:
    topic, _, conv_a, conv_b, conv_c, *_ = _seed_topic_with_evidence()

    assert search("fusion")  # sanity: the corpus is searchable at all

    scoped = search("fusion", topic_id=topic)
    assert scoped and {h["thread_id"] for h in scoped} <= {conv_a, conv_b, conv_c}
    lunch = search("lunch", topic_id=topic)
    # conv_c is a member (linked), so its content is in scope…
    assert {h["thread_id"] for h in lunch} <= {conv_c}

    # …but a thread outside the topic never surfaces, even on a matching term.
    outsider, _ = _seed_conversation("sess-out", "fusion cuisine restaurant")
    scoped2 = search("fusion", topic_id=topic)
    assert outsider not in {h["thread_id"] for h in scoped2}


def test_search_topic_scope_empty_or_bogus_matches_nothing(archive_home) -> None:
    ta.open_archive()
    _seed_conversation("sess-y", "fusion everywhere")
    empty = create_topic("Empty")["topic_id"]
    assert search("fusion", topic_id=empty) == []
    assert search("fusion", topic_id=99_999_999) == []


# ── librarian MCP tool wiring ─────────────────────────────────────────────────
def test_librarian_topic_get_and_members_tools(archive_home) -> None:
    from thread_librarian import mcp_server as L

    topic, _, conv_a, _, _, ev_a, _ = _seed_topic_with_evidence()

    d = json.loads(L.topic_get(topic))
    assert d["id"] == topic and d["citation_count"] == 2

    members = json.loads(L.topic_members(topic))
    assert members[0] == {
        "event_id": ev_a, "thread_id": conv_a,
        "thread_title": "sess-a", "quote": "rrf fusion is the merge step",
    }

    assert L.topic_get(99_999_999).startswith("Error:")
    assert L.topic_members(99_999_999).startswith("Error:")


# ── the derived hierarchy (topic_tree) ────────────────────────────────────────
def _tree_ids(node: dict) -> set:
    return {node["id"], *(i for c in node["children"] for i in _tree_ids(c))}


def test_topic_tree_from_part_of_and_contains(archive_home) -> None:
    """Both hierarchy spellings build the same forest; conversation edges and
    hierarchy edges to conversations never enter the tree."""
    ta.open_archive()
    conv, _ = _seed_conversation("sess-t", "hello tree")
    a = create_topic("Graph Theory")["topic_id"]
    b = create_topic("Leiden Communities")["topic_id"]
    c = create_topic("PageRank")["topic_id"]
    d = create_topic("Centrality Measures")["topic_id"]
    link_threads(b, a, "part-of")
    link_threads(a, c, "contains")
    link_threads(d, c, "part-of")
    link_threads(conv, a, "part-of")  # a conversation may not enter the tree

    tree = topic_tree()
    [root] = tree["roots"]
    assert root["id"] == a and _tree_ids(root) == {a, b, c, d}
    by_title = {n["title"]: n for n in root["children"]}
    assert set(by_title) == {"Leiden Communities", "PageRank"}
    assert [n["id"] for n in by_title["PageRank"]["children"]] == [d]
    assert tree["topics_in_hierarchy"] == 4 and tree["topics_total"] == 4


def test_topic_tree_cycle_is_cut(archive_home) -> None:
    """A mutual part-of pair: neither is a root, but the build must not hang or
    recurse forever — the pair simply contributes no root."""
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]
    link_threads(a, b, "part-of")
    link_threads(b, a, "part-of")
    tree = topic_tree()
    assert all(a not in _tree_ids(r) for r in tree["roots"])


def test_topic_tree_multi_parent_child_appears_under_each(archive_home) -> None:
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]
    c = create_topic("C")["topic_id"]
    d = create_topic("Shared Child")["topic_id"]
    link_threads(d, a, "part-of")
    link_threads(d, b, "part-of")
    link_threads(c, a, "part-of")  # give a more weight so root order is deterministic
    tree = topic_tree()
    roots = {r["id"]: r for r in tree["roots"]}
    assert set(roots) == {a, b}
    assert d in _tree_ids(roots[a]) and d in _tree_ids(roots[b])
    # heavier subtree lists first
    assert tree["roots"][0]["id"] == a


def test_topic_tree_empty_without_hierarchy_links(archive_home) -> None:
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]
    link_threads(a, b, "related")  # not a hierarchy spelling
    tree = topic_tree()
    assert tree["roots"] == [] and tree["topics_in_hierarchy"] == 0
    assert tree["topics_total"] == 2
