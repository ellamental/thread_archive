"""The ``archive web`` surface — the socket-free router over thread_archive.api.

The HTTP adapter is a thin stdlib shim; the logic lives in :func:`route`, which is
pure ``(method, path, params) -> (status, content_type, body, headers)``. These
exercise it directly (no sockets) against a seeded throwaway archive.
"""

from __future__ import annotations

import json

import thread_archive as ta
from thread_archive._web import route

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


def _get_multi(path, **params):
    """Like :func:`_get` but each param is already a list — for repeated keys."""
    status, ctype, body, _ = route("GET", path, {k: list(v) for k, v in params.items()})
    payload = json.loads(body) if ctype.startswith("application/json") else body
    return status, ctype, payload


def _claude_code_session_uuid(archive_home):
    """The session uuid the claude-code importer recorded in ImportState for the
    seeded thread — the kind of id an editor would resolve via archive-link."""
    from sqlalchemy import select

    from thread_archive._store import ImportState, get_session

    ta.open_archive(str(archive_home))
    with get_session() as s:
        sid = s.execute(select(ImportState.source_id)).scalars().first()
    # source_id is "{project}:{uuid}"; archive-link accepts the bare uuid suffix
    return sid.split(":")[-1]


def _seed_cloth(archive_home, uuid="27056da6-8578-4a8c-ab90-d634702dc42d"):
    """Import a minimal cloth session the way the watcher does — source_id is the bare
    session uuid (the file stem, no prefix). Returns the uuid a paster would drop into
    ``/archive/<uuid>`` and the archive thread id it seeded."""
    from thread_archive._importers import import_cloth_session_incremental

    f = archive_home / f"{uuid}.jsonl"
    lines = [
        dict(USER, message={"role": "user", "content": "hello from cloth"}),
        dict(ASSISTANT, message={"role": "assistant", "model": "claude-opus-4",
                                 "content": [{"type": "text", "text": "hi from cloth"}]}),
    ]
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.open_archive(str(archive_home))
    res = import_cloth_session_incremental(f, source_id=uuid)
    return uuid, res.thread_id


def _seed_codex(archive_home, uuid="019f33d1-3e87-7a42-bab1-489d754fd0df"):
    """Import a minimal codex session the way the watcher does — source_id is the
    rollout filename stem ``rollout-{ts}-{uuid}`` (dash-joined, no colon). Returns the
    bare ``uuid`` an editor's archive-link passes and the archive thread id it seeded."""
    from thread_archive._importers import import_codex_session_incremental

    stem = f"rollout-2026-07-05T14-46-18-{uuid}"
    f = archive_home / f"{stem}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": uuid, "cwd": "/proj", "model": "gpt-5"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hello codex"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi from codex"}},
    ]
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    ta.open_archive(str(archive_home))
    res = import_codex_session_incremental(f, source_id=stem)
    return uuid, res.thread_id


def test_status_endpoint(archive_home):
    _seed(archive_home)
    status, ctype, payload = _get("/api/status")
    assert status == 200 and ctype == "application/json"
    assert payload["threads"] == 1 and payload["events"] > 0
    assert payload["fts_indexed"] > 0


def test_health_endpoint(archive_home):
    # Cheap liveness (the family manifest's health URL) — no index survey.
    _seed(archive_home)
    status, ctype, payload = _get("/api/health")
    assert status == 200 and ctype == "application/json"
    assert payload["ok"] is True
    assert payload["home"] == str(archive_home)


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
    assert "[USER" in payload["transcript"] and "hello webview" in payload["transcript"]


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
    # per-message drawer payload: every message carries a timestamped meta; the
    # assistant message folds in the model + request tallies from its api_request events
    assert all(isinstance(m["meta"]["ts"], (str, type(None))) for m in payload["messages"])
    asst = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert "claude-opus-4" in asst["meta"]["models"]
    assert asst["meta"]["requests"] >= 1
    assert {"input", "output", "thinking"} <= asst["meta"]["tokens"].keys()
    # a user message carries just its timestamp — no model/request fields
    user = next(m for m in payload["messages"] if m["role"] == "user")
    assert "models" not in user["meta"]


def test_structured_thread_bad_id_404(archive_home):
    _seed(archive_home)
    status, _, _, _ = route("GET", "/api/thread/nope", {})
    assert status == 404


def test_built_assets_served(archive_home):
    # the vite build emits hashed assets under static/assets/ — they must be served
    # with a js/css content type, not the SPA fallback HTML.
    from pathlib import Path

    from thread_archive._web import server

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


def test_archive_link_resolves_codex_rollout_stem(archive_home):
    # codex source_id is the dash-joined rollout stem, not the colon-joined
    # {project}:{uuid} claude-code shape — the editor still links by the bare uuid.
    uuid, tid = _seed_codex(archive_home)
    status, _, payload = _get("/api/archive-link", id=uuid, source="codex")
    assert status == 200
    assert payload["thread_id"] == tid
    assert payload["url"] == f"/archive/{tid}"


def test_archive_link_resolves_bare_cloth_uuid_without_source(archive_home):
    # the paste-a-uuid-as-thread-id path: cloth stores the bare session uuid as its
    # source_id, and the paster doesn't pass a source — resolution must search every
    # provider, not default to claude-code (which would 404 on a cloth-only id).
    uuid, tid = _seed_cloth(archive_home)
    status, _, payload = _get("/api/archive-link", id=uuid)
    assert status == 200
    assert payload["thread_id"] == tid
    assert payload["url"] == f"/archive/{tid}"


def test_archive_link_source_narrows_to_provider(archive_home):
    # passing source still scopes the lookup: a cloth-only uuid asked for as claude-code
    # resolves to nothing (an editor that knows its harness gets the exact match only).
    uuid, _ = _seed_cloth(archive_home)
    status, _, _ = _get("/api/archive-link", id=uuid, source="claude-code")
    assert status == 404


def test_archive_link_skips_unresolvable_candidates(archive_home):
    # what the codex webview actually hands us: a turn id and a client-created
    # thread id that look exactly like a session uuid and resolve to nothing,
    # ranked ahead of the real one. The caller cannot tell them apart — the
    # archive can, so the first candidate that was really imported wins.
    uuid, tid = _seed_codex(archive_home)
    turn_id = "019f51d0-f529-7f41-8331-adff4d0c9d3b"
    status, _, body, headers = route(
        "GET",
        "/api/archive-link",
        {"id": [turn_id, uuid], "source": ["codex"], "redirect": ["1"]},
    )
    assert status == 302
    assert headers["Location"] == f"/archive/{tid}"


def test_archive_link_candidate_order_wins(archive_home):
    # two real threads among the candidates: the caller's ranking decides, so the
    # thread the user is looking at (ranked first) beats a stale one behind it.
    cloth_uuid, cloth_tid = _seed_cloth(archive_home)
    codex_uuid, codex_tid = _seed_codex(archive_home)
    status, _, payload = _get_multi("/api/archive-link", id=[cloth_uuid, codex_uuid])
    assert status == 200
    assert payload["thread_id"] == cloth_tid
    assert payload["id"] == cloth_uuid
    assert cloth_tid != codex_tid


def test_archive_link_unknown_is_404(archive_home):
    _seed(archive_home)
    status, _, payload = _get("/api/archive-link", id="no-such-uuid")
    assert status == 404
    assert "error" in payload


def test_archive_link_all_candidates_unknown_is_404(archive_home):
    # every id the webview could see was a turn/draft: 404 naming what was tried,
    # not a redirect into some unrelated thread.
    _seed_codex(archive_home)
    status, _, payload = _get_multi("/api/archive-link", id=["no-such-uuid", "also-not-real"])
    assert status == 404
    assert "no-such-uuid" in payload["error"]
    assert "also-not-real" in payload["error"]


def test_archive_link_missing_id_400(archive_home):
    status, _, _, _ = route("GET", "/api/archive-link", {})
    assert status == 400


def test_serve_in_thread_cohosts(archive_home):
    # the watcher's cohost path: a background server answering over a real socket
    import urllib.request

    from thread_archive._web import serve_in_thread

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
