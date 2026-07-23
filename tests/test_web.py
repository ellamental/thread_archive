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


def _seed_many_matches(archive_home, *, n: int):
    """One imported session holding ``n`` distinct user turns that all match
    ``hello`` — enough rows to read a result-limit clamp straight off the page.
    Distinct text per turn, or the route's cross-thread dup fold would collapse
    them into one row."""
    lines = []
    for i in range(n):
        lines.append({"type": "user", "uuid": f"u{i}",
                      "timestamp": f"2026-01-01T10:{i // 60:02d}:{i % 60:02d}Z",
                      "cwd": "/proj",
                      "message": {"role": "user", "content": f"hello turn number {i}"}})
    f = archive_home / "many.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
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


def _seed_demo_harness(archive_home, uuid="27056da6-8578-4a8c-ab90-d634702dc42d"):
    """Seed a thread from a source that records the **bare** session uuid as its
    ``source_id`` (no ``{project}:`` prefix, unlike claude-code). Returns the uuid a
    paster would drop into ``/archive/<uuid>`` and the archive thread id it seeded."""
    from datetime import datetime, timezone

    from thread_archive._store import Event, Thread, get_session

    ta.open_archive(str(archive_home))
    at = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    with get_session() as s:
        t = Thread(name=f"demo-harness:{uuid}", title="demo harness session",
                   thread_type="conversation", source="demo-harness", source_id=uuid,
                   inserted_at=at, updated_at=at)
        s.add(t)
        s.flush()
        s.add(Event(thread_id=t.id, stream_id="s", event_type="user_message_sent",
                    payload={"content": "hello from the demo harness"}, occurred_at=at))
        s.add(Event(thread_id=t.id, stream_id="s", event_type="api_request_completed",
                    payload={"model": "claude-opus-4",
                             "content_blocks": [{"type": "text", "text": "hi from the demo harness"}]},
                    occurred_at=at))
        s.commit()
        return uuid, t.id


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
    assert payload["pipeline"]["ran"] is False
    assert payload["watch_process_alive"] is False
    assert payload["backup_same_device"] is None


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


def test_empty_query_browses(archive_home):
    # Empty query = browse (the MCP thread_search contract): one row per thread
    # by last activity, not an empty result page.
    _seed(archive_home)
    status, _, payload = _get("/api/search", q="   ")
    assert status == 200 and payload["browse"] is True and payload["quality"] is None
    assert len(payload["hits"]) == 1
    row = payload["hits"][0]
    assert row["thread_title"] and row["thread_source"] and row["n_events"] >= 2
    # last activity serializes json-safe like every other hit timestamp
    assert isinstance(row["occurred_at"], (str, type(None)))


def test_browse_honors_source_filter(archive_home):
    _seed(archive_home)
    _seed_demo_harness(archive_home)
    _, _, everything = _get("/api/search", q="")
    assert len(everything["hits"]) == 2
    _, _, payload = _get("/api/search", q="", source="demo-harness")
    assert [h["thread_source"] for h in payload["hits"]] == ["demo-harness"]


def test_search_quality_signal(archive_home):
    # Ranked search carries the MCP header's match signal: a top-hit verdict
    # plus a per-hit K-of-N term count the UI can badge.
    _seed(archive_home)
    _, _, payload = _get("/api/search", q="hello webview")
    quality = payload["quality"]
    assert quality["verdict"] == "strong" and quality["n_terms"] == 2
    assert quality["note"] is None
    assert payload["hits"][0]["term_hits"] == 2


_WRAPPED = "<user_info>sam</user_info><user_query>\nhey grok!\n</user_query>"


def _seed_grok(archive_home):
    """A real Grok session: the operator's prompt wrapped in ``<user_query>`` with
    injected context around it — the truth the importer keeps verbatim.

    Driven through grok's own importer, so the thread's source is genuinely
    ``grok``. Unwrapping is that provider's display policy, so a fixture merely
    *shaped* like this one would prove nothing about it."""
    from thread_archive._importers import import_grok_session_incremental
    from thread_archive._store import init_db

    init_db()
    session_dir = archive_home / "grok-sess"
    session_dir.mkdir()
    f = session_dir / "chat_history.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in (
        {"type": "user", "content": [{"type": "text", "text": _WRAPPED}]},
        {"type": "assistant", "content": "hi there", "tool_calls": []},
    )) + "\n", encoding="utf-8")
    import_grok_session_incremental(f, "grok-sess")


def test_search_snippet_unwraps_user_query(archive_home):
    # The viewer's snippet shows the query span, not the raw <user_query> wrapper
    # + injected context (the reader already unwraps; the result list matches it).
    _seed_grok(archive_home)
    _, _, payload = _get("/api/search", q="hey grok")
    snips = [h["snippet"] for h in payload["hits"]]
    assert any("hey grok!" in s for s in snips)
    assert all("<user_query>" not in s and "<user_info>" not in s for s in snips)


def test_search_snippet_keeps_another_providers_quoted_wrapper(archive_home):
    """A claude-code turn that *quotes* Grok's wrapper keeps its whole text.

    Unwrapping is Grok's display policy, and a turn discussing that policy — a bug
    report, a pasted transcript — is ordinary text. Applied globally the unwrap
    replaces the entire turn with whatever sat inside the tags it happened to
    contain, which on a long turn hides essentially all of it."""
    body = "Here is the bug, in full:\n" + _WRAPPED + "\nEverything after the tag matters too."
    f = archive_home / "cc.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in (
        {"type": "user", "uuid": "c1", "timestamp": "2026-01-01T10:00:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": body}},
        dict(ASSISTANT, uuid="c2"),
    )) + "\n", encoding="utf-8")
    ta.import_path(f)

    _, _, payload = _get("/api/search", q="bug")
    hits = [h for h in payload["hits"] if "bug" in h["snippet"]]
    assert hits, "the claude-code turn should be findable"
    assert any("Here is the bug" in h["snippet"] for h in hits)


def test_search_snippet_is_a_context_window(archive_home):
    # The snippet is the matched line plus one line of context on each side, and the
    # internal numbered `context` field never leaks into the viewer payload.
    body = "line before the hit\nthe unmistakable marker sits here\nline after the hit"
    turn = dict(ASSISTANT, uuid="c1",
                message={"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": body}]})
    f = archive_home / "ctx.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in (USER, turn)) + "\n", encoding="utf-8")
    ta.import_path(f)
    _, _, payload = _get("/api/search", q="unmistakable marker")
    hit = next(h for h in payload["hits"] if "unmistakable marker" in h["snippet"])
    assert hit["snippet"] == body  # exactly the ±1 window (which is the whole 3-line body)
    assert "context" not in hit


def _seed_forks(archive_home, n, text="the identical opening prompt"):
    """n separate sessions that all open with the same line — the fork / fleet
    shape (and what a common opener like "hey grok" looks like in the index).
    Distinct cwd + day per session: same text in the same project on the same day
    is a *resume*, and the continuation detector would fold them into one thread."""
    for i in range(n):
        f = archive_home / f"fork{i}.jsonl"
        turns = [
            dict(USER, uuid=f"fu{i}", cwd=f"/proj{i}",
                 timestamp=f"2026-01-0{i + 1}T10:00:00Z",
                 message={"role": "user", "content": text}),
            dict(ASSISTANT, uuid=f"fa{i}", timestamp=f"2026-01-0{i + 1}T10:00:05Z",
                 message={"role": "assistant", "model": "claude-opus-4",
                          "content": [{"type": "text", "text": f"reply {i}"}]}),
        ]
        f.write_text("\n".join(json.dumps(ln) for ln in turns) + "\n", encoding="utf-8")
        ta.import_path(f)


def test_search_folds_threads_sharing_one_opening_line(archive_home):
    # Four sessions opening with the same prompt collapse to one row carrying the
    # other three, instead of spending the whole result page on the same line.
    _seed_forks(archive_home, 4)
    _, _, payload = _get("/api/search", q="identical opening prompt")
    rows = [h for h in payload["hits"] if "identical opening prompt" in h["snippet"]]
    assert len(rows) == 1
    dups = rows[0]["dup_threads"]
    assert len(dups) == 3
    # Resolved to titles, not bare ids — the reader needs a name to decide.
    assert all(d["thread_id"] and "title" in d for d in dups)
    assert rows[0]["thread_id"] not in [d["thread_id"] for d in dups]


def test_search_keeps_every_hit_within_one_thread(archive_home):
    # The fold is cross-thread only: a thread matching on several of its own turns
    # still lists each one (the viewer groups them into a card).
    f = archive_home / "multi.jsonl"
    turns = [dict(USER, uuid=f"m{i}", timestamp=f"2026-01-01T1{i}:00:00Z",
                  message={"role": "user", "content": f"beacon reading number {i}"})
             for i in range(3)]
    f.write_text("\n".join(json.dumps(ln) for ln in turns) + "\n", encoding="utf-8")
    ta.import_path(f)
    _, _, payload = _get("/api/search", q="beacon reading")
    assert len({h["thread_id"] for h in payload["hits"]}) == 1
    assert len(payload["hits"]) == 3
    assert not any("dup_threads" in h for h in payload["hits"])


def test_threads_endpoint(archive_home):
    _seed(archive_home)
    status, _, payload = _get("/api/threads")
    assert status == 200
    assert len(payload["threads"]) == 1
    t = payload["threads"][0]
    assert t["id"] and t["title"] and "updated_at" in t
    assert t["first_user_message"] == "hello webview"


def test_threads_first_user_message_preview_is_trimmed_and_capped(archive_home):
    content = " \n\t" + "x" * 205 + "\r\n"
    f = archive_home / "long-first-message.jsonl"
    user = dict(
        USER,
        uuid="long-user",
        message={"role": "user", "content": content},
    )
    f.write_text(json.dumps(user) + "\n", encoding="utf-8")
    ta.import_path(f)

    _, _, payload = _get("/api/threads")
    assert payload["threads"][0]["first_user_message"] == "x" * 200


def test_threads_query_filter(archive_home):
    _seed(archive_home)
    # the seeded thread's title won't contain this token
    _, _, payload = _get("/api/threads", q="zzz-nonexistent")
    assert payload["threads"] == []


def _seed_subagent(archive_home):
    """Import a Task-tool subagent transcript the way the watcher does — an
    ``agent-*`` stem, which the importer files as a ``thread_type='system'`` thread."""
    from thread_archive._importers import import_session_incremental

    sub_user = {"type": "user", "uuid": "su1", "timestamp": "2026-01-02T10:00:00Z",
                "sessionId": "parent-sess", "agentId": "agent-web", "cwd": "/proj",
                "message": {"role": "user", "content": "do the subtask"}}
    sub_asst = {"type": "assistant", "uuid": "sa1", "timestamp": "2026-01-02T10:00:05Z",
                "sessionId": "parent-sess", "agentId": "agent-web",
                "message": {"role": "assistant", "model": "claude-opus-4",
                            "content": [{"type": "text", "text": "done"}]}}
    f = archive_home / "agent.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in (sub_user, sub_asst)) + "\n", encoding="utf-8")
    ta.open_archive(str(archive_home))
    return import_session_incremental(f, "proj:agent-web").thread_id


def test_threads_hides_subagents_by_default(archive_home):
    # The recent list is for sessions someone opens by recency; subagent runs
    # (thread_type='system') stay out unless asked for by an explicit types=.
    _seed(archive_home)
    sub_id = _seed_subagent(archive_home)
    _, _, payload = _get("/api/threads")
    ids = [t["id"] for t in payload["threads"]]
    assert sub_id not in ids and len(ids) == 1
    # every row says what it is, so list consumers can badge/filter client-side
    assert payload["threads"][0]["thread_type"] == "conversation"


def test_threads_types_filter_selects_exactly(archive_home):
    _seed(archive_home)
    sub_id = _seed_subagent(archive_home)
    # types=system → only the subagent run
    _, _, payload = _get("/api/threads", types="system")
    assert [t["id"] for t in payload["threads"]] == [sub_id]
    assert payload["threads"][0]["thread_type"] == "system"
    # types=conversation,system → both, newest first
    _, _, payload = _get("/api/threads", types="conversation,system")
    assert len(payload["threads"]) == 2
    assert payload["threads"][0]["id"] == sub_id  # 2026-01-02 beats 2026-01-01


def test_threads_order_by_activity_not_metadata_writes(archive_home):
    # The list orders by the newest event (last activity). A topic-graph write —
    # here a summary — bumps the row's updated_at (the truth checkpoint's
    # dirty-flag) but is not activity and must not re-rank the list.
    _seed(archive_home)
    sub_id = _seed_subagent(archive_home)
    _, _, payload = _get("/api/threads", types="conversation,system")
    ids = [t["id"] for t in payload["threads"]]
    assert ids[0] == sub_id  # newest events (2026-01-02) first
    from .kg_seed import set_thread_summary

    set_thread_summary(ids[1], summary="written much later than its last event")
    _, _, payload = _get("/api/threads", types="conversation,system")
    assert [t["id"] for t in payload["threads"]] == ids  # unchanged
    # and the row's date is the thread's last event, not the summary write
    assert payload["threads"][1]["updated_at"].startswith("2026-01-01")


def test_thread_types_vocabulary(archive_home):
    _seed(archive_home)
    _seed_subagent(archive_home)
    status, _, payload = _get("/api/thread-types")
    assert status == 200
    counts = {t["thread_type"]: t["threads"] for t in payload["types"]}
    assert counts == {"conversation": 1, "system": 1}


def test_read_endpoint(archive_home):
    _seed(archive_home)
    _, _, search = _get("/api/search", q="hello")
    tid = search["hits"][0]["thread_id"]
    status, _, payload = _get(f"/api/read/{tid}")
    assert status == 200
    assert payload["thread_id"] == tid
    assert "[USER" in payload["transcript"] and "hello webview" in payload["transcript"]


def test_status_survey_is_cached(archive_home):
    # The status bar asks on every page load while the survey counts every
    # table, so the route serves a per-home TTL cache. A thread that lands
    # between two requests proves it: the second answer is the first one, not a
    # fresh count.
    from thread_archive._web import server as web_server

    _seed(archive_home)
    first = _get("/api/status")
    assert first[2]["threads"] == 1

    _seed_demo_harness(archive_home)  # a second real thread, mid-TTL
    assert _get("/api/status") == first  # served from cache, not re-counted

    web_server._survey_cache.clear()
    assert _get("/api/status")[2]["threads"] == 2  # a cold survey does see it


def test_survey_cold_fill_is_shared(archive_home):
    # Requests racing the cold fill must wait for it, not each start their own
    # pass: the prewarm and the first page load would otherwise survey the whole
    # archive twice and contend for the same database.
    import threading

    from thread_archive._web import server as web_server

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow():
        calls["n"] += 1
        started.set()
        release.wait(5)
        return {"ok": True}

    results: list[dict] = []

    def go():
        results.append(web_server._survey("probe", slow, ttl=60.0))

    first = threading.Thread(target=go)
    first.start()
    assert started.wait(5), "the first caller never began the survey"
    racers = [threading.Thread(target=go) for _ in range(3)]
    for t in racers:
        t.start()
    release.set()
    for t in (first, *racers):
        t.join(10)

    assert calls["n"] == 1
    assert results == [{"ok": True}] * 4


def test_sources_endpoint(archive_home):
    _seed(archive_home)
    status, _, payload = _get("/api/sources")
    assert status == 200
    [src] = payload["sources"]
    assert src["source"] and src["threads"] == 1


def test_thread_endpoint_provenance(archive_home):
    # The structured read carries the reader header's provenance: session id,
    # first/last event timestamps, and the whole event log's size.
    _seed(archive_home)
    _, _, search = _get("/api/search", q="hello")
    tid = search["hits"][0]["thread_id"]
    status, _, payload = _get(f"/api/thread/{tid}")
    assert status == 200 and payload["thread_id"] == tid
    assert "source_id" in payload
    assert payload["started_at"] and payload["ended_at"]
    assert payload["started_at"] <= payload["ended_at"]
    assert payload["event_count"] >= len(payload["messages"]) > 0


def test_read_bad_id_is_404(archive_home):
    _seed(archive_home)
    status, _, _, _ = route("GET", "/api/read/not-a-number", {})
    assert status == 404


def _seed_topics(archive_home):
    """Seed a conversation plus a small topic graph around it: three linked
    topics (a community), one citation of the conversation's user message.
    Returns ``(topic_a, topic_b, topic_c, conversation_id, cited_event_id)``."""
    from .kg_seed import add_topic_evidence, create_topic, link_threads

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


def test_search_subjects_lens(archive_home):
    # Ranked results name the topic subjects they cluster under, each with its
    # topic id so the UI can pivot into the topic page — the MCP header's
    # subjects line, JSON-shaped.
    a, _, _, _, _ = _seed_topics(archive_home)
    _, _, payload = _get("/api/search", q="hello")
    assert [s["topic_id"] for s in payload["subjects"]] == [a]
    subj = payload["subjects"][0]
    assert subj["title"] == "Graph Theory" and subj["chats"] == 1


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


def test_archive_link_resolves_bare_uuid_without_source(archive_home):
    # the paste-a-uuid-as-thread-id path: some providers store the bare session uuid
    # as their source_id, and the paster doesn't pass a source — resolution must search
    # every provider, not default to claude-code (which would 404 on such an id).
    uuid, tid = _seed_demo_harness(archive_home)
    status, _, payload = _get("/api/archive-link", id=uuid)
    assert status == 200
    assert payload["thread_id"] == tid
    assert payload["url"] == f"/archive/{tid}"


def test_archive_link_source_narrows_to_provider(archive_home):
    # passing source still scopes the lookup: a uuid that exists under one provider,
    # asked for as claude-code, resolves to nothing (an editor that knows its harness
    # gets the exact match only).
    uuid, _ = _seed_demo_harness(archive_home)
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
    demo_uuid, demo_tid = _seed_demo_harness(archive_home)
    codex_uuid, codex_tid = _seed_codex(archive_home)
    status, _, payload = _get_multi("/api/archive-link", id=[demo_uuid, codex_uuid])
    assert status == 200
    assert payload["thread_id"] == demo_tid
    assert payload["id"] == demo_uuid
    assert demo_tid != codex_tid


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
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=5
        ) as response:
            body = response.read()
            headers = response.headers
        assert json.loads(body)["threads"] == 1
        csp = headers["Content-Security-Policy"]
        assert "img-src 'self'" in csp and "object-src 'none'" in csp
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
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


def test_search_limit_clamped(archive_home):
    # limit=-1 would reach SQLite as LIMIT -1 (unlimited); huge values are an
    # unbounded read. Both clamp to [1, 500] before touching the api, so the
    # clamp is readable off the rows a real search comes back with.
    from thread_archive._web import server

    _seed_many_matches(archive_home, n=40)

    status, _, body = _get("/api/search", q="hello", limit=-1)
    assert status == 200
    assert len(body["hits"]) == 1  # lo-clamped to 1, not unlimited

    _, _, body = _get("/api/search", q="hello", limit=999999)
    assert len(body["hits"]) == 40  # hi-clamp holds without erroring…
    # …and the hi bound itself is _int's own contract:
    assert server._int({"limit": ["999999"]}, "limit", 100) == 500

    _, _, body = _get("/api/search", q="hello", limit=35)
    assert len(body["hits"]) == 35  # a value inside the range passes through

    _, _, body = _get("/api/search", q="hello", limit="not-a-number")
    assert len(body["hits"]) == 30  # unparseable → the route's default


def test_threads_limit_clamped(archive_home):
    from thread_archive._web import server

    # Two real threads, then drive the route over real data: an unclamped -1
    # would reach SQLite as LIMIT -1 (unlimited) and return both.
    _seed(archive_home)
    _seed_demo_harness(archive_home)

    status, _, body = _get("/api/threads", limit=-1)
    assert status == 200
    assert len(body["threads"]) == 1  # clamped to lo=1, not unlimited

    status, _, body = _get("/api/threads", limit=999999)
    assert status == 200
    assert len(body["threads"]) == 2  # hi-clamp holds without erroring…
    # …and the hi bound itself is _int's own contract:
    assert server._int({"limit": ["999999"]}, "limit", 100) == 500


def test_error_body_is_generic(tmp_path, monkeypatch, caplog):
    """Exception detail (paths, SQL, query internals) stays server-side.

    Driven by a genuinely unopenable archive — a directory where ``index.db``
    belongs — so the 500 comes out of the real handler over a real SQLAlchemy
    error carrying the home path, not a planted one.
    """
    import urllib.error
    import urllib.request

    from thread_archive._config import ENV_HOME
    from thread_archive._web import server

    broken = tmp_path / "broken-archive"
    (broken / "index.db").mkdir(parents=True)
    monkeypatch.setenv(ENV_HOME, str(broken))

    httpd = server.serve_in_thread(host="127.0.0.1", port=0)
    try:
        port = httpd.server_address[1]
        with caplog.at_level("ERROR", logger="thread_archive._web.server"):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5)
        assert excinfo.value.code == 500
        body = excinfo.value.read()
        excinfo.value.close()
        assert json.loads(body) == {"error": "internal error"}
        assert str(broken).encode() not in body  # no path leaked to the client
        # …while the operator still gets the failure, with its path, in the log.
        assert any("web request failed: /api/status" in r.getMessage()
                   for r in caplog.records)
    finally:
        httpd.shutdown()
        httpd.server_close()
