"""The library-native MCP server exposes thread_search + thread_read."""

from __future__ import annotations

import asyncio
import json

from thread_archive import _api as ta
from thread_archive._mcp.server import mcp, thread_read, thread_search

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello mcp"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from mcp"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_mcp_registers_two_tools() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {"thread_search", "thread_read"}
    # the schema is derived from the typed signature
    by_name = {t.name: t for t in tools}
    search_props = by_name["thread_search"].inputSchema["properties"]
    assert "query" in search_props
    # the filters + shaping args wired through from the library layer are exposed
    assert {"exclude_content_type", "source", "rerank", "startswith", "sort",
            "output", "context_lines", "context_events"} <= set(search_props)
    read_props = by_name["thread_read"].inputSchema["properties"]
    assert "thread_id" in read_props
    # summary is bool | str: true/'toc' = TOC, 'short'/'indexed' = stored summaries
    summary_types = {v["type"] for v in read_props["summary"]["anyOf"]}
    assert summary_types == {"boolean", "string"}
    # both tools carry a description (docstring)
    assert all(t.description for t in tools)


def test_mcp_tools_query_the_archive(archive_home) -> None:
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    out = thread_search("hello", limit=5)
    assert "hello mcp" in out

    thread_id = ta.search("hello")[0]["thread_id"]
    transcript = thread_read(thread_id)
    assert "[USER" in transcript and "hello mcp" in transcript


def test_mcp_search_new_filters(archive_home) -> None:
    """exclude_content_type / source / rerank dispatch through the MCP wrapper."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)  # imports as source='claude-code'

    # source matches → hit present; comma-separated string splits to a list
    assert "hello mcp" in thread_search("hello", source="claude-code,cursor")
    # source nobody has → no hit
    assert "hello mcp" not in thread_search("hello", source="chatgpt")
    # excluding the user content type drops the user-message hit
    assert "hello mcp" not in thread_search("hello", exclude_content_type="user")
    # rerank=False is accepted (cross-encoder forced off) and still searches
    assert "hello mcp" in thread_search("hello", rerank=False)


def test_mcp_search_defaults_to_user_only(archive_home) -> None:
    """Bare thread_search scopes to USER messages; assistant text is opt-in via
    content_type='all' (or the specific type) — except the one-shot dry-scope
    widen, covered by its own test. Mirrors the archive backend default."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])  # USER: "hello mcp"; ASSISTANT: "hi from mcp"
    ta.import_path(f)

    # default scope answers the query with the user hit — assistant text stays out
    assert "hi from mcp" not in thread_search("mcp")
    # 'all' clears the filter → assistant text now found
    assert "hi from mcp" in thread_search("from mcp", content_type="all")
    # an explicit type targets it directly
    assert "hi from mcp" in thread_search("from mcp", content_type="text")
    # the user message is always reachable under the default
    assert "hello mcp" in thread_search("hello mcp")


def test_mcp_search_widens_to_text_when_default_scope_dry(archive_home) -> None:
    """A default-scope search with no keyword match retries once with assistant
    text included (and says so); an explicit content_type never widens."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])  # USER: "hello mcp"; ASSISTANT: "hi from mcp"
    ta.import_path(f)

    # "hi" appears only in assistant text → default scope is dry → auto-widen
    out = thread_search("hi")
    assert "hi from mcp" in out
    assert out.startswith("note: no keyword match in the default scope")
    # an explicit scope is a deliberate choice — no widen, no note
    narrow = thread_search("hi", content_type="user")
    assert "hi from mcp" not in narrow and "note: no keyword match" not in narrow


def test_mcp_search_rendering(archive_home) -> None:
    """The shaping args render through the MCP wrapper: quality signal, count,
    linkable, startswith."""
    import json as _json

    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    # default render carries the match-quality signal
    out = thread_search("hello mcp")
    assert "quality=" in out and "/2" in out  # 2-term query → K/2 per hit

    # count → per-thread tally with corpus stats
    cnt = thread_search("hello", output="count")
    assert cnt.startswith("Total:") and "corpus:" in cnt

    # linkable → JSON of ids
    arr = _json.loads(thread_search("hello", output="linkable"))
    assert arr and "event_id" in arr[0]

    # startswith → structural prefix scan (query text ignored)
    assert "hello mcp" in thread_search("zzz", startswith="hello")


def test_mcp_call_tool_dispatch(archive_home) -> None:
    """The tools also work through the MCP call_tool dispatch path."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    result = asyncio.run(mcp.call_tool("thread_search", {"query": "hello"}))
    # FastMCP returns a (content, ...) tuple or a content list depending on version;
    # normalize to text and assert the hit shows up.
    text = json.dumps(result, default=str)
    assert "hello mcp" in text
