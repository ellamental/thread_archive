"""The library-native MCP server exposes thread_search + thread_read.

The tools' own behaviour is `thread_archive._tools` (shared with the CLI verbs —
tests/test_cli_retrieval.py drives that door); what this file covers is the MCP
serving of them: schema, transports, the bind guard, and the cohosted ingest.
"""

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
from thread_archive import _tools
from thread_archive._mcp import server
from thread_archive._mcp.server import IngestThrottle, ServePlan, mcp, thread_read, thread_search

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello mcp"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from mcp"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _ledger(archive_home) -> list[dict]:
    """The usage records this process wrote, oldest first.

    The tool clamps its sizing arguments and then runs with the clamped values;
    the ledger logs the parameters the search *actually* ran with, so it is where
    a bound that never reached the engine is observable without reaching inside
    the call.
    """
    from thread_archive._retrieval import usage

    path = archive_home / usage.LEDGER_FILE
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_mcp_registers_two_tools() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {"thread_search", "thread_read"}
    # the schema is derived from the typed signature
    by_name = {t.name: t for t in tools}
    search_props = by_name["thread_search"].inputSchema["properties"]
    assert "query" in search_props
    # the filters + shaping args wired through from the library layer are exposed
    assert {"exclude_content_type", "source", "startswith", "sort",
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
    """exclude_content_type / source dispatch through the MCP wrapper."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)  # imports as source='claude-code'

    # source matches → hit present; comma-separated string splits to a list
    assert "hello mcp" in thread_search("hello", source="claude-code,cursor")
    # source nobody has → no hit
    assert "hello mcp" not in thread_search("hello", source="chatgpt")
    # excluding the user content type drops the user-message hit
    assert "hello mcp" not in thread_search("hello", exclude_content_type="user")


def test_mcp_search_filters_by_tool_name_and_until(archive_home) -> None:
    """The two remaining engine filters the tool passes straight through: which
    tool an event ran, and the upper end of the time window."""
    early = archive_home / "early.jsonl"
    _write_cc(early, [
        {"type": "user", "uuid": "e1", "timestamp": "2026-01-01T10:00:00Z",
         "cwd": "/proj", "message": {"role": "user", "content": "grep the changelog"}},
        {"type": "assistant", "uuid": "e2", "timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "grep -n changelog README.md"}}]}},
    ])
    ta.import_path(early)
    late = archive_home / "late.jsonl"
    _write_cc(late, [
        {"type": "user", "uuid": "l1", "timestamp": "2026-06-01T10:00:00Z",
         "cwd": "/proj", "message": {"role": "user", "content": "grep the changelog again"}},
    ])
    ta.import_path(late)

    # until bounds the window: the June turn drops, the January one stays.
    bounded = thread_search("changelog", until="2026-02-01", group="none")
    assert "grep the changelog" in bounded
    assert "changelog again" not in bounded

    # tool_name scopes to events that ran that tool; a tool nobody ran is empty.
    assert "grep -n changelog" in thread_search("changelog", tool_name="Bash",
                                                content_type="all")
    assert thread_search("changelog", tool_name="Nonesuch",
                         content_type="all").startswith("No results")


# ── bounding what the caller can ask for ─────────────────────────────────────
# The tool is the trust boundary: an agent picks these numbers, and each of them
# multiplies work inside the engine. The engine itself takes them at face value.


def test_mcp_search_bounds_caller_supplied_sizing(archive_home) -> None:
    """``limit`` sizes the candidate pool (``max(limit*5, 200)`` rows) and ``page``
    multiplies it, so an unbounded value is an unbounded scan by another name. Both
    are clamped before they reach the engine — [1, 500] and [1, 200] — and the
    ledger records the parameters the search actually ran with."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    thread_search("hello", limit=99999, page=99999)
    thread_search("hello", limit=0, page=0)
    over, under = _ledger(archive_home)[-2:]
    assert (over["limit"], over["page"]) == (500, 200)
    assert (under["limit"], under["page"]) == (1, 1)


def test_mcp_search_clamped_limit_still_answers(archive_home) -> None:
    """The floor is a clamp, not a rejection: ``limit=0`` is nonsense a caller can
    still send, and it must return the one best hit rather than an empty result
    that reads as 'nothing matched'."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    assert "hello mcp" in thread_search("hello", limit=0)


def test_mcp_search_bounds_the_context_window(archive_home) -> None:
    """``context_lines`` is a per-hit window, and a hit can be a very long
    document — an unbounded window renders the whole thing, once per hit. Clamped
    to 50 lines either side, so a 300-line message returns its middle, not itself."""
    # Line 1 is the thread's title, so it renders in the hit header whatever the
    # window does — the sentinels sit well inside the document instead.
    body = "\n".join(
        ["a session about the drive"]
        + [f"pad-{i:04d}" for i in range(2, 150)]
        + ["the tachyon condenser leaks"]
        + [f"pad-{i:04d}" for i in range(151, 301)]
    )
    f = archive_home / "long.jsonl"
    _write_cc(f, [{"type": "user", "uuid": "L1", "timestamp": "2026-01-01T10:00:00Z",
                   "cwd": "/proj", "message": {"role": "user", "content": body}}])
    ta.import_path(f)

    out = thread_search("tachyon", context_lines=99999)
    assert "the tachyon condenser leaks" in out       # the match is centred
    assert "pad-0100" in out and "pad-0200" in out    # ±50 lines: the window's edges
    assert "pad-0099" not in out                      # one line further up — clamped off
    assert "pad-0201" not in out                      # and one further down


def test_mcp_search_rejects_an_unknown_match_mode(archive_home) -> None:
    """An unusable argument is answered, not raised: the tool's caller is a model,
    and an MCP exception is a failed tool call it has to guess its way out of. The
    reply names both modes so the retry is informed."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    out = thread_search("hello", match="regex")
    assert "match must be 'token'" in out and "substring" in out
    assert "hello mcp" not in out                     # refused, not silently served
    # the two real modes still run
    assert "hello mcp" in thread_search("hello", match="token")
    assert "hello mcp" in thread_search("hello", match="substring")


def test_mcp_search_pages_through_the_result_set(archive_home) -> None:
    """Paging is verified against the engine elsewhere; this is the tool carrying
    it — the page reaching the engine, and the header telling the agent where it
    is, which is the whole mechanism by which it knows to ask for more."""
    for i in range(7):
        f = archive_home / f"p{i}.jsonl"
        _write_cc(f, [{"type": "user", "uuid": f"p{i}", "cwd": "/proj",
                       "timestamp": f"2026-01-0{(i % 7) + 1}T10:00:00Z",
                       "message": {"role": "user", "content": f"the widget report {i}"}}])
        ta.import_path(f)

    first = thread_search("widget", limit=3, group="browse")
    assert "page 1/3" in first and "of 7" in first
    second = thread_search("widget", limit=3, page=2, group="browse")
    assert "page 2/3" in second

    # disjoint pages: no thread served twice across the walk
    def _ids(rendered):
        return {ln.split()[0] for ln in rendered.splitlines() if ln.startswith("01")}

    walked = [_ids(thread_search("widget", limit=3, page=p, group="browse"))
              for p in (1, 2, 3)]
    assert sum(len(p) for p in walked) == len(set().union(*walked))

    past = thread_search("widget", limit=3, page=9, group="browse")
    assert "past the end" in past


def test_mcp_search_defaults_to_the_whole_transcript(archive_home) -> None:
    """Bare thread_search reads the whole conversation — an answer that lives only
    in assistant text is found on the first pass, with no scope to widen and no
    note to explain. An explicit content_type still narrows to one type."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])  # USER: "hello mcp"; ASSISTANT: "hi from mcp"
    ta.import_path(f)

    # "hi" appears only in assistant text, and the default scope reaches it
    out = thread_search("hi")
    assert "hi from mcp" in out
    assert not out.startswith("note:")  # nothing to apologize for
    # the user message is reachable under the same default
    assert "hello mcp" in thread_search("hello mcp")
    # an explicit type still narrows: "hi" is not in the user message
    assert "hi from mcp" not in thread_search("hi", content_type="user")


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
    assert "thread-archive fix-import claude-code" in out
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
            proc.wait(timeout=30)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
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
        if proc.stdout is not None:
            proc.stdout.close()


def test_mcp_search_resolves_thread_refs(archive_home) -> None:
    """thread_id accepts any ref shape and resolves up front; a ref matching
    nothing says so instead of silently returning zero hits."""
    f = archive_home / "sess.jsonl"
    _write_cc(f, [USER, ASSISTANT])
    ta.import_path(f)

    tid = ta.search("hello")[0]["thread_id"]
    out = thread_search("hello", thread_id=tid)
    assert "hello mcp" in out
    # legacy-shaped ref that matches nothing → explicit not-found, not empty results
    missing = thread_search("hello", thread_id="999999999")
    assert "thread 999999999 not found" in missing


def test_throttle_skips_when_a_pass_is_in_flight(monkeypatch) -> None:
    """One in-flight catch-up per process: while the slot is taken, another
    claim is refused without recording an attempt."""
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "1")
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


def test_throttle_requires_an_explicit_ingest_opt_in(monkeypatch) -> None:
    """Absent, negative, and malformed values are read-only; affirmative values
    opt in, read per call so a client-supplied environment is honored."""
    throttle = IngestThrottle()
    for value in (None, "0", "false", "unexpected"):
        if value is None:
            monkeypatch.delenv("THREAD_ARCHIVE_MCP_INGEST", raising=False)
        else:
            monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", value)
        throttle.maybe_catch_up()
        assert throttle.last == 0.0
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "1")
    assert throttle.claim()


def test_pass_swallows_ingest_failure_and_releases(archive_home, monkeypatch) -> None:
    """A failing catch-up pass is advisory: it must not raise into the tool call
    and must release the in-flight slot for the next attempt."""
    # A home that cannot exist (its parent is a file) — the pass hits a real
    # OSError opening the archive, the way a broken/unmounted home would.
    blocker = archive_home / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(blocker / "home"))
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "1")

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

    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "1")
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


def test_default_scope_is_the_whole_transcript_minus_summaries() -> None:
    """The default search scope names no content type — everything the index holds
    is in play — and excludes only the librarian's derived summaries. Tool output
    needs no exclusion here because it never reaches the index at all."""
    assert _tools.DEFAULT_SEARCH_CONTENT_TYPES is None
    assert _tools.DEFAULT_SEARCH_EXCLUDE == ("summary",)


def test_warm_pass_primes_the_scope_agents_search() -> None:
    """The warm search and the agent surface share one scope constant: the vector
    matrix caches per content-type scope, so a drift between them would leave the
    first real query building a matrix inside the request."""
    from thread_archive import _retrieval

    assert _tools.DEFAULT_SEARCH_CONTENT_TYPES is _retrieval.DEFAULT_CONTENT_TYPES
    assert _tools.DEFAULT_SEARCH_EXCLUDE is _retrieval.DEFAULT_EXCLUDE_CONTENT_TYPES


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
    notice = _tools._degradation_notices()
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
    assert _tools._degradation_notices() == ""


def test_maybe_catch_up_throttles_repeat_attempts(archive_home, monkeypatch) -> None:
    """A recent attempt suppresses the next one — at most one catch-up kick per
    interval per process."""
    from thread_archive._watcher import try_ingest_owner_lock

    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "1")
    throttle = IngestThrottle()
    with try_ingest_owner_lock() as owned:
        assert owned
        throttle.maybe_catch_up()
        first = throttle.last
        assert first > 0.0
        throttle.maybe_catch_up()
        assert throttle.last == first  # throttled: no second attempt marked
    # Releasing the owner flock lets the already-started background probe run.
    # Wait for it before fixture teardown closes the process-global engine;
    # otherwise this test races teardown and strands the probe's SQLite pool.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if throttle.running.acquire(blocking=False):
            throttle.running.release()
            break
        time.sleep(0.01)
    else:  # pragma: no cover — catch-up finishes in milliseconds
        raise AssertionError("the background pass never released the slot")
