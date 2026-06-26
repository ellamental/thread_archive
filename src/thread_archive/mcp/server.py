"""Library-native MCP server.

Exposes ``thread_search`` + ``thread_read`` as MCP tools that call the
:mod:`thread_archive.api` library functions directly — no web framework, no HTTP,
no route layer. The server is library-native: it dispatches straight to the API
functions in-process.

The archive home comes from ``$THREAD_ARCHIVE_HOME`` (set by the MCP client
config), else ``~/.thread_archive``. Run with::

    python -m thread_archive.mcp.server

``import mcp`` below is the external MCP SDK (top-level absolute import); this
package is ``thread_archive.mcp`` and never shadows it.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .. import api
from ..retrieval import format_results

mcp = FastMCP("thread-archive")


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

    Query grammar: natural language, "quoted phrases", boolean AND/OR/NOT,
    pipe-OR (a|b), and code identifiers (get_session, a.b.c). Filter by
    ``thread_id``, ``content_type`` (user/text/thinking/tool/tool_result/...),
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
    content_types = [content_type] if content_type else None
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
    thread_id: int,
    offset: int = 0,
    limit: Optional[int] = None,
    include_thinking: bool = False,
    include_tools: bool = True,
) -> str:
    """Read a conversation thread as a transcript, reconstructed from its events.

    ``offset``/``limit`` paginate over rendered messages. ``include_thinking``
    adds assistant thinking blocks; ``include_tools`` (default on) shows tool
    calls and results.
    """
    return api.read_thread(
        thread_id,
        offset=offset,
        limit=limit,
        include_thinking=include_thinking,
        include_tools=include_tools,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
