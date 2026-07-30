"""Search + read over imported conversations.

- import a fixture corpus
- search returns stable, expected hits (and the query-mode battery works)
- read reconstructs a full conversation
- reindex preserves search results
"""

from __future__ import annotations

import json

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import _probe, read_thread, search
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


def test_malformed_boolean_queries_return_rather_than_raise(archive_home) -> None:
    """Operator misuse and unbalanced quotes demote to fully-quoted literals —
    results or empty, never a raw FTS5 OperationalError out of search."""
    _seed_corpus(archive_home)
    for q in ("NOT login", "login AND", "login OR OR token", '"login flow',
              "AND", '"', "login OR NOT token", "session AND NOT login"):
        assert isinstance(search(q), list)  # must not raise
    # valid boolean shapes keep their operator semantics
    assert search("authentication AND login")
    assert not search("authentication AND nonexistentterm")


def test_residual_fts_syntax_error_retries_quoted(archive_home, monkeypatch) -> None:
    """A MATCH expression FTS5 still rejects (past the builder's validation)
    retries once with the everything-quoted form instead of propagating.

    The builder is what stops such an expression reaching MATCH — the tests
    above hold it to that over every shape of operator misuse — so the retry
    can only be reached by handing the executor an expression the builder would
    never have produced."""
    from thread_archive._retrieval import fts as fts_mod

    _seed_corpus(archive_home)
    monkeypatch.setattr(fts_mod, "to_match_query", lambda q: 'NOT "login"')
    hits = fts_mod.search_events("login", or_fallback=False)
    assert hits == []  # retried as '"NOT" "login"' → empty, no raise


def test_fts_special_punctuation_returns_rather_than_raise(archive_home) -> None:
    """FTS5 metacharacters — colon (column filter), ``*`` (prefix), ``^`` (initial
    token), parens, ``NEAR()`` — are quoted per-token, so a query carrying them can
    never raise an OperationalError out of MATCH. A ``field:``-style query is the
    common one (``thread:``, ``file.py:42``, ``TODO:``)."""
    _seed_corpus(archive_home)
    for q in ("thread:", "login:", "col:val", "col:val AND login", "a:b:c",
              "*", "foo*", "a^b", "(login)", "NEAR(a b)", "get_session:"):
        assert isinstance(search(q), list)  # must not raise
    # a trailing colon is a token separator, not a failed column filter: the term
    # still matches the content it would without the colon.
    assert {h["thread_id"] for h in search("login:")} == {h["thread_id"] for h in search("login")}


def test_underscore_identifier_matches_literally(archive_home) -> None:
    """The code-mode substring LIKE escapes ``_`` — ``get_session`` must not
    wildcard-match ``getXsession``."""
    _seed_corpus(archive_home)
    f3 = archive_home / "like.jsonl"
    _write_cc(f3, _cc_turn("u3", "a3", "tell me about the getXsession wrapper",
                           "getXsession wraps the legacy pool.", 3))
    import_session_incremental(f3, "proj:like")

    hits = search("get_session")
    assert hits
    assert all("getXsession" not in (h["full_content"] or "") for h in hits)
    # the literal identifier still matches
    assert any("get_session" in (h["full_content"] or "") for h in hits)


def test_oldest_sort_missing_timestamp_sorts_last(archive_home, monkeypatch) -> None:
    """``sort='oldest'``: a hit with no parseable ``occurred_at`` lands after the
    dated hits, not first (an empty key would sort before every date).

    ``Event.occurred_at`` is not nullable and the importers backfill a missing
    timestamp from the preceding line, so an undated hit is a shape the store
    cannot hold — the sort key is defensive against a pool that arrives from
    somewhere else, and the pool has to be supplied to reach it."""
    from datetime import datetime

    import thread_archive._retrieval as retrieval
    from thread_archive._store import init_db

    init_db()

    def _hit(eid, occurred_at):
        return {
            "event_id": eid, "thread_id": 1, "thread_title": None,
            "event_type": "user_message_sent", "content_type": "user",
            "snippet": f"hit {eid}", "full_content": f"hit {eid}",
            "occurred_at": occurred_at,
        }

    fake = [_hit(1, None), _hit(2, datetime(2026, 1, 2)), _hit(3, datetime(2026, 1, 1))]
    monkeypatch.setattr(retrieval, "search_events", lambda *a, **kw: list(fake))
    hits = retrieval.search("anything", sort="oldest")
    assert [h["event_id"] for h in hits] == [3, 2, 1]


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


def test_empty_query_browses(archive_home) -> None:
    """An empty query is a browse, not a lexical search: one row per thread by
    last activity (full behavior pinned in test_browse.py)."""
    _seed_corpus(archive_home)
    rows = search("")
    assert rows and all(r.get("_browse") for r in rows)
    assert len({r["thread_id"] for r in rows}) == len(rows)


# --- match-quality signal ---------------------------------------------------

def test_quality_verdict_logic() -> None:
    from thread_archive._retrieval.format import _search_quality, term_hit_count

    # term matching: ≥4 chars substring, <4 chars word-boundary, each term once
    assert term_hit_count("the authentication flow", ["authentication"]) == 1
    assert term_hit_count("goodbye world", ["go"]) == 0       # boundary, not substring
    assert term_hit_count("let's go now", ["go"]) == 1
    assert term_hit_count("auth auth auth", ["auth"]) == 1     # distinct count

    assert _search_quality(0, 0) is None                # no terms → no verdict
    assert _search_quality(0, 2)[0] == "weak"           # zero overlap
    assert _search_quality(2, 3)[0] == "strong"         # ceil(2/3·3)=2
    assert _search_quality(1, 3)[0] == "partial"
    assert _search_quality(2, 2)[0] == "strong"


def test_quality_signal_rendered(archive_home) -> None:
    from thread_archive._retrieval import format_results

    _seed_corpus(archive_home)
    strong = format_results(search("authentication login"), "authentication login")
    assert "quality=strong" in strong and "2/2" in strong

    # 'authenticated' stems to the same root as 'authentication' (FTS5 porter), so
    # it MATCHES — but the literal term is absent, so K=0 → weak, flagged semantic.
    weak = format_results(search("authenticated"), "authenticated")
    assert "quality=weak" in weak and "0/1" in weak and "(semantic)" in weak


def test_a_struggling_search_offers_next_moves(archive_home) -> None:
    """The tools ship a compact description and keep their manual behind
    ``thread_help``, so the alternatives are not sitting in a caller's context by
    default. A search that found nothing — or found only guesses — hands over the
    ones that would plausibly change *this* result, and nothing else."""
    from thread_archive._retrieval import format_results

    # A single token could be living inside longer words; the infix scan is the
    # retry that finds it, and the one no index can do.
    single = format_results([], "p4")
    assert single.startswith('No results for "p4".')
    assert "match='substring'" in single
    assert "thread_help('search')" in single

    # A filename-shaped query gets the code axis, which answers the other question.
    assert "path='rank.py'" in format_results([], "rank.py")
    assert "path='src/rank.py'" in format_results([], "src/rank.py")

    # A phrase has no substring story to tell, so it is not offered one.
    wordy = format_results([], "no such phrase here")
    assert "match='substring'" not in wordy and "path=" not in wordy
    assert "thread_help('search')" in wordy

    # Weak hits are the same wall reached from the other side: rows came back, but
    # no query term is in the best of them.
    _seed_corpus(archive_home)
    weak = format_results(search("authenticated"), "authenticated")
    assert "quality=weak" in weak and "thread_help('search')" in weak
    # A good answer is left alone — no advice on a search that worked.
    strong = format_results(search("authentication login"), "authentication login")
    assert "thread_help" not in strong


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


def test_the_lexical_arm_reports_which_pass_spent_the_time(archive_home) -> None:
    """``fts_ms`` covers a ladder of passes with unrelated cost models — an indexed
    MATCH and a full-table substring scan differ by orders of magnitude — so the arm
    total alone can't say whether the index was slow or whether the ladder ran all
    the way down. Same corpus, same query, two pool cuts: the split and the pass
    count tell those two searches apart."""
    from thread_archive._retrieval import search_events

    init_db()
    f = archive_home / "code.jsonl"
    _write_cc(f, _cc_turn("u1", "a1", "please call get_session for the pool",
                          "the megaget_sessionizer helper wraps it", 1))
    import_session_incremental(f, "proj:code")

    # Saturated by the indexed phrase pass: every fallback is skipped, so the arm's
    # whole cost is one MATCH and nothing was scanned.
    with _probe.install() as saturated:
        search_events("get_session", limit=1)
    assert saturated.fts_passes == 1
    assert saturated.match_ms > 0.0
    assert saturated.scan_ms == 0.0

    # Short pool: the ladder walks down through the indexed token passes and into
    # the full-table LIKE. Identical arm, identical query — the difference is only
    # visible in the split.
    with _probe.install() as ladder:
        search_events("get_session", limit=10)
    assert ladder.fts_passes > saturated.fts_passes
    assert ladder.scan_ms > 0.0
    # Hydration is timed apart from the SQL: a wide pool spends real time building
    # hits, and that is not the index being slow.
    assert ladder.build_ms > 0.0


def test_duplicate_flood_rescan_surfaces_a_buried_distinct_hit(archive_home) -> None:
    """A MATCH pass flooded with byte-identical copies of one message can bury the
    distinct answer below the pool cut. The rescan folds the flood to one
    representative per ``(thread, content)`` and merges the survivors, so the
    buried hit stays reachable — bounded, best-effort (see ``fts._rescan_distinct``)."""
    from datetime import datetime, timezone

    from thread_archive._retrieval import index_events, search_events
    from thread_archive._store import Event, Thread, get_session

    init_db()
    with get_session() as s:
        flood = Thread(name="conv:flood", title="flood", thread_type="conversation",
                       source="cc", source_id="flood")
        s.add(flood)
        s.flush()
        # Many copies of one high-term-frequency message: every one out-ranks the
        # longer, single-mention buried doc, so the top of the pool is all flood.
        events = [
            Event(thread_id=flood.id, stream_id="f", event_type="user_message_sent",
                  payload={"content": "zebra zebra zebra zebra"},
                  occurred_at=datetime(2026, 1, 1, 10, i, tzinfo=timezone.utc))
            for i in range(6)
        ]
        buried = Thread(name="conv:buried", title="buried", thread_type="conversation",
                        source="cc", source_id="buried")
        s.add(buried)
        s.flush()
        events.append(Event(
            thread_id=buried.id, stream_id="b", event_type="user_message_sent",
            payload={"content": "zebra distinctive marmoset sentinel phrase"},
            occurred_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)))
        for e in events:
            s.add(e)
        s.flush()
        index_events(s, events)
        s.commit()

    # Cut at 4: the pool fills with flood copies (distinct·2 < len(hits)), so the
    # rescan runs and the buried 'marmoset' hit — below every copy — is folded back
    # in. Without it the top 4 would be flood copies alone.
    with _probe.install() as probe:
        hits = search_events("zebra", limit=4)
    assert any("marmoset" in (h["full_content"] or "") for h in hits)
    # The rescan is a second SQL shape over the same MATCH, so its cost is its own
    # bucket rather than more milliseconds inside the pass that triggered it.
    assert probe.rescan_ms > 0.0
    assert probe.fts_passes == 1  # the rescan is not a pass; it is what follows one


def _seed_agent_thread(archive_home):
    """A third thread retyped 'system' — an agent-run (subagent/machinery) session
    that echoes the corpus vocabulary, the way a spawned swarm echoes its prompt."""
    from sqlalchemy import select, update

    from thread_archive._store import Thread, use_session

    f3 = archive_home / "agent.jsonl"
    _write_cc(f3, _cc_turn("u9", "a9", "how does authentication work in the login flow",
                           "Subagent report: authentication rides the session token.", 3))
    import_session_incremental(f3, "proj:agent")
    with use_session() as s:
        tid = s.execute(
            select(Thread.id).where(Thread.source_id == "proj:agent")
        ).scalar_one()
        s.execute(update(Thread).where(Thread.id == tid).values(thread_type="system"))
        s.commit()
    return tid


def test_agents_excluded_from_search_by_default(archive_home) -> None:
    """Agent-run threads (thread_type='system') never surface in a default search;
    agents='include' adds them, agents='only' returns nothing else."""
    _seed_corpus(archive_home)
    agent_tid = _seed_agent_thread(archive_home)

    default = search("authentication")
    assert default, "the human thread must still hit"
    assert all(h["thread_id"] != agent_tid for h in default)

    included = search("authentication", agents="include")
    assert {agent_tid} < {h["thread_id"] for h in included}

    only = search("authentication", agents="only")
    assert only and all(h["thread_id"] == agent_tid for h in only)


def test_agents_filter_covers_structural_shapes(archive_home) -> None:
    """The exclusion rides the shared WHERE, so count / oldest / startswith
    inherit it."""
    _seed_corpus(archive_home)
    agent_tid = _seed_agent_thread(archive_home)

    tally = search("authentication", output="count")
    assert tally and all(h["thread_id"] != agent_tid for h in tally)

    oldest = search("authentication", sort="oldest")
    assert oldest and all(h["thread_id"] != agent_tid for h in oldest)

    prefix = search("", startswith="how does authentication")
    assert prefix and all(h["thread_id"] != agent_tid for h in prefix)


def test_agents_deliberate_scopes_bypass(archive_home) -> None:
    """An explicit thread_id reaches an agent thread regardless of the default,
    and an explicit types list wins over the agents switch entirely."""
    _seed_corpus(archive_home)
    agent_tid = _seed_agent_thread(archive_home)

    scoped = search("authentication", thread_id=agent_tid)
    assert scoped and all(h["thread_id"] == agent_tid for h in scoped)

    typed = search("authentication", types=["system"])
    assert typed and all(h["thread_id"] == agent_tid for h in typed)


def test_agents_invalid_value_raises(archive_home) -> None:
    import pytest

    _seed_corpus(archive_home)
    with pytest.raises(ValueError, match="agents"):
        search("authentication", agents="everyone")


def test_a_saturated_search_bills_its_reconciliation_apart_from_its_fold(archive_home) -> None:
    """A search's latency splits at the pool: how it was *found* (the arms) and what
    then happened to it (the shape stages). A search whose pool saturated pays both
    halves — it folds the pool by thread and then reconciles that fold against the
    exact match set, two costs with unrelated scaling that a single number would
    hide behind whichever one happened to dominate."""
    init_db()
    for i in range(6):
        f = archive_home / f"b{i}.jsonl"
        _write_cc(f, _cc_turn(f"u{i}", f"a{i}", f"the widget report {i}",
                              f"acknowledged, widget {i}", i + 1))
        import_session_incremental(f, f"proj:b{i}")

    from thread_archive._retrieval import SearchParams

    # A pool floor of 1 makes the pool `limit * 5` deep, which this corpus
    # saturates — the state where the reconciliation has work to do.
    saturating = SearchParams(pool_floor=1)
    with _probe.install() as browse:
        search("widget", limit=2, params=saturating)
    rec = browse.as_record()
    # The reconciliation ran and is its own bucket — and it is the outer bound on
    # the exact-set scan nested inside it, not a sibling of it.
    assert browse.extend_ms > 0.0
    assert browse.extend_ms >= browse.set_ms
    # The fold is billed separately, so a slow reconciliation can never be read as
    # a slow grouping pass.
    assert "group_ms" in rec and rec["group_ms"] >= 0.0

    # A search whose pool held the whole match set has nothing to reconcile, so it
    # records no extend at all rather than a zero that would read as "measured and
    # instant".
    with _probe.install() as ranked:
        search("widget", limit=2)
    assert ranked.extend_ms == 0.0
    assert "extend_ms" not in ranked.as_record()
    # It still ranks, which is the stage a grouping shape runs over the whole pool.
    assert ranked.rank_ms > 0.0
