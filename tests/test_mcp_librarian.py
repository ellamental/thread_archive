"""The librarian MCP server — the curatorial write surface's tool wiring.

The knowledge-write functions themselves are covered by ``test_kg_write`` /
``test_librarian_gate``; these tests pin the *server* layer: the registered tool
set, the JSON-string in/out contract, and the Error-string (not raise) contract
on bad curation input.
"""

from __future__ import annotations

import asyncio
import json

from thread_archive._mcp import librarian as L

from .helpers import import_cc_session


def test_librarian_registers_the_write_surface() -> None:
    tools = asyncio.run(L.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "review_queue", "topic_search", "topic_get", "topic_members",
        "thread_user_messages",
        "topic_create", "topic_rename", "topic_archive", "topic_merge",
        "topic_link", "topic_unlink", "topic_cite", "topic_uncite",
        "thread_set_summary",
    }
    assert all(t.description for t in tools)


def test_librarian_curation_flow(archive_home, tmp_path) -> None:
    """The whole curate loop over the tool layer: queue → read → topic → cite →
    summarize → the thread leaves the queue."""
    from sqlalchemy import text as sa_text

    from thread_archive._store import get_session

    import_cc_session(tmp_path)
    # A just-imported thread sits inside the queue's quiet window (recorded_at =
    # import time); backdate the ingest stamps — the tool doesn't expose the knob.
    with get_session() as s:
        s.execute(sa_text("UPDATE events SET recorded_at = '2026-01-01 12:00:00'"))
        s.commit()

    queue = json.loads(L.review_queue())
    assert len(queue) == 1
    assert {"id", "title", "source_id"} <= set(queue[0])
    tid = queue[0]["id"]

    msgs = json.loads(L.thread_user_messages(tid))
    assert msgs and msgs[0]["text"]

    topic = json.loads(L.topic_create("Curation Topic"))["topic_id"]
    hits = json.loads(L.topic_search("Curation"))
    assert [h["topic_id"] for h in hits] == [topic]

    cite = json.loads(L.topic_cite(topic, msgs[0]["event_id"], tid, "a quote"))
    assert cite
    # Cited but not yet summarized — the thread stays queued.
    assert any(row["id"] == tid for row in json.loads(L.review_queue()))

    r = json.loads(L.thread_set_summary(tid, summary="a dense stored summary"))
    assert r == {"thread_id": tid, "fields": ["summary"]}
    # Both halves done — it leaves the queue.
    assert all(row["id"] != tid for row in json.loads(L.review_queue()))

    # Link + tombstones round-trip through the string layer.
    assert json.loads(L.topic_link(topic, tid))
    assert json.loads(L.topic_unlink(topic, tid))
    assert json.loads(L.topic_uncite(topic, msgs[0]["event_id"]))


def test_librarian_error_contract_is_a_string_not_a_raise(archive_home) -> None:
    """Curation mistakes come back as ``Error: …`` strings the calling agent can
    read, never as exceptions that kill the MCP call."""
    topic = json.loads(L.topic_create("Solo"))["topic_id"]

    assert L.topic_create("Solo").startswith("Error:")  # duplicate title
    assert L.topic_rename(99_999_999, "x").startswith("Error:")
    assert L.topic_archive(99_999_999).startswith("Error:")
    assert L.topic_merge(topic, 99_999_999).startswith("Error:")
    # Link/cite validate their endpoints and must honor the same string contract.
    assert L.topic_link(99_999_999, 88_888_888).startswith("Error:")  # endpoints must exist
    assert L.topic_cite(topic, 99_999_999, 88_888_888, "q").startswith("Error:")  # event must exist
    # Summary writes too: missing thread, topic target, nothing to set.
    assert L.thread_set_summary(99_999_999, "s").startswith("Error:")
    assert L.thread_set_summary(topic, "s").startswith("Error:")
    assert L.thread_set_summary(99_999_999).startswith("Error:")


def test_librarian_rename_and_archive(archive_home) -> None:
    topic = json.loads(L.topic_create("Old Title"))["topic_id"]
    json.loads(L.topic_rename(topic, "New Title"))
    assert json.loads(L.topic_search("New Title"))

    json.loads(L.topic_archive(topic))
    assert json.loads(L.topic_search("New Title")) == []  # archived → out of the live graph
