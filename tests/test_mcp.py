"""The library-native MCP server exposes thread_search + thread_read."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from thread_archive import _api as ta
from thread_archive._mcp import server
from thread_archive._mcp.server import IngestThrottle, ServePlan, mcp, thread_read, thread_search

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
    assert {"around_event", "context_turns"} <= set(read_props)
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

    event_id = ta.search("hello")[0]["event_id"]
    focused = thread_read(thread_id, around_event=event_id, context_turns=0)
    assert f"match:{event_id}" in focused and "hi from mcp" in focused


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
    # 'all' clears the filter → assistant text now found (group='none': grouping
    # would fold the assistant hit into the higher-weighted user hit's row)
    assert "hi from mcp" in thread_search("from mcp", content_type="all", group="none")
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


def test_mcp_search_prepends_degradation_notice(archive_home) -> None:
    """A degraded source (coverage's verdict in health.json) prepends one line
    naming the remedy — unconditionally, since a degraded source's freshest
    content is exactly what search can't return."""
    from thread_archive._ops.health import record_health

    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    record_health("coverage_last", {"ok": True, "degraded": {
        "claude-code": {"reason": "validation_drift",
                        "since": "2026-07-12T00:00:00+00:00"}}})
    out = thread_search("hello")
    assert out.startswith("note: claude-code import is degraded")
    assert "since 2026-07-12" in out
    assert "archive fix-import claude-code" in out
    assert "hello mcp" in out  # the notice prepends; results still render

    # a healthy verdict clears it
    record_health("coverage_last", {"ok": True, "degraded": {}})
    assert "degraded" not in thread_search("hello")


def test_mcp_degradation_notice_ignores_stale_verdicts(archive_home) -> None:
    """A frozen verdict from a dead nightly must not nag forever — verdicts
    older than the cutoff are ignored (the nightly's own staleness alarm is the
    surface for that failure)."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    (archive_home / "health.json").write_text(json.dumps({"coverage_last": {
        "at": "2020-01-01T00:00:00+00:00",
        "degraded": {"claude-code": {"reason": "went_dark", "since": None}},
    }}))
    assert "degraded" not in thread_search("hello")


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


def test_plan_stdio_default_neither_warms_nor_serves_http(monkeypatch) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_MCP_WARM", raising=False)
    # stdio default: no transport argument for run(), and no model warm.
    assert server.plan_serve([]) == ServePlan(transport=None, warm=False)


def test_plan_http_warms_and_serves_streamable() -> None:
    plan = server.plan_serve(["--http", "--host", "localhost", "--port", "9999"])
    # --http picks the shared transport, carries the bind, and warms the model
    # stack (one resident copy every client shares).
    assert plan == ServePlan(transport="streamable-http", warm=True,
                             host="localhost", port=9999)
    # Applying it points the server at that bind, in the many-agents shape.
    server.apply_settings(plan)
    assert mcp.settings.host == "localhost" and mcp.settings.port == 9999
    assert mcp.settings.stateless_http is True and mcp.settings.json_response is True


def test_plan_http_refuses_nonloopback_host_without_optin(monkeypatch, capsys) -> None:
    # The server is unauthenticated full read of the archive: binding beyond
    # loopback must be an explicit opt-in, exactly like the web viewer.
    monkeypatch.delenv("THREAD_ARCHIVE_MCP_NONLOCAL", raising=False)
    with pytest.raises(SystemExit):
        server.plan_serve(["--http", "--host", "1.2.3.4"])
    assert "refusing non-loopback bind" in capsys.readouterr().err


def test_plan_http_nonloopback_host_with_optin(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_NONLOCAL", "1")
    plan = server.plan_serve(["--http", "--host", "1.2.3.4", "--port", "9999"])
    assert plan.transport == "streamable-http" and plan.host == "1.2.3.4"


def test_plan_stdio_warms_when_env_opts_in(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_WARM", "1")
    assert server.plan_serve([]).warm is True


# ── the two real deployments ─────────────────────────────────────────────────
# stdio (one server per MCP client) and streamable-HTTP (one shared always-on
# server) are the documented invocations, so they are exercised as invocations:
# a real child process running the real entry point, spoken to over the real
# transport. Nothing about the server is stubbed — these are the tests that
# would catch a transport, registration, or startup break.


def _server_env(home) -> dict:
    """The environment an MCP client gives the server, pinned to a throwaway
    archive and model-free (the child does not inherit the suite's fixtures)."""
    return {**os.environ,
            "THREAD_ARCHIVE_HOME": str(home),
            "THREAD_ARCHIVE_MCP_INGEST": "0",
            "THREAD_ARCHIVE_EMBED": "off",
            "THREAD_ARCHIVE_RERANK": "off"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.integration
def test_stdio_server_answers_a_real_client_over_the_module_entry(archive_home) -> None:
    """``python -m thread_archive._mcp.server`` — the documented per-client
    stdio invocation — completes a real MCP handshake, lists its tools, and
    searches a seeded archive."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "thread_search", "arguments": {"query": "hello"}}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "thread_archive._mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_server_env(archive_home),
    )
    # A client holds stdin open while it waits: EOF is how it says goodbye, and
    # the server takes it as one, cancelling anything still in flight.
    watchdog = threading.Timer(120, proc.kill)
    watchdog.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write("".join(json.dumps(fr) + "\n" for fr in frames))
        proc.stdin.flush()
        lines = [proc.stdout.readline() for _ in range(3)]
        proc.stdin.close()
        assert proc.wait(timeout=60) == 0
    finally:
        watchdog.cancel()
        if proc.poll() is None:  # pragma: no cover — only on a hung server
            proc.kill()
    replies = {d["id"]: d for d in (json.loads(ln) for ln in lines if ln.strip())}
    assert replies[1]["result"]["serverInfo"]["name"] == "thread-archive"
    assert {t["name"] for t in replies[2]["result"]["tools"]} == {"thread_search", "thread_read"}
    assert "hello mcp" in json.dumps(replies[3]["result"])


@pytest.mark.integration
def test_http_server_serves_the_shared_streamable_transport(archive_home) -> None:
    """``archive-mcp --http`` binds the requested loopback port and answers
    stateless JSON requests — the shared always-on deployment the LaunchAgent
    runs and every client points at."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "thread_archive._mcp.server", "--http",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=_server_env(archive_home),
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert proc.poll() is None, "server exited before binding"
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        else:  # pragma: no cover — the bind is sub-second in practice
            raise AssertionError(f"server never bound 127.0.0.1:{port}")

        def _call(payload: dict) -> dict:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/mcp", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.status == 200
                return json.loads(resp.read())

        # Stateless: each request stands alone, no session handshake to carry.
        listed = _call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert {t["name"] for t in listed["result"]["tools"]} == {"thread_search", "thread_read"}
        called = _call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "thread_read",
                                   "arguments": {"thread_id": ta.search("hello")[0]["thread_id"]}}})
        assert "hello mcp" in json.dumps(called["result"])
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def test_mcp_search_resolves_thread_and_topic_refs(archive_home) -> None:
    """thread_id / topic_id accept any ref shape and resolve up front; a ref
    matching nothing says so instead of silently returning zero hits."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    tid = ta.search("hello")[0]["thread_id"]
    out = thread_search("hello", thread_id=tid)
    assert "hello mcp" in out
    # legacy-shaped ref that matches nothing → explicit not-found, not empty results
    missing = thread_search("hello", thread_id="999999999")
    assert "thread 999999999 not found" in missing
    # topic_id resolves through the same ref machinery
    resolved = thread_search("hello", topic_id=tid)
    assert "not found" not in resolved
    missing_topic = thread_search("hello", topic_id="999999999")
    assert "topic 999999999 not found" in missing_topic


def test_throttle_skips_when_a_pass_is_in_flight(monkeypatch) -> None:
    """One in-flight catch-up per process: while the slot is taken, another
    claim is refused without recording an attempt."""
    monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST", raising=False)
    throttle = IngestThrottle()
    assert throttle.running.acquire(blocking=False)  # a pass is under way
    assert not throttle.claim()
    assert throttle.last == 0.0  # refused before the attempt was marked
    throttle.release()

    # Free slot → claimed, and the attempt marked.
    assert throttle.claim()
    marked = throttle.last
    assert marked > 0.0
    throttle.release()
    # Released, but the interval now holds the next attempt back.
    assert not throttle.claim()
    assert throttle.last == marked


def test_throttle_refuses_while_the_kill_switch_is_set(monkeypatch) -> None:
    """``THREAD_ARCHIVE_MCP_INGEST=0`` stops the pass before anything is
    claimed or recorded — read per call, so it applies whenever it is set."""
    throttle = IngestThrottle()
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "0")
    throttle.maybe_catch_up()
    assert throttle.last == 0.0
    monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST")
    assert throttle.claim()


def test_pass_swallows_ingest_failure_and_releases(archive_home, monkeypatch) -> None:
    """A failing catch-up pass is advisory: it must not raise into the tool call
    and must release the in-flight slot for the next attempt."""
    # A home that cannot exist (its parent is a file) — the pass hits a real
    # OSError opening the archive, the way a broken/unmounted home would.
    blocker = archive_home / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(blocker / "home"))
    monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST", raising=False)

    throttle = IngestThrottle()
    assert throttle.claim()
    throttle.run_pass()  # must not raise
    # the slot was released in the finally — the next pass can claim it
    assert throttle.running.acquire(blocking=False)
    throttle.running.release()


def test_maybe_catch_up_runs_the_pass_off_the_caller_thread(archive_home, monkeypatch) -> None:
    """The kick is a background pass: the tool call returns immediately, and the
    pass releases the slot when it finishes. With another process owning ingest
    (the always-on watcher's flock), that pass is a no-op probe."""
    from thread_archive._watcher import try_ingest_owner_lock

    monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST", raising=False)
    throttle = IngestThrottle()
    with try_ingest_owner_lock() as owned:  # stand in for the watcher daemon
        assert owned
        throttle.maybe_catch_up()
        assert throttle.last > 0.0  # the attempt was marked
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if throttle.running.acquire(blocking=False):
                throttle.running.release()
                break
            time.sleep(0.01)
        else:  # pragma: no cover — the probe returns in milliseconds
            raise AssertionError("the background pass never released the slot")


def test_default_scope_weakness_needs_query_terms() -> None:
    """A termless query (empty / punctuation-only) can never be judged weak —
    there is nothing to look for in the top hit."""
    assert server._default_scope_is_weak([], "") is False
    assert server._default_scope_is_weak([], "  ") is False


def test_degradation_notice_accepts_naive_timestamp(archive_home) -> None:
    """A verdict whose 'at' has no timezone is read as UTC, and an unknown
    reason still renders with the generic phrase."""
    from datetime import datetime, timezone

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    (archive_home / "health.json").write_text(json.dumps({"coverage_last": {
        "at": now_naive,
        "degraded": {
            "grok": {"reason": "capture_skips", "since": "2026-07-01T00:00:00Z"},
            "mystery": {"reason": "novel_failure"},
        },
    }}))
    notice = server._degradation_notices()
    assert "grok import is degraded (content is being consumed without importing" in notice
    assert "since 2026-07-01" in notice
    # unknown reason → generic phrase, and no dangling "since" for a missing date
    assert "mystery import is degraded (import degraded)" in notice


def test_degradation_notice_fails_soft_on_unparseable_record(archive_home) -> None:
    """A verdict whose timestamp won't parse yields no notice — retrieval must
    work identically when health.json is malformed."""
    (archive_home / "health.json").write_text(json.dumps({"coverage_last": {
        "at": None,
        "degraded": {"grok": {"reason": "went_dark"}},
    }}))
    assert server._degradation_notices() == ""


def test_maybe_catch_up_throttles_repeat_attempts(archive_home, monkeypatch) -> None:
    """A recent attempt suppresses the next one — at most one catch-up kick per
    interval per process."""
    from thread_archive._watcher import try_ingest_owner_lock

    monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST", raising=False)
    throttle = IngestThrottle()
    with try_ingest_owner_lock() as owned:
        assert owned
        throttle.maybe_catch_up()
        first = throttle.last
        assert first > 0.0
        throttle.maybe_catch_up()
        assert throttle.last == first  # throttled: no second attempt marked
