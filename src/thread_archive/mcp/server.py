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
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> str:
    """Search the local conversation archive (lexical FTS5).

    Query grammar: natural language, "quoted phrases", boolean AND/OR/NOT,
    pipe-OR (a|b), and code identifiers (get_session, a.b.c). Filter by
    ``thread_id``, ``content_type`` (user/text/thinking/tool/tool_result/...),
    ``tool_name``, and a ``since``/``until`` window (ISO timestamp or '7d').
    """
    content_types = [content_type] if content_type else None
    hits = api.search(
        query,
        limit=limit,
        thread_id=thread_id,
        content_types=content_types,
        since=since,
        until=until,
        tool_name=tool_name,
    )
    return format_results(hits, query)


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
