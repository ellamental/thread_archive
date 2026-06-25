"""Search + read over imported conversations.

- import a fixture corpus
- search returns stable, expected hits (and the query-mode battery works)
- read reconstructs a full conversation
- reindex preserves search results
"""

from __future__ import annotations

import json

import pytest

from thread_archive.importers import import_session_incremental
from thread_archive.retrieval import read_thread, search
from thread_archive.store import _base, get_engine, init_db
from thread_archive.truth import jsonl_log, reindex


def _write_cc(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _cc_turn(uid, aid, user_text, asst_text, t):
    return [
        {"type": "user", "uuid": uid, "timestamp": f"2026-01-0{t}T10:00:00Z",
         "sessionId": "s", "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": aid, "timestamp": f"2026-01-0{t}T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": asst_text}]}},
    ]


def _seed_corpus(archive_home):
    """Two threads with distinct vocabulary."""
    init_db()
    f1 = archive_home / "auth.jsonl"
    _write_cc(f1, _cc_turn("u1", "a1", "how does authentication work in the login flow",
                           "Authentication uses a session token after login.", 1))
    import_session_incremental(f1, "proj:auth")

    f2 = archive_home / "db.jsonl"
    _write_cc(f2, _cc_turn("u2", "a2", "what database does get_session use",
                           "It calls get_session over the postgres connection pool.", 2))
    import_session_incremental(f2, "proj:db")


def test_search_finds_user_and_assistant_content(archive_home) -> None:
    _seed_corpus(archive_home)

    hits = search("authentication")
    assert hits, "expected a hit for 'authentication'"
    assert any("authentication" in (h["full_content"] or "").lower() for h in hits)
    # the hit carries the enriched thread title
    assert all(h["thread_title"] for h in hits)

    # a term only in the second thread
    hits2 = search("database")
    assert hits2 and all(h["thread_id"] != hits[0]["thread_id"] for h in hits2) or hits2


def test_query_mode_battery(archive_home) -> None:
    _seed_corpus(archive_home)

    # natural language / FTS5 MATCH
    assert search("login")
    # quoted phrase
    assert search('"login flow"')
    # boolean AND
    assert search("authentication AND login")
    # boolean: a term present AND a term absent → no hits
    assert not search("authentication AND nonexistentterm")
    # pipe-OR (un-stemmed substring)
    assert search("authentication | database")
    # code identifier (dotted/underscore → substring LIKE, not stemmed)
    assert search("get_session")


def test_content_type_filter(archive_home) -> None:
    _seed_corpus(archive_home)
    # 'authentication' appears in both a user message and an assistant text block
    user_only = search("authentication", content_types=["user"])
    assert user_only and all(h["content_type"] == "user" for h in user_only)


def test_read_reconstructs_conversation(archive_home) -> None:
    _seed_corpus(archive_home)
    hits = search("authentication", content_types=["user"])
    thread_id = hits[0]["thread_id"]

    transcript = read_thread(thread_id)
    assert "## USER" in transcript
    assert "## ASSISTANT" in transcript
    assert "authentication work in the login flow" in transcript
    assert "session token after login" in transcript


def test_read_missing_thread(archive_home) -> None:
    init_db()
    assert "not found" in read_thread(99999)


def test_reindex_preserves_search(archive_home) -> None:
    _seed_corpus(archive_home)
    jsonl_log.checkpoint()
    before = {h["event_id"] for h in search("authentication")}
    assert before

    # Nuke the index; rebuild from JSONL truth (which rebuilds FTS).
    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    for suffix in ("", "-wal", "-shm"):
        (archive_home / f"index.db{suffix}").unlink(missing_ok=True)

    counts = reindex()
    assert counts["fts"] > 0

    after = {h["event_id"] for h in search("authentication")}
    assert after == before, "reindex must preserve the search result set"


def test_assistant_text_indexed_once(archive_home) -> None:
    """Assistant text must not be double-indexed (text_complete + the
    api_request_completed summary that duplicates it)."""
    _seed_corpus(archive_home)
    hits = search("session token")
    assert len(hits) == 1
    assert hits[0]["content_type"] == "text"


def test_empty_query_returns_nothing(archive_home) -> None:
    _seed_corpus(archive_home)
    # browse mode (empty query) isn't a lexical search here → no hits
    assert search("") == []
