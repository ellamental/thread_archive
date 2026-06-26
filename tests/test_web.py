"""The ``archive web`` surface — the socket-free router over thread_archive.api.

The HTTP adapter is a thin stdlib shim; the logic lives in :func:`route`, which is
pure ``(method, path, params) -> (status, content_type, body, headers)``. These
exercise it directly (no sockets) against a seeded throwaway archive.
"""

from __future__ import annotations

import json

import thread_archive as ta
from thread_archive.web import route

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello webview"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from the assistant"}]}}


def _seed(archive_home):
    f = archive_home / "sess.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in (USER, ASSISTANT)) + "\n", encoding="utf-8")
    ta.import_path(f)


def _get(path, **params):
    qp = {k: [str(v)] for k, v in params.items()}
    status, ctype, body, _ = route("GET", path, qp)
    payload = json.loads(body) if ctype.startswith("application/json") else body
    return status, ctype, payload


def _claude_code_session_uuid(archive_home):
    """The session uuid the claude-code importer recorded in ImportState for the
    seeded thread — the kind of id an editor would resolve via archive-link."""
    from sqlalchemy import select

    from thread_archive.store import ImportState, get_session

    ta.open_archive(str(archive_home))
    with get_session() as s:
        sid = s.execute(select(ImportState.source_id)).scalars().first()
    # source_id is "{project}:{uuid}"; archive-link accepts the bare uuid suffix
    return sid.split(":")[-1]


def test_status_endpoint(archive_home):
    _seed(archive_home)
    status, ctype, payload = _get("/api/status")
    assert status == 200 and ctype == "application/json"
    assert payload["threads"] == 1 and payload["events"] > 0
    assert payload["fts_indexed"] > 0


def test_search_endpoint(archive_home):
    _seed(archive_home)
    status, _, payload = _get("/api/search", q="hello")
    assert status == 200
    assert payload["query"] == "hello"
    assert payload["hits"] and all(h["thread_title"] for h in payload["hits"])
    # datetimes serialize as ISO strings (json-safe)
    assert all(isinstance(h["occurred_at"], (str, type(None))) for h in payload["hits"])


def test_empty_query_returns_no_hits(archive_home):
    _seed(archive_home)
    status, _, payload = _get("/api/search", q="   ")
    assert status == 200 and payload["hits"] == []


def test_threads_endpoint(archive_home):
    _seed(archive_home)
    status, _, payload = _get("/api/threads")
    assert status == 200
    assert len(payload["threads"]) == 1
    t = payload["threads"][0]
    assert t["id"] and t["title"] and "updated_at" in t


def test_threads_query_filter(archive_home):
    _seed(archive_home)
    # the seeded thread's title won't contain this token
    _, _, payload = _get("/api/threads", q="zzz-nonexistent")
    assert payload["threads"] == []


def test_read_endpoint(archive_home):
    _seed(archive_home)
    _, _, search = _get("/api/search", q="hello")
    tid = search["hits"][0]["thread_id"]
    status, _, payload = _get(f"/api/read/{tid}")
    assert status == 200
    assert payload["thread_id"] == tid
    assert "## USER" in payload["transcript"] and "hello webview" in payload["transcript"]


def test_read_bad_id_is_404(archive_home):
    _seed(archive_home)
    status, _, _, _ = route("GET", "/api/read/not-a-number", {})
    assert status == 404


def test_structured_thread_endpoint(archive_home):
    _seed(archive_home)
    _, _, search = _get("/api/search", q="hello")
    tid = search["hits"][0]["thread_id"]
    status, _, payload = _get(f"/api/thread/{tid}")
    assert status == 200
    assert payload["thread_id"] == tid and payload["title"]
    # contiguous same-role events grouped into messages of typed blocks
    roles = [m["role"] for m in payload["messages"]]
    assert "user" in roles and "assistant" in roles
    blocks = [b for m in payload["messages"] for b in m["blocks"]]
    assert any(b["type"] == "text" and "hello webview" in b["text"] for b in blocks)


def test_structured_thread_bad_id_404(archive_home):
    _seed(archive_home)
    status, _, _, _ = route("GET", "/api/thread/nope", {})
    assert status == 404


def test_built_assets_served(archive_home):
    # the vite build emits hashed assets under static/assets/ — they must be served
    # with a js/css content type, not the SPA fallback HTML.
    from pathlib import Path

    from thread_archive.web import server

    assets = Path(server.STATIC_DIR) / "assets"
    if not assets.is_dir():
        import pytest

        pytest.skip("frontend not built (no static/assets)")
    js = next((p for p in assets.iterdir() if p.suffix == ".js"), None)
    assert js is not None, "no built JS asset found"
    status, ctype, body, _ = route("GET", f"/assets/{js.name}", {})
    assert status == 200
    assert "javascript" in ctype
    assert len(body) > 0


def test_path_traversal_blocked(archive_home):
    # a climbing path must not escape static/ — falls through to the SPA shell, not /etc
    status, ctype, body, _ = route("GET", "/../../etc/passwd", {})
    assert status == 200 and ctype.startswith("text/html")
    assert b"root:" not in body


def test_spa_routes_serve_index(archive_home):
    for path in ("/", "/search", "/archive", "/archive/42"):
        status, ctype, body, _ = route("GET", path, {})
        assert status == 200, path
        assert ctype.startswith("text/html")
        assert b"thread-archive" in body


def test_unknown_path_404(archive_home):
    status, _, _, _ = route("GET", "/api/nope", {})
    assert status == 404


def test_non_get_405(archive_home):
    status, _, _, _ = route("POST", "/api/status", {})
    assert status == 405


# ---- archive-link: resolve a provider session id to its archive thread ----
def test_archive_link_resolves_to_thread(archive_home):
    _seed(archive_home)
    _, _, search = _get("/api/search", q="hello")
    tid = search["hits"][0]["thread_id"]
    uuid = _claude_code_session_uuid(archive_home)
    status, _, payload = _get("/api/archive-link", id=uuid)
    assert status == 200
    assert payload["thread_id"] == tid
    assert payload["url"] == f"/archive/{tid}"


def test_archive_link_redirect(archive_home):
    _seed(archive_home)
    uuid = _claude_code_session_uuid(archive_home)
    status, _, _, headers = route("GET", "/api/archive-link", {"id": [uuid], "redirect": ["1"]})
    assert status == 302
    assert headers["Location"].startswith("/archive/")


def test_archive_link_unknown_is_404(archive_home):
    _seed(archive_home)
    status, _, payload = _get("/api/archive-link", id="no-such-uuid")
    assert status == 404
    assert "error" in payload


def test_archive_link_missing_id_400(archive_home):
    status, _, _, _ = route("GET", "/api/archive-link", {})
    assert status == 400


def test_serve_in_thread_cohosts(archive_home):
    # the watcher's cohost path: a background server answering over a real socket
    import urllib.request

    from thread_archive.web import serve_in_thread

    _seed(archive_home)
    ta.open_archive(str(archive_home))
    httpd = serve_in_thread(host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5).read()
        assert json.loads(body)["threads"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
