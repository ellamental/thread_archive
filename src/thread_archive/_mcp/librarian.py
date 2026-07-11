"""The librarian MCP server — the curatorial *write* surface, kept separate.

A second, deliberately distinct MCP server (``thread-archive-librarian``) carrying the
knowledge-layer write tools + the curation-read tools the librarian needs to decide
what to write. The read-only ``thread-archive`` server (``thread_search`` /
``thread_read``) stays read-only: mixing write tools into it would hand every
read-only client (e.g. a reader bot) curation power. This mirrors the read/write split
the upstream operator surface enforces.

Library-native, like the read server: each tool opens the archive and dispatches
straight to :mod:`thread_archive._knowledge.write` in-process — no HTTP, no route layer.
The archive home comes from ``$THREAD_ARCHIVE_HOME`` (else ``~/.thread/archive``).
Run with::

    python -m thread_archive._mcp.librarian

Every write is event-sourced (an append-only ``KgEvent``) and idempotent at the
projection layer, so a re-run never double-links or double-cites.
"""

from __future__ import annotations

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .. import _api as api
from .._knowledge import write as _write

mcp = FastMCP("thread-archive-librarian")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# ── curation-read ────────────────────────────────────────────────────────────
@mcp.tool()
def review_queue(limit: int = 20, exclude_source_id: Optional[str] = None) -> str:
    """Unreviewed conversation threads (the librarian backlog), newest first.

    Event-bearing conversations the librarian hasn't curated yet — a thread leaves the
    queue once it gains its first topic citation/link. Pass your own session's
    ``source_id`` as ``exclude_source_id`` to drop your still-growing transcript from the
    queue. In a parallel backfill (when ``$THREAD_ARCHIVE_LIBRARIAN_WORKER`` is set) this
    transparently claims its batch under a lease, so concurrent workers don't overlap —
    you call it the same way regardless. Returns a JSON list of ``{id, title, source_id}``."""
    api.open_archive()
    return _dump(_write.review_queue(limit=limit, exclude_source_id=exclude_source_id))


@mcp.tool()
def topic_search(query: str, limit: int = 10) -> str:
    """Find existing live topics by title substring — search before creating duplicates.
    Returns a JSON list of ``{topic_id, title}``."""
    api.open_archive()
    return _dump(_write.topic_search(query, limit=limit))


@mcp.tool()
def thread_user_messages(thread_id: int, limit: Optional[int] = None) -> str:
    """A thread's user messages as ``[{event_id, text}]`` — the cheap, high-signal read
    to cite from (cite the ``event_id`` values)."""
    api.open_archive()
    return _dump(_write.thread_user_messages(thread_id, limit=limit))


# ── topics ───────────────────────────────────────────────────────────────────
@mcp.tool()
def topic_create(title: str, description: Optional[str] = None) -> str:
    """Create a topic. Errors if one with that title exists — ``topic_search`` first and
    link to the existing topic rather than duplicating."""
    api.open_archive()
    try:
        return _dump(_write.create_topic(title, description))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_rename(topic_id: int, title: str, description: Optional[str] = None) -> str:
    """Rename (and optionally re-describe) a topic."""
    api.open_archive()
    try:
        return _dump(_write.rename_topic(topic_id, title, description=description))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_archive(topic_id: int) -> str:
    """Archive a topic — it drops out of the live graph (the thread stays reachable)."""
    api.open_archive()
    try:
        return _dump(_write.archive_topic(topic_id))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_merge(from_id: int, into_id: int) -> str:
    """Merge ``from_id`` into ``into_id``: repoint its links + citations, archive it."""
    api.open_archive()
    try:
        return _dump(_write.merge_topics(from_id, into_id))
    except ValueError as e:
        return f"Error: {e}"


# ── links + citations ─────────────────────────────────────────────────────────
@mcp.tool()
def topic_link(
    source_id: int, target_id: int, link_type: str = "related",
    strength: float = 1.0, evidence: Optional[str] = None,
) -> str:
    """Link two threads/topics (idempotent on source+target+link_type). ``link_type`` is
    e.g. related / implements / example-of / contrast / supersedes / works_on."""
    api.open_archive()
    return _dump(_write.link_threads(
        source_id, target_id, link_type, strength=strength, evidence=evidence))


@mcp.tool()
def topic_unlink(source_id: int, target_id: int, link_type: str = "related") -> str:
    """Remove a link (a tombstone event — recorded, not erased from history)."""
    api.open_archive()
    return _dump(_write.unlink_threads(source_id, target_id, link_type))


@mcp.tool()
def topic_cite(topic_id: int, event_id: int, thread_id: int, quote: str) -> str:
    """Cite a conversation message (``event_id`` in ``thread_id``) as evidence for a
    topic. Idempotent on (topic_id, event_id)."""
    api.open_archive()
    return _dump(_write.add_topic_evidence(topic_id, event_id, thread_id, quote))


@mcp.tool()
def topic_uncite(topic_id: int, event_id: int) -> str:
    """Archive a citation (tombstone — sets archived_at, keeps the row + history)."""
    api.open_archive()
    return _dump(_write.archive_topic_evidence(topic_id, event_id))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
