"""CLI delegation to the shared MCP server (`thread_archive._delegate`).

A `thread-archive search` that resolves to the default archive home asks the
warm :8788 server instead of building a serving process for one call. What is
pinned here: the wire (one stateless JSON-RPC ``tools/call`` POST, plain-JSON
answer), the fallback contract (every failure shape returns None so the caller
runs in-process), and eligibility (only the default home delegates — an
explicit or env home names a different archive). The servers stood up here are
real HTTP servers speaking the shape :mod:`thread_archive._mcp.server`
configures (``stateless_http`` + ``json_response``); what they don't run is
the engine behind it, which has its own suites.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from thread_archive import _config as config
from thread_archive import _delegate

SENTINEL = "1 result(s) — served by the fake shared server"


def _serve(reply: dict, requests: list[dict]):
    """A live loopback HTTP endpoint answering every POST with ``reply``.

    Returns (url, shutdown). Bodies received are appended to ``requests`` so a
    test can assert what went over the wire.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length", 0))
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # quiet
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"

    def stop() -> None:
        srv.shutdown()
        srv.server_close()

    return url, stop


def _rpc_text(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


# ── the wire ─────────────────────────────────────────────────────────────────

def test_call_posts_tools_call_and_returns_the_text() -> None:
    seen: list[dict] = []
    url, stop = _serve(_rpc_text(SENTINEL), seen)
    try:
        out = _delegate.call("thread_search", {"query": "warm flock", "limit": 2}, url=url)
    finally:
        stop()
    assert out == SENTINEL
    (req,) = seen
    assert req["method"] == "tools/call"
    assert req["params"]["name"] == "thread_search"
    assert req["params"]["arguments"] == {"query": "warm flock", "limit": 2}


def test_call_joins_multiple_text_parts() -> None:
    reply = _rpc_text("")
    reply["result"]["content"] = [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
    url, stop = _serve(reply, [])
    try:
        assert _delegate.call("thread_search", {"query": "x"}, url=url) == "a\nb"
    finally:
        stop()


# ── every failure shape is None, never a raise ───────────────────────────────

def test_tool_error_falls_back_rather_than_relaying() -> None:
    """isError → None: the local re-run owns error rendering and exit codes."""
    reply = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "boom"}], "isError": True},
    }
    url, stop = _serve(reply, [])
    try:
        assert _delegate.call("thread_search", {"query": "x"}, url=url) is None
    finally:
        stop()


def test_malformed_answer_is_none() -> None:
    url, stop = _serve({"jsonrpc": "2.0", "id": 1, "error": {"code": -32600}}, [])
    try:
        assert _delegate.call("thread_search", {"query": "x"}, url=url) is None
    finally:
        stop()


def test_empty_content_is_none() -> None:
    reply = _rpc_text("")
    reply["result"]["content"] = []
    url, stop = _serve(reply, [])
    try:
        assert _delegate.call("thread_search", {"query": "x"}, url=url) is None
    finally:
        stop()


def test_nothing_listening_is_none_and_fast() -> None:
    """A dead loopback port refuses immediately — liveness probing is free."""
    url, stop = _serve(_rpc_text("x"), [])
    stop()  # port is now closed
    t0 = time.monotonic()
    assert _delegate.call("thread_search", {"query": "x"}, url=url) is None
    assert time.monotonic() - t0 < 2.0


# ── eligibility ──────────────────────────────────────────────────────────────

def test_non_default_home_never_delegates(archive_home) -> None:
    """An env home pointing at a tmp archive is a different archive."""
    assert _delegate.eligible(None) is False


def test_explicit_other_home_never_delegates(tmp_path) -> None:
    assert _delegate.eligible(str(tmp_path)) is False


def test_default_home_delegates_even_spelled_explicitly(monkeypatch) -> None:
    monkeypatch.delenv(config.ENV_HOME, raising=False)
    assert _delegate.eligible(None) is True
    assert _delegate.eligible(str(config.default_home())) is True


def test_env_kill_switch(monkeypatch) -> None:
    monkeypatch.delenv(config.ENV_HOME, raising=False)
    monkeypatch.setenv(_delegate.ENV_OFF, "1")
    assert _delegate.eligible(None) is False


# ── the verb, end to end ─────────────────────────────────────────────────────

def test_search_verb_delegates_for_the_default_home(tmp_path) -> None:
    """A real one-shot against a live fake server: prints the server's text,
    exits 0, and never opens (or creates) a local archive. HOME is pointed at a
    tmp dir so "the default home" is a sandbox rather than this machine's."""
    seen: list[dict] = []
    url, stop = _serve(_rpc_text(SENTINEL), seen)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in (config.ENV_HOME, config.ENV_TRUTH, config.ENV_INDEX)
    }
    env["HOME"] = str(tmp_path)
    env[_delegate.ENV_URL] = url
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from thread_archive.cli import main; sys.exit(main(sys.argv[1:]))",
                "search",
                "warm flock",
                "--limit",
                "2",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        stop()
    assert proc.returncode == 0, proc.stderr
    assert SENTINEL in proc.stdout
    assert seen and seen[0]["params"]["name"] == "thread_search"
    # The engine never ran here: no index was created under the sandbox home.
    assert not (tmp_path / ".thread" / "archive" / "index.db").exists()


def test_every_typed_filter_reaches_the_wire(monkeypatch, capsys) -> None:
    """A delegated search carries the same scope the local one would.

    This is the failure shape delegation has that nothing else does, and it has
    no symptom: a filter dropped on the way to the warm server comes back as a
    *wider* search, ranked, rendered, and indistinguishable from a real answer.
    So the arguments are built from ``thread_search``'s own signature
    (:func:`thread_archive.cli.cmd_search`) rather than from a second list of
    names, and this is that holding over every flag at once.
    """
    from thread_archive.cli import main

    seen: list[dict] = []
    url, stop = _serve(_rpc_text(SENTINEL), seen)
    monkeypatch.delenv(config.ENV_HOME, raising=False)  # = the default home delegates
    monkeypatch.setenv(_delegate.ENV_URL, url)
    try:
        rc = main([
            "search", "retry backoff", "--limit", "7", "--page", "3",
            "--thread-id", "01JQ8ZK4X0000000000000000", "--content-type", "user",
            "--exclude-content-type", "tool", "--since", "7d", "--until", "1d",
            "--tool-name", "Bash", "--source", "claude-code", "--types", "system",
            "--agents", "only", "--startswith", "commit ", "--path", "rank.py",
            "--path-ops", "edit", "--commit", "deadbeef", "--pr", "4",
            "--repo", "/repo", "--sort", "oldest", "--output", "linkable",
            "--context-lines", "5", "--context-events", "1:1", "--match", "substring",
        ])
    finally:
        stop()

    assert rc == 0 and SENTINEL in capsys.readouterr().out
    assert len(seen) == 1
    sent = seen[0]["params"]["arguments"]
    assert sent == {
        "query": "retry backoff", "limit": 7, "page": 3,
        "thread_id": "01JQ8ZK4X0000000000000000", "content_type": "user",
        "exclude_content_type": "tool", "since": "7d", "until": "1d",
        "tool_name": "Bash", "source": "claude-code", "types": "system",
        "agents": "only", "startswith": "commit ", "path": "rank.py",
        "path_ops": "edit", "commit": "deadbeef", "pr": "4", "repo": "/repo",
        "sort": "oldest", "output": "linkable", "context_lines": 5,
        "context_events": "1:1", "match": "substring",
    }
    # Nothing the door owns rides along: --home and --local decide *who* answers,
    # and the server has no business being told either.
    assert not ({"home", "local", "func"} & set(sent))


def test_search_verb_stays_local_for_another_home(archive_home, capsys) -> None:
    """With a non-default home the fake server must never be contacted — the
    sentinel appearing in output would mean a query about one archive was
    answered from another."""
    from thread_archive.cli import main

    seen: list[dict] = []
    url, stop = _serve(_rpc_text(SENTINEL), seen)
    try:
        os.environ[_delegate.ENV_URL] = url
        try:
            rc = main(["search", "anything", "--limit", "2", "--home", str(archive_home)])
        finally:
            os.environ.pop(_delegate.ENV_URL, None)
    finally:
        stop()
    out = capsys.readouterr().out
    assert rc == 0
    assert SENTINEL not in out
    assert seen == []
