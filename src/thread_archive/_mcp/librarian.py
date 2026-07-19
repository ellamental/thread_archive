"""The librarian MCP server — the curatorial *write* surface, kept separate.

A second, deliberately distinct MCP server (``thread-archive-librarian``) carrying the
knowledge-layer write tools + the curation-read tools the librarian needs to decide
what to write, plus the gardener's structural diagnostics (``garden_status`` /
``garden_queue`` / ``communities`` — the read surface the ``/gardener-lite`` skill
drains with the same write tools). The read-only ``thread-archive`` server (``thread_search`` /
``thread_read``) stays read-only: mixing write tools into it would hand every
read-only client (e.g. a reader bot) curation power. This mirrors the read/write split
the upstream operator surface enforces.

Library-native, like the read server: each tool opens the archive and dispatches
straight to :mod:`thread_archive._knowledge.write` in-process — no HTTP, no route layer.
The archive home comes from ``$THREAD_ARCHIVE_HOME`` (else ``~/.thread/archive``).
Run with::

    python -m thread_archive._mcp.librarian

Every graph write is event-sourced (an append-only ``KgEvent``) and idempotent at the
projection layer, so a re-run never double-links or double-cites. The one non-graph
write, ``thread_set_summary``, is thread metadata carried by the thread's own truth
record — idempotent the same way (overwrite, not append).
"""

from __future__ import annotations

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .. import _api as api
from .. import _knowledge
from .._knowledge import read as _read
from .._knowledge import write as _write

mcp = FastMCP("thread-archive-librarian")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _resolve_ref(ref: int | str) -> Optional[str]:
    """Resolve a thread/topic ref to the archive's ULID thread id: a ULID
    resolves as the primary key, an all-digit ref via the permanent
    ``legacy_id`` alias, anything else as a provider session id. None when
    nothing matches. Callers must have opened the archive."""
    from .._retrieval.read import resolve_thread_ref
    from .._store import get_session

    with get_session() as s:
        return resolve_thread_ref(s, ref)


# ── curation-read ────────────────────────────────────────────────────────────
@mcp.tool()
def review_queue(limit: int = 20, exclude_source_id: Optional[str] = None) -> str:
    """Conversation threads with librarian work left (the backlog), newest first.

    Event-bearing conversations still missing either half of the librarian's output —
    a thread leaves the queue once it has BOTH its first topic citation/link AND a
    stored summary. Threads that ingested events within the last hour are held back
    (a live session's curation would be premature). Pass your own session's
    ``source_id`` as ``exclude_source_id`` to drop your still-growing transcript from the
    queue. Returns a JSON list of ``{id, title, source_id}``."""
    api.open_archive()
    return _dump(_write.review_queue(limit=limit, exclude_source_id=exclude_source_id))


@mcp.tool()
def topic_search(query: str, limit: int = 10) -> str:
    """Find existing live topics by title substring — search before creating duplicates.
    Returns a JSON list of ``{topic_id, title}``."""
    api.open_archive()
    return _dump(_write.topic_search(query, limit=limit))


@mcp.tool()
def topic_get(topic_id: int | str) -> str:
    """Read one topic back out of the graph: metadata, links (both directions),
    citation + member-thread counts, graph metadata (community, pagerank), and
    community peers. The JSON twin of a topic page — use ``topic_members`` for the
    citations themselves. Id params here and across the librarian tools take a
    ULID thread/topic id or a legacy integer alias."""
    api.open_archive()
    tid = _resolve_ref(topic_id)
    if tid is None:
        return f"Error: Topic {topic_id} not found"
    try:
        return _dump(_read.topic_get(tid))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_members(topic_id: int | str, limit: int = 200) -> str:
    """A topic's live citations, oldest first — JSON ``[{event_id, thread_id,
    thread_title, quote}]``. Each ``event_id`` opens in ``thread_read`` via
    ``around_event``."""
    api.open_archive()
    tid = _resolve_ref(topic_id)
    if tid is None:
        return f"Error: Topic {topic_id} not found"
    try:
        return _dump(_read.topic_members(tid, limit=limit))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def thread_user_messages(thread_id: int | str, limit: Optional[int] = None) -> str:
    """A thread's user messages as ``[{event_id, text}]`` — the cheap, high-signal read
    to cite from (cite the ``event_id`` values)."""
    api.open_archive()
    tid = _resolve_ref(thread_id)
    if tid is None:
        return f"Error: Thread {thread_id} not found"
    return _dump(_write.thread_user_messages(tid, limit=limit))


# ── gardener diagnostics ──────────────────────────────────────────────────────
@mcp.tool()
def garden_status() -> str:
    """The gardener's dashboard: per-kind structural-issue counts over the live
    topic graph (singletons, uncited, unparented, near-duplicate title pairs),
    hierarchy coverage, and graph status. Pull the lists with ``garden_queue``."""
    api.open_archive()
    return _dump(_knowledge.garden_status())


@mcp.tool()
def garden_queue(kind: str, limit: int = 20) -> str:
    """One kind's prioritized structural-issue list. Kinds: ``singleton`` (topics
    with no link to another live topic, most-cited first), ``uncited`` (topics
    with no live citation, least-linked first — archive candidates), ``unparented``
    (linked topics outside the part-of/contains hierarchy, highest-pagerank
    first), ``dupes`` (near-duplicate title pairs, best score first). State is the
    data: fixing an issue removes it from the queue."""
    api.open_archive()
    try:
        return _dump(_knowledge.garden_queue(kind, limit=limit))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def communities(limit: int = 30, member_limit: int = 8) -> str:
    """The graph's community clusters, largest first, each with its
    highest-pagerank members — the map for promoting a cluster into a hierarchy
    subtree (a parent topic + ``part-of`` children)."""
    api.open_archive()
    return _dump(_knowledge.get_communities(limit=limit, member_limit=member_limit))


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
def topic_rename(topic_id: int | str, title: str, description: Optional[str] = None) -> str:
    """Rename (and optionally re-describe) a topic."""
    api.open_archive()
    tid = _resolve_ref(topic_id)
    if tid is None:
        return f"Error: Topic {topic_id} not found"
    try:
        return _dump(_write.rename_topic(tid, title, description=description))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_archive(topic_id: int | str) -> str:
    """Archive a topic — it drops out of the live graph (the thread stays reachable)."""
    api.open_archive()
    tid = _resolve_ref(topic_id)
    if tid is None:
        return f"Error: Topic {topic_id} not found"
    try:
        return _dump(_write.archive_topic(tid))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_merge(from_id: int | str, into_id: int | str) -> str:
    """Merge ``from_id`` into ``into_id``: repoint its links + citations, archive it."""
    api.open_archive()
    src = _resolve_ref(from_id)
    if src is None:
        return f"Error: Topic {from_id} not found"
    dst = _resolve_ref(into_id)
    if dst is None:
        return f"Error: Topic {into_id} not found"
    try:
        return _dump(_write.merge_topics(src, dst))
    except ValueError as e:
        return f"Error: {e}"


# ── links + citations ─────────────────────────────────────────────────────────
@mcp.tool()
def topic_link(
    source_id: int | str, target_id: int | str, link_type: str = "related",
    strength: float = 1.0, evidence: Optional[str] = None,
) -> str:
    """Link two threads/topics (idempotent on source+target+link_type). ``link_type`` is
    e.g. related / implements / example-of / contrast / supersedes / works_on."""
    api.open_archive()
    src = _resolve_ref(source_id)
    if src is None:
        return f"Error: Thread/topic {source_id} not found"
    dst = _resolve_ref(target_id)
    if dst is None:
        return f"Error: Thread/topic {target_id} not found"
    try:
        return _dump(_write.link_threads(
            src, dst, link_type, strength=strength, evidence=evidence))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_unlink(source_id: int | str, target_id: int | str, link_type: str = "related") -> str:
    """Remove a link (a tombstone event — recorded, not erased from history)."""
    api.open_archive()
    src = _resolve_ref(source_id)
    if src is None:
        return f"Error: Thread/topic {source_id} not found"
    dst = _resolve_ref(target_id)
    if dst is None:
        return f"Error: Thread/topic {target_id} not found"
    return _dump(_write.unlink_threads(src, dst, link_type))


@mcp.tool()
def topic_cite(topic_id: int | str, event_id: int, thread_id: int | str, quote: str) -> str:
    """Cite a conversation message (``event_id`` in ``thread_id``) as evidence for a
    topic. Idempotent on (topic_id, event_id)."""
    api.open_archive()
    top = _resolve_ref(topic_id)
    if top is None:
        return f"Error: Topic {topic_id} not found"
    thr = _resolve_ref(thread_id)
    if thr is None:
        return f"Error: Thread {thread_id} not found"
    try:
        return _dump(_write.add_topic_evidence(top, event_id, thr, quote))
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
def topic_uncite(topic_id: int | str, event_id: int) -> str:
    """Archive a citation (tombstone — sets archived_at, keeps the row + history)."""
    api.open_archive()
    tid = _resolve_ref(topic_id)
    if tid is None:
        return f"Error: Topic {topic_id} not found"
    return _dump(_write.archive_topic_evidence(tid, event_id))


# ── stored summaries ──────────────────────────────────────────────────────────
@mcp.tool()
def thread_set_summary(
    thread_id: int | str, summary: Optional[str] = None, indexed_summary: Optional[str] = None,
) -> str:
    """Store a conversation thread's summary — the second half of the librarian's
    per-thread commit (citations are the first).

    ``summary`` is the short one: a few dense, specific sentences that become a
    search doc (lexical + semantic) in the default search scope — pack it with the
    distinctive vocabulary someone would search for (system names, decisions, errors,
    outcomes). ``indexed_summary`` is the structured markdown map for long threads —
    ``## section (event NNNN)`` headings anchored to real event ids — served by
    ``thread_read summary='indexed'``. Pass either or both; a passed field
    overwrites the stored one (re-summarizing is an update). Topics are refused
    (their description is their summary surface — ``topic_rename``)."""
    api.open_archive()
    tid = _resolve_ref(thread_id)
    if tid is None:
        return f"Error: Thread {thread_id} not found"
    try:
        return _dump(_write.set_thread_summary(
            tid, summary, indexed_summary=indexed_summary))
    except ValueError as e:
        return f"Error: {e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
