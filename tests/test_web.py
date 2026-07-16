"""The ``archive web`` surface — the socket-free router over thread_archive._api.

The HTTP adapter is a thin stdlib shim; the logic lives in :func:`route`, which is
pure ``(method, path, params) -> (status, content_type, body, headers)``. These
exercise it directly (no sockets) against a seeded throwaway archive.
"""

from __future__ import annotations

import json

import pytest

from thread_archive import _api as ta
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


def _seed_topics(archive_home):
    """Seed a conversation plus a small curated graph around it: three linked
    topics (a community), one citation of the conversation's user message.
    Returns ``(topic_a, topic_b, topic_c, conversation_id, cited_event_id)``."""
    from thread_archive._knowledge import add_topic_evidence, create_topic, link_threads

    _seed(archive_home)
    _, _, search = _get("/api/search", q="hello")
    conv_id = search["hits"][0]["thread_id"]
    event_id = search["hits"][0]["event_id"]
    a = create_topic("Graph Theory", "nodes and edges", topic_kind="concept")["topic_id"]
    b = create_topic("Leiden Communities", "community detection")["topic_id"]
    c = create_topic("PageRank", None, topic_kind="concept")["topic_id"]
    link_threads(a, b, "related", strength=0.9)
    link_threads(a, c, "implements", strength=0.5)
    link_threads(a, conv_id, "works_on", evidence="came up here")
    add_topic_evidence(a, event_id, conv_id, "hello webview")
    return a, b, c, conv_id, event_id


def test_topics_endpoint(archive_home):
    a, b, c, _, _ = _seed_topics(archive_home)
    status, ctype, payload = _get("/api/topics")
    assert status == 200 and ctype == "application/json"
    assert {t["id"] for t in payload["topics"]} == {a, b, c}
    assert payload["graph"]["available"] is True and payload["graph"]["nodes"] == 3
    by_id = {t["id"]: t for t in payload["topics"]}
    # a carries the citation and both topic links; its topic↔conversation link
    # is not a graph edge, so link_count counts only the two topic peers.
    assert by_id[a]["evidence_count"] == 1 and by_id[a]["link_count"] == 2
    assert by_id[b]["evidence_count"] == 0 and by_id[b]["link_count"] == 1
    assert by_id[a]["topic_kind"] == "concept"
    assert by_id[a]["community"] is not None
    # ranked: a (the hub) has the highest pagerank, so it lists first
    assert payload["topics"][0]["id"] == a


def test_topics_query_filter(archive_home):
    _, b, _, _, _ = _seed_topics(archive_home)
    _, _, payload = _get("/api/topics", q="leiden")
    assert [t["id"] for t in payload["topics"]] == [b]
    _, _, none = _get("/api/topics", q="zzz-nonexistent")
    assert none["topics"] == []


def test_topic_detail_endpoint(archive_home):
    a, b, c, conv_id, event_id = _seed_topics(archive_home)
    status, _, payload = _get(f"/api/topic/{a}")
    assert status == 200
    assert payload["title"] == "Graph Theory" and payload["topic_kind"] == "concept"
    assert payload["description"] == "nodes and edges"
    assert payload["archived"] is False
    assert payload["graph"]["degree"] == 2
    links = {(li["direction"], li["other_id"]): li for li in payload["links"]}
    assert links[("out", b)]["link_type"] == "related"
    assert links[("out", c)]["link_type"] == "implements"
    conv_link = links[("out", conv_id)]
    assert conv_link["other_type"] == "conversation" and conv_link["evidence"] == "came up here"
    [ev] = payload["evidence"]
    assert ev["event_id"] == event_id and ev["thread_id"] == conv_id
    assert ev["quote"] == "hello webview" and ev["thread_title"]
    # peers: the other community members, and never the topic itself
    peer_ids = {p["thread_id"] for p in payload["peers"]}
    assert a not in peer_ids and peer_ids <= {b, c}


def test_topic_detail_incoming_link_direction(archive_home):
    a, b, _, _, _ = _seed_topics(archive_home)
    _, _, payload = _get(f"/api/topic/{b}")
    [li] = [li for li in payload["links"] if li["other_id"] == a]
    assert li["direction"] == "in" and li["link_type"] == "related"


def test_topic_detail_conversation_id_is_404(archive_home):
    _, _, _, conv_id, _ = _seed_topics(archive_home)
    status, _, payload = _get(f"/api/topic/{conv_id}")
    assert status == 404 and "no topic" in payload["error"]


def test_topic_detail_bad_id_is_404(archive_home):
    _seed(archive_home)
    status, _, _, _ = route("GET", "/api/topic/nope", {})
    assert status == 404


def test_archived_topic_hidden_from_list_but_readable(archive_home):
    from thread_archive._knowledge import archive_topic

    a, b, c, _, _ = _seed_topics(archive_home)
    archive_topic(c)
    _, _, listing = _get("/api/topics")
    assert {t["id"] for t in listing["topics"]} == {a, b}
    status, _, payload = _get(f"/api/topic/{c}")
    assert status == 200 and payload["archived"] is True


def _tree_ids(node):
    return {node["id"], *(i for c in node["children"] for i in _tree_ids(c))}


def test_topic_tree_from_part_of_and_contains(archive_home):
    from thread_archive._knowledge import create_topic, link_threads

    a, b, c, conv_id, _ = _seed_topics(archive_home)
    d = create_topic("Centrality Measures")["topic_id"]
    # b and c hang under a: one edge per hierarchy spelling (child part-of parent,
    # parent contains child); d under c gives depth 2. The conversation edge from
    # _seed_topics (a works_on conv) and a hierarchy edge to a conversation must
    # never enter the tree.
    link_threads(b, a, "part-of")
    link_threads(a, c, "contains")
    link_threads(d, c, "part-of")
    link_threads(conv_id, a, "part-of")
    status, _, payload = _get("/api/topics/tree")
    assert status == 200
    [root] = payload["roots"]
    assert root["id"] == a and _tree_ids(root) == {a, b, c, d}
    by_title = {n["title"]: n for n in root["children"]}
    assert set(by_title) == {"Leiden Communities", "PageRank"}
    assert [n["id"] for n in by_title["PageRank"]["children"]] == [d]
    assert payload["topics_in_hierarchy"] == 4 and payload["topics_total"] == 4


def test_topic_tree_cycle_is_cut(archive_home):
    from thread_archive._knowledge import link_threads

    a, b, _, _, _ = _seed_topics(archive_home)
    # a mutual part-of pair: neither is a root, but the forest must not hang or
    # recurse forever — the pair simply contributes no root
    link_threads(a, b, "part-of")
    link_threads(b, a, "part-of")
    status, _, payload = _get("/api/topics/tree")
    assert status == 200
    assert all(a not in _tree_ids(r) for r in payload["roots"])


def test_topic_tree_multi_parent_child_appears_under_each(archive_home):
    from thread_archive._knowledge import create_topic, link_threads

    a, b, c, _, _ = _seed_topics(archive_home)
    d = create_topic("Shared Child")["topic_id"]
    link_threads(d, a, "part-of")
    link_threads(d, b, "part-of")
    link_threads(c, a, "part-of")  # give a more weight so root order is deterministic
    _, _, payload = _get("/api/topics/tree")
    roots = {r["id"]: r for r in payload["roots"]}
    assert set(roots) == {a, b}
    assert d in _tree_ids(roots[a]) and d in _tree_ids(roots[b])
    # heavier subtree lists first
    assert payload["roots"][0]["id"] == a


def test_topic_tree_empty_without_hierarchy_links(archive_home):
    _seed_topics(archive_home)  # related/implements/works_on links only
    status, _, payload = _get("/api/topics/tree")
    assert status == 200
    assert payload["roots"] == [] and payload["topics_in_hierarchy"] == 0
    assert payload["topics_total"] == 3


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


@pytest.mark.integration
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


def test_non_loopback_bind_refused(monkeypatch):
    # the viewer is unauthenticated full read; a stray --web-host must not expose it
    from thread_archive._web import serve_in_thread

    monkeypatch.delenv("THREAD_ARCHIVE_WEB_NONLOCAL", raising=False)
    for host in ("0.0.0.0", "192.168.1.10"):
        with pytest.raises(ValueError, match="non-loopback"):
            serve_in_thread(host=host, port=0)


def test_host_header_parsing():
    # DNS-rebinding defense: only loopback names pass, port-stripped, including the
    # bracketed IPv6 form; absent/empty/malformed Hosts are rejected.
    from thread_archive._web.server import _host_allowed

    for host in ("localhost", "localhost:8787", "127.0.0.1", "127.0.0.1:8787",
                 "::1", "[::1]", "[::1]:8787", "LOCALHOST:8787"):
        assert _host_allowed(host), host
    for host in (None, "", "evil.example", "evil.example:8787", "localhost.evil.example",
                 "10.0.0.5:8787", "[::1", "[2001:db8::1]:8787"):
        assert not _host_allowed(host), host


@pytest.mark.integration
def test_rebound_host_rejected(archive_home, monkeypatch):
    # a DNS-rebound page reaches 127.0.0.1 but its Host is the attacker's domain —
    # the adapter must 403 it before routing; real loopback Hosts still pass.
    import http.client

    from thread_archive._web import serve_in_thread

    _seed(archive_home)
    ta.open_archive(str(archive_home))
    monkeypatch.delenv("THREAD_ARCHIVE_WEB_NONLOCAL", raising=False)
    httpd = serve_in_thread(host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]

        def status_for(host):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                if host is None:
                    conn.putrequest("GET", "/api/status", skip_host=True)
                    conn.endheaders()
                else:
                    conn.request("GET", "/api/status", headers={"Host": host})
                return conn.getresponse().status
            finally:
                conn.close()

        assert status_for(f"localhost:{port}") == 200
        assert status_for(f"[::1]:{port}") == 200
        assert status_for("evil.example") == 403
        assert status_for(None) == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_search_limit_clamped(archive_home, monkeypatch):
    # limit=-1 would reach SQLite as LIMIT -1 (unlimited); huge values are an
    # unbounded read. Both clamp to [1, 500] before touching the api.
    from thread_archive._web import server

    _seed(archive_home)
    seen = {}

    def fake_search(q, **kwargs):
        seen["limit"] = kwargs["limit"]
        return []

    monkeypatch.setattr(server.api, "search", fake_search)
    status, _, _ = _get("/api/search", q="hello", limit=-1)
    assert status == 200 and seen["limit"] == 1
    _get("/api/search", q="hello", limit=999999)
    assert seen["limit"] == 500
    _get("/api/search", q="hello", limit=40)
    assert seen["limit"] == 40
    _get("/api/search", q="hello", limit="not-a-number")
    assert seen["limit"] == 30  # the default


def test_threads_limit_clamped(archive_home):
    from thread_archive._web import server

    # Two real threads, then drive the route over real data: an unclamped -1
    # would reach SQLite as LIMIT -1 (unlimited) and return both.
    _seed(archive_home)
    _seed_cloth(archive_home)

    status, _, body = _get("/api/threads", limit=-1)
    assert status == 200
    assert len(body["threads"]) == 1  # clamped to lo=1, not unlimited

    status, _, body = _get("/api/threads", limit=999999)
    assert status == 200
    assert len(body["threads"]) == 2  # hi-clamp holds without erroring…
    # …and the hi bound itself is _int's own contract:
    assert server._int({"limit": ["999999"]}, "limit", 100) == 500


def test_error_body_is_generic(monkeypatch):
    # exception detail (paths, SQL, query internals) stays server-side
    import urllib.error
    import urllib.request

    from thread_archive._web import server

    def boom(method, path, params):
        raise RuntimeError("secret detail: /Users/somebody/private.db")

    monkeypatch.setattr(server, "route", boom)
    httpd = server.serve_in_thread(host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5)
        assert excinfo.value.code == 500
        assert json.loads(excinfo.value.read()) == {"error": "internal error"}
    finally:
        httpd.shutdown()
        httpd.server_close()
