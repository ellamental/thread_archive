"""Library-native MCP server.

Exposes ``thread_search`` + ``thread_read`` as MCP tools that call the
:mod:`thread_archive.api` library functions directly — no web framework, no HTTP,
no route layer. The server is library-native: it dispatches straight to the API
functions in-process.

The archive home comes from ``$THREAD_ARCHIVE_HOME`` (set by the MCP client
config), else ``~/.thread/archive``. Run with::

    python -m thread_archive.mcp.server

``import mcp`` below is the external MCP SDK (top-level absolute import); this
package is ``thread_archive.mcp`` and never shadows it.
"""

from __future__ import annotations

import threading
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .. import api
from ..retrieval import format_results, warm_models

mcp = FastMCP("thread-archive")

# The agent-facing default search scope: USER messages plus the thread-meta docs
# (title + stored summary) — the intentional signals of what a thread was about.
# Assistant text, tool calls/results, and thinking are opt-in (pass an explicit
# content_type), and content_type='all' clears the filter to search everything.
# Mirrors the archive backend's thread_search default.
DEFAULT_SEARCH_CONTENT_TYPES = ("user", "title", "summary")


@mcp.tool()
def thread_search(
    query: str,
    limit: int = 10,
    thread_id: Optional[int] = None,
    content_type: Optional[str] = None,
    exclude_content_type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[str] = None,
    startswith: Optional[str] = None,
    sort: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
) -> str:
    """Search the local conversation archive (federated: lexical FTS5 + optional
    semantic vectors → fusion → rank → optional cross-encoder re-rank).

    Read the match signal before trusting a result: the header carries
    ``quality=strong|partial|weak|semantic`` for the top hit, and each hit shows
    ``K/N`` (how many query terms landed) — a weak / 0-of-N result means these are
    nearest-neighbour guesses and the log likely lacks it, so rephrase or switch
    store rather than piling on synonyms.

    By default USER messages, thread titles, and stored thread summaries are
    searched — the strongest signals of what a thread was about. Assistant text,
    tool calls/results, and thinking are opt-in: pass ``content_type='all'`` to
    search everything, or a specific ``content_type``
    (text/thinking/tool/tool_result/...) to target one.

    Query grammar: natural language, "quoted phrases", boolean AND/OR/NOT,
    pipe-OR (a|b), and code identifiers (get_session, a.b.c). Filter by
    ``thread_id``, ``content_type`` (default user+title+summary; 'all' searches
    everything),
    ``exclude_content_type`` (comma-separated types to drop), ``tool_name``,
    ``source`` (comma-separated providers, e.g. 'claude-code,cursor'), and a
    ``since``/``until`` window (ISO timestamp or '7d').

    ``startswith`` does a structural prefix scan (content LIKE 'prefix%'; query text
    unused). ``sort='oldest'`` returns matches chronologically (find when something
    was first discussed) instead of the default recency-biased ranking.
    ``context_lines`` (default 2; set 0 for the raw FTS snippet) replaces each
    snippet with a numbered ±N-line window around the match; ``context_events``
    ('N' / 'before:after' /
    'before:after:types', e.g. '2' or '0:1:user') appends the neighbouring events.
    ``output='count'`` returns a per-thread tally (no snippets); ``output='linkable'``
    returns JSON of event/thread ids. ``rerank`` forces the cross-encoder head
    re-rank on/off (else auto-gated to conceptual queries when the ``[embeddings]``
    extra is installed).
    """
    # Default scope is user messages only; an explicit type targets it, and
    # content_type='all' clears the filter to search everything (see the constant).
    if content_type == "all":
        content_types = None
    elif content_type:
        content_types = [content_type]
    else:
        content_types = list(DEFAULT_SEARCH_CONTENT_TYPES)
    exclude = [c.strip() for c in exclude_content_type.split(",") if c.strip()] if exclude_content_type else None
    sources = [s.strip() for s in source.split(",") if s.strip()] if source else None
    hits = api.search(
        query,
        limit=limit,
        thread_id=thread_id,
        content_types=content_types,
        exclude_content_types=exclude,
        since=since,
        until=until,
        tool_name=tool_name,
        source=sources,
        startswith=startswith,
        sort=sort,
        output=output,
        context_lines=context_lines,
        context_events=context_events,
        rerank=rerank,
    )
    return format_results(hits, query, output=output)


@mcp.tool()
def thread_read(
    thread_id: int | str,
    limit: int = 200,
    offset: int = 0,
    summary: bool | str = False,
    mode: Optional[str] = None,
    user_only: Optional[bool] = None,
    tool_results: bool = False,
    max_chars: int = 0,
    after_event: Optional[int] = None,
) -> str:
    """Read a thread's conversation, reconstructed from the event log.

    ``thread_id`` accepts either the archive's own integer thread id OR a provider
    **session uuid** (the id a tool like claude-code / cursor / codex knows the
    conversation by — its ``source_id``). The uuid is resolved to the thread
    automatically (newest match wins), so you can pass a session uuid straight
    through without looking the integer id up first.

    ``mode`` picks the view: 'user' (default) = only the USER messages — the real
    signal of what a thread was about and what was wanted, far cheaper than the
    transcript (for research / 'what was this thread about' that IS what you want);
    'chat' = the readable conversation — user turns + the assistant's reasoning/text
    with tool calls stripped out (use when you need what was *decided/concluded/built*,
    which lives in assistant text); 'full' = the whole transcript including every
    tool call (bulky, mostly tool noise — only when you need what the assistant *did*).
    Tool *output* is off by default; set ``tool_results=true`` (only meaningful with
    'full', where the calls are shown) to fold each tool's result under its call.

    The read is size-budgeted (~48k chars), so it never silently overflows the MCP
    output cap: a thread bigger than one chunk ends in a CHUNKED footer naming the
    exact offset to read next (that's pagination, not lost data — page with
    ``offset``, or resume from an event with ``after_event``). ``summary`` picks a
    summary view instead of the transcript: ``true``/``'toc'`` = a compact per-message
    TOC; ``'short'`` = the thread's stored short summary (a few sentences);
    ``'indexed'`` = the stored indexed summary (structured, with event anchors) —
    the stored kinds exist only where the summarizer has covered the thread.
    ``user_only`` is a back-compat alias for ``mode`` (true→user, false→full);
    prefer ``mode``, which wins if both are set.

    Args:
        thread_id: Integer thread id, or a provider session uuid (source_id) which
            is resolved to the thread automatically.
        limit: Max turns per chunk (safety cap; the char budget usually bites first).
            Default: 200.
        offset: Skip first N turns. Use the offset from a CHUNKED footer to read the
            next chunk. Negative counts from end: -20 = last 20 turns. Default: 0.
        summary: Summary view instead of full content — true/'toc' for a compact
            TOC with previews, 'short' or 'indexed' for the stored thread summary.
        mode: View — 'user' (default), 'chat', or 'full'. Default: user.
        user_only: Back-compat alias for mode (true→user, false→full). Prefer mode.
        tool_results: Include tool output under each call (default off; needs 'full').
        max_chars: Per-chunk character budget; the read stops at a clean turn
            boundary once hit and the footer points at the next offset. Default: ~48k.
        after_event: Resume reading from the turn AFTER this event id (overrides
            offset). Robust way to continue from where a previous read stopped.
    """
    return api.read_thread(
        thread_id,
        limit=limit,
        offset=offset,
        summary=summary,
        mode=mode,
        user_only=user_only,
        tool_results=tool_results,
        max_chars=max_chars,
        after_event=after_event,
    )


def main() -> None:
    # Warm the embedding + cross-encoder models on a background daemon thread. The cold load
    # is tens of seconds; when it lands inside the first conceptual search it can exceed the
    # client's MCP request timeout (cloth defaults to 60s), which surfaces to the model as a
    # failed tool call. Warming at startup moves that cost off the request path — the models
    # are (usually) resident by the time the first query arrives, and _load()'s lock makes an
    # early query that races the warm wait on one load rather than kick off a second. Daemon
    # so it never holds up interpreter exit; warm_models is fail-soft (a missing extra / load
    # failure just restores the old lazy behaviour).
    threading.Thread(target=warm_models, name="archive-warm-models", daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
