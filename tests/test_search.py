"""Search + read over imported conversations.

- import a fixture corpus
- search returns stable, expected hits (and the query-mode battery works)
- read reconstructs a full conversation
- reindex preserves search results
"""

from __future__ import annotations

import json

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import read_thread, search
from thread_archive._store import _base, get_engine, init_db
from thread_archive._truth import jsonl_log, reindex


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


def test_source_filter(archive_home) -> None:
    """``source`` restricts to threads of the named provider(s) — the lexical arm
    resolves it through an indexed subquery on threads.source."""
    from sqlalchemy import update

    from thread_archive._store import Thread, use_session

    _seed_corpus(archive_home)
    # both fixture threads import as 'claude-code'; relabel the db thread to 'cursor'
    db_tid = search("database")[0]["thread_id"]
    with use_session() as s:
        s.execute(update(Thread).where(Thread.id == db_tid).values(source="cursor"))
        s.commit()

    # restrict to claude-code → the relabeled 'database' thread drops out
    cc = search("authentication OR database", source=["claude-code"])
    assert cc and all(h["thread_id"] != db_tid for h in cc)

    # restrict to cursor → only the relabeled thread
    cur = search("authentication OR database", source=["cursor"])
    assert cur and all(h["thread_id"] == db_tid for h in cur)

    # both providers → spans both threads
    both = search("authentication OR database", source=["claude-code", "cursor"])
    assert {h["thread_id"] for h in both} == {tid for tid in (cc[0]["thread_id"], db_tid)}

    # a provider nobody has → empty (subquery yields no ids, not invalid SQL)
    assert search("authentication", source=["chatgpt"]) == []


def test_read_reconstructs_conversation(archive_home) -> None:
    _seed_corpus(archive_home)
    hits = search("authentication", content_types=["user"])
    thread_id = hits[0]["thread_id"]

    # default view = user turns only
    user_view = read_thread(thread_id)
    assert "[USER" in user_view
    assert "authentication work in the login flow" in user_view
    assert "[ASSISTANT" not in user_view  # assistant suppressed in the user view

    # chat view surfaces the assistant's visible text
    chat_view = read_thread(thread_id, mode="chat")
    assert "[ASSISTANT" in chat_view
    assert "session token after login" in chat_view


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
    # The full-phrase doc surfaces exactly once (the OR tier may append docs
    # holding only 'session'; the duplicate-indexing bug would repeat THIS one).
    exact = [h for h in hits if "session token" in (h["full_content"] or "").lower()]
    assert len(exact) == 1
    assert exact[0]["content_type"] == "text"
    assert hits[0] is exact[0]  # and the full match outranks the partials


def test_empty_query_returns_nothing(archive_home) -> None:
    _seed_corpus(archive_home)
    # browse mode (empty query) isn't a lexical search here → no hits
    assert search("") == []


# --- match-quality signal ---------------------------------------------------

def test_quality_verdict_logic() -> None:
    from thread_archive._retrieval.format import _search_quality, term_hit_count

    # term matching: ≥4 chars substring, <4 chars word-boundary, each term once
    assert term_hit_count("the authentication flow", ["authentication"]) == 1
    assert term_hit_count("goodbye world", ["go"]) == 0       # boundary, not substring
    assert term_hit_count("let's go now", ["go"]) == 1
    assert term_hit_count("auth auth auth", ["auth"]) == 1     # distinct count

    assert _search_quality(0, 0, False) is None                # no terms → no verdict
    assert _search_quality(9, 3, True)[0] == "semantic"        # rerank wins outright
    assert _search_quality(0, 2, False)[0] == "weak"           # zero overlap
    assert _search_quality(2, 3, False)[0] == "strong"         # ceil(2/3·3)=2
    assert _search_quality(1, 3, False)[0] == "partial"
    assert _search_quality(2, 2, False)[0] == "strong"


def test_quality_signal_rendered(archive_home) -> None:
    from thread_archive._retrieval import format_results

    _seed_corpus(archive_home)
    # rerank=False forces the lexical verdict (a 2-term query auto-reranks to
    # 'semantic' when the cross-encoder is installed).
    strong = format_results(search("authentication login", rerank=False), "authentication login")
    assert "quality=strong" in strong and "2/2" in strong

    # 'authenticated' stems to the same root as 'authentication' (FTS5 porter), so
    # it MATCHES — but the literal term is absent, so K=0 → weak, flagged semantic.
    weak = format_results(search("authenticated"), "authenticated")
    assert "quality=weak" in weak and "0/1" in weak and "(semantic)" in weak


# --- output modes -----------------------------------------------------------

def test_output_count_and_linkable(archive_home) -> None:
    import json as _json

    from thread_archive._retrieval import format_results

    _seed_corpus(archive_home)
    cnt = format_results(search("get_session", output="count"), "get_session", output="count")
    assert cnt.startswith("Total:") and "corpus:" in cnt

    lnk = format_results(search("authentication", output="linkable"), "authentication", output="linkable")
    arr = _json.loads(lnk)
    assert arr and all({"event_id", "thread_id", "preview"} <= set(e) for e in arr)


# --- sort -------------------------------------------------------------------

def test_sort_oldest(archive_home) -> None:
    _seed_corpus(archive_home)  # auth thread @ 2026-01-01, db thread @ 2026-01-02
    hits = search("authentication OR database", sort="oldest")
    assert hits
    times = [str(h.get("occurred_at") or "") for h in hits]
    assert times == sorted(times)                  # chronological, oldest first
    assert hits[0]["occurred_at"] <= hits[-1]["occurred_at"]


# --- OR-fallback tier -------------------------------------------------------

def test_natural_language_or_fallback(archive_home) -> None:
    """A conversational query whose terms never co-occur still finds the docs
    holding *some* meaningful term — the strict all-terms MATCH comes up short and
    the OR tier tops the pool up. Boolean / quoted queries keep their exact
    semantics (no fallback)."""
    _seed_corpus(archive_home)

    # 'authentication' exists, 'zzzmissing' nowhere: strict AND is zero-recall.
    hits = search("authentication zzzmissing")
    assert hits and any("authentication" in (h["full_content"] or "").lower() for h in hits)

    # Explicit boolean AND means AND — no fallback.
    assert search("authentication AND zzzmissing") == []
    # A quoted phrase means that phrase — no fallback.
    assert search('"authentication zzzmissing"') == []
    # Stopwords alone don't gate recall: only 'authentication' is meaningful here.
    assert search("how did we do the authentication zzzmissing")

    # count stays strict: partial matches must not inflate the tally.
    assert search("authentication zzzmissing", output="count") == []


def test_sort_oldest_is_strict_and_chronological(archive_home) -> None:
    """oldest = earliest strict matches; the OR tier sits out so a partial match
    can't leapfrog the true first mention, and the lexical scan itself runs
    oldest-first (pool holds the earliest rows, not bm25's favourites)."""
    from thread_archive._retrieval import search_events

    _seed_corpus(archive_home)
    assert search("authentication zzzmissing", sort="oldest") == []

    ordered = search_events("authentication OR database", oldest_first=True)
    times = [str(h["occurred_at"] or "") for h in ordered]
    assert ordered and times == sorted(times)


# --- context_lines ----------------------------------------------------------

def test_context_lines(archive_home) -> None:
    from thread_archive._retrieval._context import extract_context_lines

    block = extract_context_lines("line one\nhas the AUTH term\nline three\nfour", "auth", 1)
    rows = block.split("\n")
    assert rows[0].startswith("    1: ")           # context line, numbered (3sp prefix + " 1:")
    assert rows[1].startswith(">>> 2: ")           # matched line, marked
    assert rows[2].startswith("    3: ")

    _seed_corpus(archive_home)
    from thread_archive._retrieval import format_results
    out = format_results(search("authentication", content_types=["user"], context_lines=2),
                         "authentication")
    assert ">>>" in out                            # context block replaced the snippet


# --- context_events ---------------------------------------------------------

def test_context_events(archive_home) -> None:
    from thread_archive._retrieval._context import parse_context_events_spec

    assert parse_context_events_spec("3") == (3, 3, None)
    assert parse_context_events_spec("0:1") == (0, 1, None)
    assert parse_context_events_spec("2:0:user,text") == (2, 0, ["user", "text"])

    _seed_corpus(archive_home)
    hits = search("authentication", content_types=["user"], context_events="0:1")
    assert hits
    ce = hits[0].get("context_events")
    assert ce and ce.get("after")                  # the next event (assistant reply)
    assert any("session token" in (e.get("content") or "") for e in ce["after"])


# --- startswith -------------------------------------------------------------

def test_startswith(archive_home) -> None:
    _seed_corpus(archive_home)  # asst text begins "Authentication uses a session token..."
    hits = search("", startswith="Authentication uses")
    assert hits and all((h["full_content"] or "").startswith("Authentication uses") for h in hits)

    # query text is ignored under a structural prefix scan
    assert search("zzznomatch", startswith="Authentication uses")

    # LIKE wildcards in the prefix are escaped → literal, so '%' matches nothing extra
    assert search("", startswith="Authentication%") == []


def test_match_rank_order_streams_without_external_sort(archive_home) -> None:
    """The MATCH passes must sort via FTS5's internal rank order (``ORDER BY
    rank``), not an expression like ``bm25(event_search)``: an expression sort
    builds a temp B-tree and evaluates the SELECT list — snippet() above all —
    for every matching row, which over a ~1M-doc index turns a broad OR query
    into seconds. The plan shape is the contract: no external sort step."""
    from sqlalchemy import text as sa_text

    from thread_archive._retrieval.fts import _RANK_EXPR
    from thread_archive._store import get_session

    _seed_corpus(archive_home)
    sql = (
        "EXPLAIN QUERY PLAN "
        "SELECT event_id, snippet(event_search, 0, '', '', ' … ', 12) AS snippet, "
        "content AS full_content FROM event_search "
        "WHERE event_search MATCH '\"authentication\" OR \"login\"' "
        "AND thread_id NOT IN (SELECT id FROM threads WHERE exclude_from_search) "
        "ORDER BY " + _RANK_EXPR + " LIMIT 200"
    )
    with get_session() as s:
        plan = " | ".join(str(row) for row in s.execute(sa_text(sql)))
    assert "TEMP B-TREE" not in plan, plan


def test_substring_like_pass_only_runs_on_pool_shortfall(archive_home) -> None:
    """The code-shape substring LIKE is a full-table scan, so it only runs when
    the phrase-MATCH pass left the candidate pool short. Saturated pool → the
    within-token-substring-only doc stays unreachable; short pool → the LIKE
    still catches it (the case the pass exists for)."""
    from thread_archive._retrieval import search_events

    init_db()
    f = archive_home / "code.jsonl"
    _write_cc(f, _cc_turn("u1", "a1", "please call get_session for the pool",
                          "the megaget_sessionizer helper wraps it", 1))
    import_session_incremental(f, "proj:code")

    # Pool short of limit: both the exact-token phrase hit and the LIKE-only
    # within-token substring hit ("mega·get_session·izer") surface.
    contents = {h["full_content"] for h in search_events("get_session", limit=10)}
    assert any("call get_session" in c for c in contents)
    assert any("megaget_sessionizer" in c for c in contents)

    # Pool already full from the phrase pass: the LIKE scan is skipped, so the
    # substring-only doc can't displace or extend a saturated pool.
    hits = search_events("get_session", limit=1)
    assert len(hits) == 1
    assert "megaget_sessionizer" not in (hits[0]["full_content"] or "")
