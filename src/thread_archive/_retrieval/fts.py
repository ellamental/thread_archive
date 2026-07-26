"""SQLite FTS5 lexical search.

The embedded FTS arm, SQLite-native throughout. One in-DB FTS5 virtual table
(``event_search``) in **external-content** mode over the ``events_fts`` shadow,
which is in turn derived from the events: the FTS table holds only the inverted
index — column reads, snippets, and LIKE scans resolve through the shadow by
rowid, so the corpus text is stored once, not twice. Shadow→index sync is
trigger-based (``events_fts_ai``/``_ad``/``_au``): every writer — incremental
import, thread-meta sync — writes the shadow alone and the triggers
mirror it, so the two surfaces can't drift. ``rebuild_fts`` re-derives the
shadow from the events and retokenizes the index, and is the FTS half of
``reindex``; it is also the heal for an ``event_search`` that predates the
external-content layout (``ensure_fts`` swaps such a table for an empty
current-shape one, and lexical search stays dark until the reindex refills it).

SQL is composed with literal table names + bound params (never f-strings) — the
no-f-string-SQL convention.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .._store import Event, EventFts, get_engine, use_session
from . import _probe
from ._classify import canonical_time_bound, classify_query
from ._extract import INDEXABLE_EVENT_TYPES, extract_fts_content
from ._types import EventHit

logger = logging.getLogger(__name__)

# Content is indexed; everything else is UNINDEXED so it can still be filtered in
# WHERE (thread/type/tool/time) without bloating the index. External content:
# the FTS table stores no column values of its own — every column read resolves
# through the ``events_fts`` shadow by rowid.
_CREATE_FTS = (
    "CREATE VIRTUAL TABLE event_search USING fts5("
    "content, event_id UNINDEXED, thread_id UNINDEXED, event_type UNINDEXED, "
    "content_type UNINDEXED, tool_name UNINDEXED, occurred_at UNINDEXED, "
    "content='events_fts', content_rowid='id', "
    "tokenize = 'porter unicode61')"
)

_FTS_COLS = "content, event_id, thread_id, event_type, content_type, tool_name, occurred_at"
_NEW_VALS = ", ".join("new." + c.strip() for c in _FTS_COLS.split(","))
_OLD_VALS = ", ".join("old." + c.strip() for c in _FTS_COLS.split(","))

# Shadow→index sync triggers. External-content FTS5 doesn't watch its content
# table — every ``events_fts`` write must be mirrored, and removing a row's
# postings (the 'delete' command form) needs the old column values, which only
# a trigger still sees.
_TRIGGERS = {
    "events_fts_ai": (
        "CREATE TRIGGER events_fts_ai AFTER INSERT ON events_fts BEGIN "
        "INSERT INTO event_search(rowid, " + _FTS_COLS + ") "
        "VALUES (new.id, " + _NEW_VALS + "); END"
    ),
    "events_fts_ad": (
        "CREATE TRIGGER events_fts_ad AFTER DELETE ON events_fts BEGIN "
        "INSERT INTO event_search(event_search, rowid, " + _FTS_COLS + ") "
        "VALUES ('delete', old.id, " + _OLD_VALS + "); END"
    ),
    "events_fts_au": (
        "CREATE TRIGGER events_fts_au AFTER UPDATE ON events_fts BEGIN "
        "INSERT INTO event_search(event_search, rowid, " + _FTS_COLS + ") "
        "VALUES ('delete', old.id, " + _OLD_VALS + "); "
        "INSERT INTO event_search(rowid, " + _FTS_COLS + ") "
        "VALUES (new.id, " + _NEW_VALS + "); END"
    ),
}

# Plain FTS5 bm25 (more-negative = better) — via the hidden ``rank`` column, NOT
# a literal ``bm25(event_search)`` expression. The two order identically (rank IS
# bm25 by default), but only ``ORDER BY rank`` engages FTS5's internal rank-sort
# (xBestIndex flag; EXPLAIN shows ``INDEX ...:M`` with no temp B-tree), so the
# SELECT list — snippet() above all — is evaluated for the LIMIT rows that come
# out, not for every matching row going into an external sort. Over this ~1M-doc
# index a broad OR query matches 200k–600k docs; the expression form pays a
# per-match bm25()+snippet() sort (seconds), the rank form streams (~0.4s).
_RANK_EXPR = "rank"

# Duplicate-flood rescan bounds. A burst of byte-identical events — a fleet of
# agents launched on one prompt, a thread re-emitting a line — can fill the whole
# bm25 head of a MATCH pass, so the one conversation that recorded the answer
# never enters the candidate pool: its longer doc ranks *below* every copy, and
# the content-dedup that collapses the copies runs only after the pool is cut.
# When the primary MATCH pass comes back saturated and mostly-duplicate, one
# bounded rescan re-gathers the distinct-content representatives — a streaming
# top-``_FLOOD_RESCAN_CAP`` window (cheap; snippet is only computed there) folded
# by ``GROUP BY thread_id, content`` (a global GROUP BY over a broad match would
# forfeit FTS5's streaming rank-sort — measured ~7× slower). The pool needs
# fewer than half its rows to be duplicates before the rescan is worth it.
_FLOOD_RESCAN_CAP = 5000


def build_event_hit(
    *,
    event_id: int,
    thread_id: str,
    event_type: str,
    content_type: Optional[str],
    snippet: str,
    full_content: str,
    occurred_at: Optional[str],
    bm25: Optional[float] = None,
) -> EventHit:
    """One event search hit in the canonical shape. ``thread_title`` is enriched
    by the caller. ``occurred_at`` is the stored column text (canonical naive
    form); it parses to a naive datetime — a stray offset-carrying value is
    normalized to local-naive so every hit's datetime compares against the rest.

    ``bm25`` is FTS5's own score for the hit, negated so higher is better
    (``rank`` is more-negative-is-better), and stamped as ``_bm25`` only when the
    pass that found it was a MATCH — a substring scan has no score, and a
    semantic-only hit never sees this constructor. It arrives here raw, on a
    per-query scale set by the query's term count and their IDF;
    :func:`thread_archive._retrieval.retrieve_pool` normalizes it against the
    pool's peak before the ranker weighs it."""
    dt: Optional[datetime] = None
    if occurred_at:
        try:
            dt = datetime.fromisoformat(str(occurred_at))
        except ValueError:
            dt = None
        else:
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
    hit: EventHit = {
        "event_id": event_id,
        "thread_id": thread_id,
        "thread_title": None,
        "event_type": event_type,
        "content_type": content_type,
        "snippet": snippet,
        "full_content": full_content,
        "occurred_at": dt,
    }
    if bm25 is not None:
        hit["_bm25"] = -float(bm25)
    return hit


def _event_search_shape(s: Session) -> Optional[bool]:
    """``None`` when ``event_search`` doesn't exist, ``True`` when it is the
    current external-content shape, ``False`` when it predates it (a contentful
    table storing its own copy of the corpus)."""
    sql = s.execute(
        sa_text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": "event_search"}
    ).scalar()
    if sql is None:
        return None
    return "content=" in sql


def _create_triggers(s: Session) -> None:
    for name, ddl in _TRIGGERS.items():
        exists = s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = :n"),
            {"n": name},
        ).scalar()
        if not exists:
            s.execute(sa_text(ddl))


def _drop_triggers(s: Session) -> None:
    for name in _TRIGGERS:
        s.execute(sa_text("DROP TRIGGER IF EXISTS " + name))


def ensure_fts(session: Optional[Session] = None) -> None:
    """Create the FTS5 virtual table + its sync triggers if absent. Idempotent.

    An ``event_search`` that predates the external-content layout is swapped for
    an empty current-shape table: queries keep working (lexically dark) and the
    next ``reindex`` refills it — heavy work stays on the operator-visible path,
    per the schema module's "verify reports, reindex heals" rule. The swap
    deliberately leaves the sync triggers ABSENT: over a populated shadow and an
    empty index, a trigger-fired FTS 'delete' would target postings that don't
    exist, which fts5 raises as SQLITE_CORRUPT. Triggerless, shadow writes stay
    safe (the empty index misses nothing it wasn't already missing), verify's
    ``fts_triggers`` check reports the state, and ``rebuild_fts`` restores the
    triggers when it refills the index."""
    with use_session(session) as s:
        shape = _event_search_shape(s)
        if shape is False:
            logger.warning(
                "event_search predates the external-content layout; replaced with an "
                "empty one — lexical search returns nothing until a reindex refills it"
            )
            _drop_triggers(s)
            s.execute(sa_text("DROP TABLE event_search"))
            s.execute(sa_text(_CREATE_FTS))
        elif shape is None:
            s.execute(sa_text(_CREATE_FTS))
        # The dark window (populated shadow, empty index — i.e. the swap above,
        # observed now or on any later open) must stay triggerless; a fresh
        # empty-empty store is not dark and gets its triggers immediately.
        dark = bool(s.execute(sa_text(
            "SELECT EXISTS(SELECT 1 FROM events_fts) "
            "AND NOT EXISTS(SELECT 1 FROM event_search_docsize)"
        )).scalar())
        if not dark:
            _create_triggers(s)
        if session is None:
            s.commit()


#: What SQLite cannot carry in a bound parameter, whatever we wrap it in. NUL is
#: C's string terminator: sqlite3 reports ``unterminated string`` for a param
#: holding one, and inside a quoted FTS5 phrase it truncates the expression
#: mid-token — so even the fully-quoted demotion form below raises on it. A lone
#: surrogate is not encodable to UTF-8 at all, so the driver raises before SQLite
#: sees the statement.
_SQL_UNSAFE = re.compile("[\x00\ud800-\udfff]")


def _sql_safe(text_: str) -> str:
    """Drop the code points of :data:`_SQL_UNSAFE`.

    Queries arrive from an agent over MCP, where a JSON string is free to carry
    ``\\u0000`` or an unpaired ``\\ud800``; neither is text anyone meant to search
    for, so they are dropped the way a stray ``"`` is rather than escaped. Applied
    by each builder immediately before its text becomes a bound param, so every
    entry point into the lexical arm inherits it.
    """
    return _SQL_UNSAFE.sub("", text_ or "")


def _quote_all_tokens(text_: str) -> str:
    """Every whitespace token as a quoted FTS5 phrase term — no operators, no
    syntax, so the expression can never raise. The demotion target for malformed
    boolean shapes and the retry form for a residual fts5 syntax error."""
    toks = []
    for tok in re.findall(r"\S+", _sql_safe(text_)):
        inner = tok.replace('"', "")
        if inner:
            toks.append('"' + inner + '"')
    return " ".join(toks) or '""'


def to_match_query(query: str) -> str:
    """Translate a natural-language / boolean / quoted-phrase query into a safe
    FTS5 MATCH expression. AND/OR/NOT and "quoted phrases" pass through; every
    other token is emitted quoted so stray punctuation can't raise a syntax error.

    Malformed boolean shapes — an unbalanced quote, a leading/trailing operator,
    or adjacent operators (FTS5's NOT is binary, so ``AND NOT`` is a syntax error
    too) — can't compile as operators, so they demote to the fully-quoted literal
    form instead of raising out of MATCH."""
    out: list[str] = []
    ops: list[bool] = []
    for tok in re.findall(r'"[^"]*"|\S+', _sql_safe(query)):
        if tok in ("AND", "OR", "NOT"):
            out.append(tok)
            ops.append(True)
        elif tok.startswith('"'):
            if len(tok) < 2 or not tok.endswith('"'):
                return _quote_all_tokens(query)  # unbalanced quote
            out.append(tok)
            ops.append(False)
        else:
            out.append('"' + tok.replace('"', "") + '"')
            ops.append(False)
    if ops and (ops[0] or ops[-1] or any(a and b for a, b in zip(ops, ops[1:]))):
        return _quote_all_tokens(query)
    return " ".join(out) or '""'


def _clean_query_text(query: str) -> str:
    """Strip quotes + boolean keywords and collapse whitespace — the substring a
    code-identifier / pipe-OR query matches."""
    clean = re.sub(r'["\']', "", query or "")
    clean = re.sub(r"\b(AND|OR|NOT)\b", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _escape_like(text_: str) -> str:
    """Escape LIKE wildcards (``\\`` ``%`` ``_``) so the text matches literally;
    pair with ``ESCAPE '\\'`` in the SQL. Identifier queries are full of ``_`` —
    unescaped, ``get_session`` would match ``getXsession``."""
    return _sql_safe(text_).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_prefix(prefix: str) -> str:
    """A ``startswith`` LIKE pattern matching a literal prefix."""
    return _escape_like(prefix) + "%"


def _like_substring(term: str) -> str:
    """A substring LIKE pattern matching a literal infix."""
    return "%" + _escape_like(term) + "%"


def _in_clause(column: str, values: list, prefix: str, params: dict, negate: bool) -> str:
    names = []
    for i, v in enumerate(values):
        key = prefix + str(i)
        params[key] = v
        names.append(":" + key)
    op = " NOT IN (" if negate else " IN ("
    return column + op + ",".join(names) + ")"


def _quote_phrase(text_: str) -> str:
    """A cleaned text span as one FTS5 phrase term."""
    return '"' + _sql_safe(text_).replace('"', "") + '"'


def _identifier_tokens(text_: str) -> list[str]:
    """The FTS tokens of a code query — the alphanumeric runs an identifier splits
    into on its separators (``get_session`` → ``get`` ``session``, ``a.b.c`` →
    ``a`` ``b`` ``c``). Order-preserving, deduped. These are reachable through the
    FTS index, so a MATCH over them fills the candidate pool without the full-table
    substring scan for any query whose tokens exist as tokens."""
    out: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9]+", text_ or ""):
        if tok not in out:
            out.append(tok)
    return out


# The substring LIKE fallback is a full-table scan (no index serves an infix
# LIKE). When it does run — a query whose tokens are all too rare for the indexed
# passes to fill the pool — it is bounded to the most recent this-many rows by id.
# The scan is recency-ordered already, so the cap keeps the newest within-token
# matches and holds the worst case well under the latency budget instead of
# walking the whole ~1M-doc corpus (~10s). id is the FTS rowid (append-ordered),
# so a ``rowid >= max-cap`` floor is an indexed range, not a scan to find the cap.
_LIKE_SCAN_CAP = 25000


@dataclass(frozen=True)
class _Pass:
    """One candidate-gathering pass of :func:`search_events`: a WHERE fragment
    plus its bound params, the ORDER BY, whether the fragment is an FTS5 MATCH
    (drives the snippet expression and the fts5-syntax-error retry), whether the
    pass is a fallback (runs only when the passes before it left the pool short),
    and whether it is a full-table substring scan to bound to the recent-id window
    (``scan_cap`` — the latency backstop on the one pass that can't ride the index)."""

    where: str
    params: dict = field(default_factory=dict)
    order: str = _RANK_EXPR
    use_match: bool = True
    is_fallback: bool = False
    scan_cap: bool = False


#: The match modes an explicit ``match=`` selects between. ``token`` is the
#: indexed FTS5 MATCH — the shipped behavior, and what every query mode in
#: :func:`~._classify.classify_query` builds on. ``substring`` is the uncapped
#: infix LIKE: it finds ``p4`` inside ``mp4`` and ``p400``, which no MATCH can
#: see, and pays a full-table scan for it (see :func:`search_events`).
MATCH_MODES = ("token", "substring")


def _shared_filters(
    *,
    thread_id: Optional[str] = None,
    thread_ids: Optional[list[str]] = None,
    tool_name: Optional[str] = None,
    path: Optional[str] = None,
    types: Optional[list[str]] = None,
    content_types: Optional[list[str]] = None,
    exclude_content_types: Optional[list[str]] = None,
    source: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    agents: str = "exclude",
) -> tuple[list[str], dict]:
    """The scope predicate every pass and every exact-set query shares, as
    ``(where_fragments, params)``.

    One definition, three readers — the candidate-pool passes
    (:func:`search_events`), the per-thread tally (:func:`matched_threads`), and
    the capped total (:func:`count_matches`). Shared because a filter that
    applied to the pool but not to the tally would make the two disagree about
    the same corpus, and the tally is what a paginated caller trusts to know
    when it has seen everything.
    """
    shared: list[str] = []
    params: dict = {}
    if thread_id is not None:
        shared.append("thread_id = :tid")
        params["tid"] = thread_id
    elif thread_ids is not None:
        # A resolved id-set scope (e.g. a topic's member threads). Like a single
        # explicit thread_id, the scope is deliberate and bypasses the blacklist.
        shared.append(_in_clause("thread_id", thread_ids, "tids", params, negate=False))
    else:
        # Honor the per-thread search blacklist (threads.exclude_from_search).
        # An explicit thread_id scope is deliberate and bypasses it.
        shared.append("thread_id NOT IN (SELECT id FROM threads WHERE exclude_from_search)")
        # Agent-run threads (subagent/machinery sessions) ride the same pattern:
        # out of the default pool, reachable via agents='include'/'only', an
        # explicit thread scope, or a ``types`` filter that names 'system'
        # (an explicit type request must not be emptied by the default).
        if agents == "exclude" and not (types and "system" in types):
            shared.append("thread_id NOT IN (SELECT id FROM threads WHERE thread_type = 'system')")
        elif agents == "only":
            shared.append("thread_id IN (SELECT id FROM threads WHERE thread_type = 'system')")
    if tool_name:
        shared.append("tool_name = :tool")
        params["tool"] = tool_name
    if path:
        from .code import path_scope_sql

        shared.append(path_scope_sql(path, params))
    if types:
        # event_search carries thread_id but not thread_type; constrain via the
        # threads table (idx_threads_type), same pattern as the source filter.
        shared.append(
            "thread_id IN (SELECT id FROM threads WHERE "
            + _in_clause("thread_type", types, "tt", params, negate=False) + ")"
        )
    if content_types:
        shared.append(_in_clause("content_type", content_types, "ct", params, negate=False))
    if exclude_content_types:
        shared.append(_in_clause("content_type", exclude_content_types, "xct", params, negate=True))
    if source:
        # event_search carries thread_id but not source; constrain to threads of
        # the named provider(s) via an indexed subquery (idx_threads_source). An
        # empty match yields no rows rather than invalid SQL.
        shared.append(
            "thread_id IN (SELECT id FROM threads WHERE "
            + _in_clause("source", source, "src", params, negate=False) + ")"
        )
    if since:
        shared.append("occurred_at >= :since")
        params["since"] = since
    if until:
        shared.append("occurred_at <= :until")
        params["until"] = until
    return shared, params


def _primary_predicate(
    query: str, *, match_mode: str, startswith: Optional[str]
) -> Optional[tuple[str, dict]]:
    """The single WHERE fragment that defines a query's match **set** — what the
    exact-set queries count and group over, as ``(fragment, params)``.

    This is deliberately the *primary* pass only, never the fallback ladder
    :func:`search_events` runs to fill a short pool. The tiers exist to top up a
    candidate pool with looser matches (an OR pass over a conjunctive query, the
    within-token substring catcher); folding them into the set would make "every
    thread matching this query" mean something different on a corpus where the
    strict pass happened to come back short. ``None`` when the query has no
    matchable content.
    """
    if startswith is not None:
        return "content LIKE :sw ESCAPE '\\'", {"sw": _like_prefix(startswith)}
    if match_mode == "substring":
        clean = _clean_query_text(query)
        return ("content LIKE :sub ESCAPE '\\'", {"sub": _like_substring(clean)}) if clean else None
    mode, _ = classify_query(query)
    if mode == "or":
        terms = [t for t in (_clean_query_text(t) for t in (query or "").split("|")) if t]
        if not terms:
            return None
        return "event_search MATCH :q", {"q": " OR ".join(_quote_phrase(t) for t in terms)}
    if mode == "code":
        clean = _clean_query_text(query)
        return ("event_search MATCH :q", {"q": _quote_phrase(clean)}) if clean else None
    return "event_search MATCH :q", {"q": to_match_query(query)}


def search_events(
    query: str,
    thread_id: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    exclude_content_types: Optional[list[str]] = None,
    source: Optional[list[str]] = None,
    types: Optional[list[str]] = None,
    startswith: Optional[str] = None,
    path: Optional[str] = None,
    *,
    thread_ids: Optional[list[str]] = None,
    agents: str = "exclude",
    oldest_first: bool = False,
    or_fallback: bool = True,
    match_mode: str = "token",
    session: Optional[Session] = None,
) -> list[EventHit]:
    """Lexical search over the FTS5 index → canonical event-hit dicts.

    Query mode (shared classifier): natural-language / boolean / quoted-phrase
    queries run FTS5 MATCH (bm25-ranked). Pipe-OR and code-identifier shapes run
    TWO passes, merged: a phrase MATCH (the tokenizer splits ``get_session`` into
    ``get session``, so the quoted phrase rides the index, bm25-ranked over the
    whole corpus) plus a substring LIKE over the most recent matches (catches
    within-token substrings MATCH can't see). The MATCH pass is what keeps *old*
    hits reachable for common identifiers — a single recency-ordered LIKE pass
    caps out on the newest ``limit`` matches. The LIKE pass is a full-table scan,
    so it only runs when the MATCH pass left the pool short (see the pass list). ``startswith`` overrides the query
    mode entirely with a structural prefix scan (content LIKE 'prefix%',
    recency-ordered) — the query text is not matched, only the structural filters.

    A plain natural-language query (no operators, no quotes) is implicitly
    conjunctive — FTS5 MATCH requires *every* token, stopwords included, so
    "how did we fix the auth bug" needs all seven words in one document. When
    the strict pass leaves the pool short, a fallback OR pass over the
    meaningful (non-stopword) terms tops it up, bm25-ranked, appended *after*
    the strict hits so full matches keep their rank priority. That's what saves
    lexical-only installs from hard zero-recall on conversational queries.
    ``or_fallback=False`` disables the tier — a count tally must stay strict, or
    partial matches inflate it.

    ``oldest_first`` orders every pass by ``occurred_at ASC`` instead of its
    ranking order, so the candidate pool holds the *earliest* matching rows —
    without it, "when was this first discussed" sorts only whatever bm25's
    top-N happened to keep, which for a frequent term is recency-biased.

    ``agents`` controls agent-run threads (``thread_type='system'`` — subagent /
    machinery sessions): 'exclude' (default) keeps them out of the pool, 'include'
    searches them alongside conversations, 'only' searches nothing else. Like the
    blacklist, an explicit ``thread_id``/``thread_ids`` scope is deliberate and
    bypasses the filter.

    ``path`` restricts to the threads that touched a file (see
    :mod:`.code`) — a subquery rather than a materialized id list, because a broad
    pattern puts thousands of threads in scope and a bound-parameter list that wide
    would have to be silently truncated.

    ``match_mode='substring'`` replaces the whole mode ladder above with ONE
    uncapped infix LIKE over the cleaned query text. It is the only way to reach
    a within-token match the index cannot see (``p4`` inside ``mp4``), and the
    only path that lifts :data:`_LIKE_SCAN_CAP`: the cap is a latency backstop on
    a scan the caller did not ask for, and an explicit ``match='substring'`` is
    the caller asking for it — the same "a deliberate scope stands the default
    down" rule the blacklist and the ``agents`` filter already follow. It costs a
    full-table scan (no index serves an infix LIKE) and runs alone: no fallback
    ladder and no flood rescan, because there is no short pool to top up — the
    scan either found a row or the row does not contain the substring.
    """
    ensure_fts(session)
    if match_mode not in MATCH_MODES:
        raise ValueError("match must be 'token' or 'substring'")
    if thread_ids is not None and not thread_ids:
        return []  # an empty id-set scope matches nothing (IN () isn't valid SQL)
    mode, is_boolean = classify_query(query)

    # Each pass is (match_where, match_params, order, use_match, fallback); shared
    # filters are appended to every pass. A fallback pass is a substring LIKE — a
    # full-table scan (seconds over a ~1M-doc index; ``content`` has no index that
    # can serve an infix LIKE) — so it only runs when the MATCH pass ahead of it
    # left the candidate pool short: it exists to catch within-token substrings
    # MATCH can't see, and when the exact-token phrase already fills the pool
    # those can't displace anything the scan is worth seconds for.
    passes: list[_Pass] = []
    if startswith is not None:
        # Structural prefix scan — wildcards in the prefix are escaped (ESCAPE '\')
        # so a % or _ in user input matches literally rather than as a LIKE wildcard.
        passes.append(_Pass("content LIKE :sw ESCAPE '\\'", {"sw": _like_prefix(startswith)},
                            order="occurred_at DESC", use_match=False))
    elif match_mode == "substring":
        # One uncapped scan, no ladder behind it: an explicit substring ask has
        # exactly one right answer set, and a fallback tier could only widen it
        # past what the caller asked for.
        clean = _clean_query_text(query)
        if not clean:
            return []
        # Ordered by rowid, not occurred_at: the scan already has to visit every
        # row, and ``occurred_at`` is UNINDEXED, so sorting by it means materializing
        # and sorting the whole match list (measured ~14s where the scan alone is
        # ~1s). The FTS rowid is append-ordered, so DESC walks the index backwards
        # for the same newest-first intent at no cost.
        passes.append(_Pass("content LIKE :sub ESCAPE '\\'", {"sub": _like_substring(clean)},
                            order="rowid DESC", use_match=False))
    elif mode == "or":
        terms = [_clean_query_text(t) for t in (query or "").split("|")]
        terms = [t for t in terms if t]
        if not terms:
            return []
        match_q = " OR ".join(_quote_phrase(t) for t in terms)
        passes.append(_Pass("event_search MATCH :q", {"q": match_q}))
        like_params: dict = {}
        ors = []
        for i, term in enumerate(terms):
            like_params["or" + str(i)] = _like_substring(term)
            ors.append("content LIKE :or" + str(i) + " ESCAPE '\\'")
        passes.append(_Pass("(" + " OR ".join(ors) + ")", like_params,
                            order="occurred_at DESC", use_match=False, is_fallback=True,
                            scan_cap=True))
    elif mode == "code":
        clean = _clean_query_text(query)
        passes.append(_Pass("event_search MATCH :q", {"q": _quote_phrase(clean)}))
        # Indexed token fallbacks before the substring scan: an identifier
        # tokenizes on its separators, so its tokens ride the FTS index. Filling
        # the pool from a token AND (all tokens present) then a token OR (any)
        # keeps the full-table LIKE from running for any query whose tokens exist
        # as tokens — the common case, and the one that made ``foo_bar``-style
        # queries scan the whole corpus.
        toks = _identifier_tokens(clean)
        if len(toks) > 1:
            passes.append(_Pass("event_search MATCH :qand",
                                {"qand": " AND ".join(_quote_phrase(t) for t in toks)},
                                is_fallback=True))
            passes.append(_Pass("event_search MATCH :qor",
                                {"qor": " OR ".join(_quote_phrase(t) for t in toks)},
                                is_fallback=True))
        # The within-token substring catcher (``get_session`` inside
        # ``megaget_sessionizer``) MATCH can't see — last, and bounded to the
        # recent-id window so a rare-token query can't turn it into a full scan.
        passes.append(_Pass("content LIKE :codepat ESCAPE '\\'", {"codepat": _like_substring(clean)},
                            order="occurred_at DESC", use_match=False, is_fallback=True,
                            scan_cap=True))
    else:
        passes.append(_Pass("event_search MATCH :q", {"q": to_match_query(query)}))

    shared, shared_params = _shared_filters(
        thread_id=thread_id, thread_ids=thread_ids, tool_name=tool_name, path=path,
        types=types, content_types=content_types,
        exclude_content_types=exclude_content_types, source=source,
        since=since, until=until, agents=agents,
    )
    shared_params["lim"] = limit

    hits: list[EventHit] = []
    seen: set[tuple[int, Optional[str]]] = set()
    with use_session(session) as s:
        def run_pass(p: _Pass) -> None:
            order = "occurred_at ASC" if oldest_first else p.order
            snippet_expr = (
                "snippet(event_search, 0, '', '', ' … ', 12)" if p.use_match
                else "substr(content, 1, 300)"
            )
            where = [p.where] + shared
            pass_params: dict = dict(p.params)
            # Bound a full-table substring scan to the recent-id window: an indexed
            # rowid range instead of walking the whole corpus (the latency backstop
            # on the one pass that can't use the FTS index). Skipped under an
            # explicit id scope, where the pool is already a handful of threads and
            # the recency cap would wrongly drop their older within-token matches.
            if p.scan_cap and thread_id is None and thread_ids is None:
                where.append("rowid >= (SELECT max(rowid) FROM event_search) - :scan_cap")
                pass_params["scan_cap"] = _LIKE_SCAN_CAP
            # ``rank`` rides the SELECT list beside the row: FTS5 already computed
            # it for the sort, it is NULL on a non-MATCH pass rather than an error,
            # and the query plan is unchanged (still ``INDEX …:M`` — the streaming
            # rank-sort, measured at parity). That surfaces the arm's own bm25
            # magnitude, which the ranker weighs as ``bm25_score_weight``; without
            # it the arm's verdict survives only as the order rows arrive in.
            sql = sa_text(
                "SELECT event_id, thread_id, event_type, content_type, occurred_at, "
                + snippet_expr + " AS snippet, content AS full_content, "
                + _RANK_EXPR + " AS bm25 "
                "FROM event_search WHERE " + " AND ".join(where) +
                " ORDER BY " + order + " LIMIT :lim"
            )
            # An indexed MATCH and a full-table LIKE are separate buckets: they
            # differ by orders of magnitude, and which one a slow arm spent its time
            # in is the whole question. The syntax-error retry stays inside the same
            # bucket — it is the same pass, paid twice.
            _probe.bump("fts_passes")
            _t = perf_counter()
            try:
                rows = s.execute(sql, {**shared_params, **pass_params}).mappings().all()
            except OperationalError as exc:
                # A residual fts5 syntax error (a shape to_match_query's
                # validation didn't catch) retries once with every MATCH param
                # demoted to the everything-quoted form — a malformed query
                # returns results-or-empty, never a raw OperationalError.
                msg = str(exc.orig).lower()
                if not p.use_match or ("fts5" not in msg and "unterminated string" not in msg):
                    raise
                logger.warning("FTS5 rejected MATCH %r; retrying fully quoted", p.params)
                retry = {k: _quote_all_tokens(v) if isinstance(v, str) else v
                         for k, v in pass_params.items()}
                rows = s.execute(sql, {**shared_params, **retry}).mappings().all()
            _probe.record("match_ms" if p.use_match else "scan_ms", _t)
            _t = perf_counter()
            for r in rows:
                key = (r["event_id"], r["content_type"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(build_event_hit(
                    event_id=r["event_id"],
                    thread_id=r["thread_id"],
                    event_type=r["event_type"],
                    content_type=r["content_type"],
                    snippet=r["snippet"] or "",
                    full_content=r["full_content"] or "",
                    occurred_at=r["occurred_at"],
                    bm25=r["bm25"],
                ))
            _probe.record("build_ms", _t)

        for p in passes:
            if p.is_fallback and len(hits) >= limit:
                continue
            run_pass(p)

        # Fallback OR tier for plain natural-language queries (see the docstring):
        # only when the strict all-terms pass left the pool short, and only over
        # the meaningful terms (stopwords carry no retrieval signal). Explicit
        # boolean / quoted / pipe-OR / identifier queries asked for their own
        # semantics and are left alone.
        #
        # This tier is the single largest stage of a natural-language search, and
        # both obvious ways to cheapen it cost recall the gold floors are holding.
        # A union's cost is set by its commonest token — one corpus-wide word puts
        # six figures of rows through bm25 — but that breadth *is* the recall: this
        # is the pass that answers the vague and paraphrase shapes, where the strict
        # pass matched almost nothing and bm25 over the whole union is what finds
        # the answer. Ordering it by rowid instead runs 3–9× faster and drops
        # findability and judged-cases below floor (the vague shape hardest);
        # pruning the high-document-frequency terms out of the union is not
        # order-preserving either — over the pool's own top 20 it changes 10–85% of
        # the rows. A cheaper tier has to come from somewhere other than the shape
        # of this MATCH.
        if (or_fallback and startswith is None and match_mode == "token"
                and mode == "tsquery" and not is_boolean
                and '"' not in (query or "") and len(hits) < limit):
            from .rank import search_terms

            terms = [t for t in search_terms(query) if " " not in t]
            or_q = " OR ".join(_quote_phrase(t) for t in terms)
            # Skip when the tier would be the strict pass verbatim (single term,
            # nothing dropped) — same MATCH, nothing new to add.
            if terms and or_q.lower() != to_match_query(query).lower():
                run_pass(_Pass("event_search MATCH :orq", {"orq": or_q}))

        # Duplicate-flood rescan: when the primary MATCH pass saturated the pool
        # and most of it is byte-identical content, the distinct answer may rank
        # below every copy and have missed the cut. Re-gather the distinct-content
        # representatives so it is reachable (bounded; see _FLOOD_RESCAN_CAP).
        match_pass = passes[0] if passes and passes[0].use_match else None
        if match_pass is not None and not oldest_first and len(hits) >= limit:
            def _norm(c: str) -> str:
                return " ".join((c or "").split()).lower()
            distinct = len({(h["thread_id"], _norm(h.get("full_content") or "")) for h in hits})
            if distinct * 2 < len(hits):
                # Whole stage in one bucket, hydration included: the rescan is a
                # single extra SQL shape that either runs or doesn't, and its cost
                # is worth knowing against the passes rather than split within.
                _t = perf_counter()
                _rescan_distinct(s, match_pass, shared, shared_params, limit, seen, hits)
                _probe.record("rescan_ms", _t)
    return hits


def _rescan_distinct(
    s: Session, match_pass: _Pass, shared: list[str], shared_params: dict,
    limit: int, seen: set, hits: list[EventHit],
) -> None:
    """Fold a duplicate-flooded MATCH pass to one representative per distinct
    ``(thread_id, content)`` and merge the survivors into ``hits`` — the buried,
    distinct answer among them. Streams the top ``_FLOOD_RESCAN_CAP`` by rank
    (snippet computed only there, in the MATCH context a GROUP BY can't provide),
    then keeps the best-ranked row of each content group. Fail-soft: the flood
    guard is an enhancement, so any error leaves the already-gathered pool intact."""
    inner = (
        "SELECT event_id, thread_id, event_type, content_type, occurred_at, "
        "snippet(event_search, 0, '', '', ' … ', 12) AS snip, content AS full_content, rank AS rk "
        "FROM event_search WHERE " + " AND ".join([match_pass.where] + shared) +
        " ORDER BY rank LIMIT :flood_cap"
    )
    sql = sa_text(
        "SELECT event_id, thread_id, event_type, content_type, occurred_at, snip, full_content, "
        "MIN(rk) AS bm25 "
        "FROM (" + inner + ") GROUP BY thread_id, full_content ORDER BY MIN(rk) LIMIT :lim"
    )
    params = {**shared_params, **match_pass.params, "flood_cap": _FLOOD_RESCAN_CAP}
    try:
        rows = s.execute(sql, params).mappings().all()
    except Exception:  # noqa: BLE001 — the rescan is a best-effort reachability boost
        logger.debug("duplicate-flood rescan skipped", exc_info=True)
        return
    for r in rows:
        key = (r["event_id"], r["content_type"])
        if key in seen:
            continue
        seen.add(key)
        hits.append(build_event_hit(
            event_id=r["event_id"], thread_id=r["thread_id"], event_type=r["event_type"],
            content_type=r["content_type"], snippet=r["snip"] or "",
            full_content=r["full_content"] or "", occurred_at=r["occurred_at"],
            bm25=r["bm25"],
        ))


#: How many matched rows the exact-set queries below will scan before giving up
#: on an exact answer. The scan is the whole cost of both (the GROUP BY forfeits
#: FTS5's streaming rank-sort, so a broad query would otherwise walk its entire
#: match list — measured ~7s for a 460k-match term over this corpus). Capped and
#: taken newest-first, the same query lands in well under a second and the answer
#: degrades to an honest floor rather than to a slow exact one. 20k is chosen to
#: cover realistic enumeration targets outright: a term appearing in ~1k threads
#: resolves exactly, and only corpus-common words hit the cap.
SET_SCAN_CAP = 20000


def _set_scan_sql(select_cols: str, predicate: str, shared: list[str], *, group: str = "") -> str:
    """SQL for a capped exact-set query: take the newest ``SET_SCAN_CAP`` matched
    rows, then aggregate. The cap sits on the *inner* scan, so it bounds the work
    rather than the output — a ``LIMIT`` on the aggregate would still walk every
    match to produce the groups it then discards.

    Newest-first (``rowid DESC`` — the FTS rowid is append-ordered, so this is an
    index walk, not a sort) because a capped enumeration has to drop *something*
    and the recent end is what the rest of the system biases toward; ordering
    would otherwise fall to fts5's internal rowid-ascending iteration and silently
    return the OLDEST slice of a truncated set."""
    inner = (
        "SELECT thread_id, event_id, occurred_at FROM event_search WHERE "
        + " AND ".join([predicate] + shared)
        + " ORDER BY rowid DESC LIMIT :set_cap"
    )
    return "SELECT " + select_cols + " FROM (" + inner + ")" + group


#: Upper bound on how long a memoized exact-set answer is served (see
#: :func:`_set_memo_get`). The watermark below catches appended rows outright, so
#: this only bounds what a watermark cannot see — an in-place update, a reindex's
#: deletes — and a minute is short against the cadence any of those run at.
_SET_MEMO_TTL_S = 60.0

#: Distinct exact-set answers kept. The set queries are per (query, scope), and the
#: pattern this exists for is one caller walking one query's pages, so a handful
#: covers it with room for a few interleaved callers. Each entry is at most one row
#: per matched thread, which the corpus itself bounds.
_SET_MEMO_MAX = 8

_set_memo: "OrderedDict[tuple, tuple[float, Any]]" = OrderedDict()
_set_memo_lock = threading.Lock()
_set_memo_hits = 0
_set_memo_misses = 0


def set_memo_stats() -> dict:
    """How the exact-set memo is doing: ``{entries, hits, misses}``.

    A memo whose hit rate is zero is pure overhead wearing the shape of an
    optimization, and nothing else in a served page distinguishes the two — the
    latency it saves is exactly the latency it would have cost. Counted so the
    question is answerable from outside (the counters mirror
    :class:`.pool_cache.PoolCache`'s)."""
    with _set_memo_lock:
        return {"entries": len(_set_memo), "hits": _set_memo_hits, "misses": _set_memo_misses}


def _set_watermark(session: Optional[Session]) -> object:
    """The index's append watermark — the FTS rowid high-water mark, which moves on
    every indexed event. Part of the memo key, so ingest invalidates a memoized set
    rather than the memo hiding rows that arrived after it. An unreadable watermark
    returns a unique object, which can never equal a stored key: the memo misses and
    the scan runs, which is the safe direction."""
    try:
        with use_session(session) as s:
            return s.execute(sa_text("SELECT max(rowid) FROM event_search")).scalar()
    except Exception:  # noqa: BLE001 — a memo probe must never break a search
        logger.debug("exact-set memo: watermark probe failed", exc_info=True)
        return object()


def _set_memo_get(key: tuple) -> Any:
    """A memoized exact-set answer for ``key``, or ``None``.

    The exact-set scan is the one stage whose cost does not depend on the page
    being asked for: ``group='browse'`` resolves the whole match set to decide
    membership and totals, then slices one page out of it — so a caller walking
    N pages pays the identical scan N times, and it is the largest stage in that
    walk. Memoizing it is also what makes the walk *coherent*: pages are sold as
    slices of one ordering, and a set re-resolved per page against a moving index
    can drop a row a later page was counting on.
    """
    global _set_memo_hits, _set_memo_misses
    now = time.monotonic()
    with _set_memo_lock:
        entry = _set_memo.get(key)
        if entry is not None and now - entry[0] > _SET_MEMO_TTL_S:
            del _set_memo[key]
            entry = None
        if entry is None:
            _set_memo_misses += 1
            return None
        _set_memo_hits += 1
        _set_memo.move_to_end(key)
        return entry[1]


def _set_memo_put(key: tuple, value: Any) -> None:
    """Store ``value`` under ``key``, evicting least-recently-used past the cap."""
    with _set_memo_lock:
        _set_memo[key] = (time.monotonic(), value)
        _set_memo.move_to_end(key)
        while len(_set_memo) > _SET_MEMO_MAX:
            _set_memo.popitem(last=False)


def reset_set_memo() -> None:
    """Drop every memoized exact-set answer and its counters. For where a stale set
    would be *wrong* rather than merely dated — reindex (rows that must stop being
    counted) — mirroring :func:`.vectors.reset_matrix_cache`."""
    global _set_memo_hits, _set_memo_misses
    with _set_memo_lock:
        _set_memo.clear()
        _set_memo_hits = _set_memo_misses = 0


def _set_memo_key(
    kind: str, where: str, params: dict, shared: list[str], shared_params: dict,
    session: Optional[Session],
) -> tuple:
    """The memo key for one exact-set query: the SQL it would run, its bound values,
    and the index it would run against.

    Built from the resolved predicate and filters rather than from the caller's
    arguments, so it names exactly what the answer depends on — two spellings of one
    scope share an entry, and a scope field that reaches the SQL cannot be left out
    of the key by omission. ``id(get_engine())`` scopes it to the open archive: a
    process holding two homes (the ``use_engine`` seam) must not serve one's set as
    the other's."""
    return (
        kind, id(get_engine()), where,
        tuple(sorted(params.items())), tuple(shared), tuple(sorted(shared_params.items())),
        _set_watermark(session),
    )


def matched_threads(
    query: str,
    *,
    match_mode: str = "token",
    startswith: Optional[str] = None,
    session: Optional[Session] = None,
    **scope,
) -> tuple[list[dict], bool]:
    """Every thread the query matches, tallied — the exact-set half of a
    thread-granular list, as ``(rows, capped)``.

    Rows are ``{thread_id, n_hits, event_id, last_match}``, newest match first;
    ``event_id`` is the thread's newest matching event, so a row opens where the
    query landed rather than at the thread's tail. ``capped`` is True when the
    scan hit :data:`SET_SCAN_CAP` and the enumeration is therefore a floor.

    This is the query that makes a *complete* answer possible. The candidate pool
    :func:`search_events` returns is a cut — ``pool_floor`` rows deep, ordered by
    relevance — so the threads past it are unreachable at any page depth and,
    worse, indistinguishable from a set that simply ended. Here the set is
    resolved directly and ranking is a separate question applied on top of it.
    ``**scope`` takes the :func:`_shared_filters` arguments verbatim.
    """
    ensure_fts(session)
    if match_mode not in MATCH_MODES:
        raise ValueError("match must be 'token' or 'substring'")
    if scope.get("thread_ids") is not None and not scope["thread_ids"]:
        return [], False
    predicate = _primary_predicate(query, match_mode=match_mode, startswith=startswith)
    if predicate is None:
        return [], False
    where, params = predicate
    shared, shared_params = _shared_filters(**scope)
    _t = perf_counter()
    key = _set_memo_key("threads", where, params, shared, shared_params, session)
    memo = _set_memo_get(key)
    if memo is not None:
        # Fresh dicts per hand-out: the rows travel into a caller that builds hits
        # beside them, and a shared dict is one careless write away from a memoized
        # answer that drifts from the query it answers.
        _probe.record("set_ms", _t)
        return [dict(r) for r in memo[0]], memo[1]
    sql = sa_text(_set_scan_sql(
        "thread_id, count(*) AS n_hits, max(event_id) AS event_id, "
        "max(occurred_at) AS last_match",
        where, shared, group=" GROUP BY thread_id ORDER BY last_match DESC",
    ))
    with use_session(session) as s:
        rows = s.execute(sql, {**shared_params, **params, "set_cap": SET_SCAN_CAP}).mappings().all()
    _probe.record("set_ms", _t)
    total_hits = sum(r["n_hits"] for r in rows)
    result = ([dict(r) for r in rows], total_hits >= SET_SCAN_CAP)
    _set_memo_put(key, result)
    return [dict(r) for r in result[0]], result[1]


def count_matches(
    query: str,
    *,
    match_mode: str = "token",
    startswith: Optional[str] = None,
    session: Optional[Session] = None,
    **scope,
) -> tuple[int, int, bool]:
    """The match set's size as ``(n_events, n_threads, capped)`` — what a result
    page is a page *of*.

    The event-granular counterpart to :func:`matched_threads`, under the same
    :data:`SET_SCAN_CAP`; ``capped`` True means both numbers are floors. An exact
    uncapped count is deliberately not offered: over this corpus a common term
    costs seconds to count exactly, and every search would pay it to render one
    header line."""
    ensure_fts(session)
    if match_mode not in MATCH_MODES:
        raise ValueError("match must be 'token' or 'substring'")
    if scope.get("thread_ids") is not None and not scope["thread_ids"]:
        return 0, 0, False
    predicate = _primary_predicate(query, match_mode=match_mode, startswith=startswith)
    if predicate is None:
        return 0, 0, False
    where, params = predicate
    shared, shared_params = _shared_filters(**scope)
    _t = perf_counter()
    key = _set_memo_key("count", where, params, shared, shared_params, session)
    memo = _set_memo_get(key)
    if memo is not None:
        _probe.record("set_ms", _t)
        return memo
    sql = sa_text(_set_scan_sql(
        "count(*) AS n_events, count(DISTINCT thread_id) AS n_threads", where, shared,
    ))
    with use_session(session) as s:
        row = s.execute(sql, {**shared_params, **params, "set_cap": SET_SCAN_CAP}).mappings().one()
    _probe.record("set_ms", _t)
    result = (row["n_events"], row["n_threads"], row["n_events"] >= SET_SCAN_CAP)
    _set_memo_put(key, result)
    return result


def _write_doc(
    session: Session,
    *,
    event_id: int,
    thread_id: str,
    event_type: str,
    content: str,
    content_type: Optional[str],
    tool_name: Optional[str],
    occurred_at: Optional[str],
) -> None:
    """Write one search doc — a single ``events_fts`` shadow row; the sync
    triggers mirror it into ``event_search``. The single incremental-write seam;
    ``rebuild_fts`` is the one exception (it bulk-loads the shadow with the
    triggers dropped and retokenizes the index in one 'rebuild' pass)."""
    session.add(EventFts(
        event_id=event_id, thread_id=thread_id, event_type=event_type,
        content=content, content_type=content_type, tool_name=tool_name,
        occurred_at=occurred_at,
    ))

# Thread-meta docs: the thread's title and short summary, indexed as searchable
# docs so "find the thread about X" works when X never appears verbatim in a
# message. Rows carry event_type='thread_meta' and content_type 'title'/'summary',
# anchored to the thread's first indexed event so every hit keeps a real
# [thread/event] anchor (reading from it lands at the thread's opening).
THREAD_META_EVENT_TYPE = "thread_meta"
THREAD_META_CONTENT_TYPES = ("title", "summary")


def _thread_meta_desired(s: Session, thread_ids: Optional[list[str]]) -> dict[tuple[str, str], str]:
    """The meta docs that *should* exist: ``(thread_id, content_type) → content``.
    Conversations only (topics read through their pages, not meta docs), search-
    excluded threads omitted, empty title/summary omitted."""
    where = "t.thread_type = 'conversation' AND NOT t.exclude_from_search"
    params: dict = {}
    if thread_ids is not None:
        if not thread_ids:
            return {}
        where += " AND " + _in_clause("t.id", list(thread_ids), "tid", params, negate=False)
    rows = s.execute(
        sa_text("SELECT t.id, t.title, t.summary FROM threads t WHERE " + where), params
    ).all()
    desired: dict[tuple[str, str], str] = {}
    for tid, title, summary in rows:
        if title and title.strip():
            desired[(tid, "title")] = title.strip()
        if summary and summary.strip():
            desired[(tid, "summary")] = summary.strip()
    return desired


def index_thread_meta(session: Optional[Session] = None, thread_ids: Optional[list[str]] = None) -> int:
    """Sync thread titles + short summaries into the FTS surface (shadow + FTS5)
    as thread-meta docs. Diff-based: an unchanged thread writes nothing, a changed
    title/summary replaces its rows (and drops its stale vector so the embed cohost
    re-embeds it), a vanished one is deleted. ``thread_ids=None`` syncs every
    thread — cheap enough for the watcher's maintenance cadence. Returns the
    number of rows written."""
    ensure_fts(session)
    with use_session(session) as s:
        desired = _thread_meta_desired(s, thread_ids)

        params: dict = {"met": THREAD_META_EVENT_TYPE}
        scope = ""
        if thread_ids is not None:
            if not thread_ids:
                return 0
            scope = " AND " + _in_clause("thread_id", list(thread_ids), "tid", params, negate=False)
        existing = {
            (r.thread_id, r.content_type): (r.id, r.event_id, r.content)
            for r in s.execute(
                sa_text(
                    "SELECT id, event_id, thread_id, content_type, content FROM events_fts "
                    "WHERE event_type = :met" + scope
                ),
                params,
            )
        }

        stale = [k for k, (_, _, content) in existing.items() if desired.get(k) != content]
        fresh = [k for k, content in desired.items() if existing.get(k, (None, None, None))[2] != content]
        if not stale and not fresh:
            return 0

        # Anchor each thread at its first indexed event; a thread with no indexed
        # events gets no meta docs (nothing to anchor a read to).
        need_anchor = sorted({tid for tid, _ in fresh})
        anchors: dict[str, tuple[int, Optional[str]]] = {}
        for chunk_start in range(0, len(need_anchor), 500):
            chunk = need_anchor[chunk_start:chunk_start + 500]
            aparams: dict = {"met": THREAD_META_EVENT_TYPE}
            rows = s.execute(
                sa_text(
                    "SELECT f.thread_id, MIN(f.event_id) FROM events_fts f "
                    "WHERE f.event_type != :met AND "
                    + _in_clause("f.thread_id", chunk, "tid", aparams, negate=False)
                    + " GROUP BY f.thread_id"
                ),
                aparams,
            ).all()
            eids = [eid for _, eid in rows]
            oparams: dict = {}
            occurred: dict = {
                row[0]: row[1]
                for row in s.execute(
                    sa_text(
                        "SELECT id, occurred_at FROM events WHERE "
                        + _in_clause("id", eids, "eid", oparams, negate=False)
                    ),
                    oparams,
                )
            } if eids else {}
            for tid, eid in rows:
                anchors[tid] = (eid, str(occurred[eid]) if occurred.get(eid) else None)

        # Probe once instead of catching mid-transaction: a failed statement would
        # poison the session (lexical-only installs have no vector table).
        have_vectors = bool(s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": "event_vectors"}
        ).scalar())

        written = 0
        for key in stale:
            tid, ct = key
            rid, old_eid, _ = existing[key]
            # The shadow delete cascades into event_search via the sync trigger.
            s.execute(sa_text("DELETE FROM events_fts WHERE id = :rid"), {"rid": rid})
            # Drop the doc's vector so the embed cohost's missing-vector anti-join
            # re-embeds the replacement (or forgets a removed doc).
            if have_vectors:
                s.execute(
                    sa_text("DELETE FROM event_vectors WHERE event_id = :eid AND content_type = :ct"),
                    {"eid": old_eid, "ct": ct},
                )
        for key in fresh:
            tid, ct = key
            if tid not in anchors:
                continue
            eid, oa = anchors[tid]
            _write_doc(
                s, event_id=eid, thread_id=tid, event_type=THREAD_META_EVENT_TYPE,
                content=desired[key], content_type=ct, tool_name=None, occurred_at=oa,
            )
            written += 1
        if session is None:
            s.commit()
    if written or stale:
        logger.info("index_thread_meta: wrote %d meta docs (%d removed)", written, len(stale))
    return written


# The twins whose presence gates api_request_completed indexing (see the NB in
# _extract.INDEXABLE_EVENT_TYPES): index the summary ONLY for api_calls with no
# granular twin — matched by api_call_id, then by text (a doubly captured thread
# carries the file importer's twins under different api_call_ids than the live
# stream's summaries). The same rule, by the same names, drives what the reader
# synthesizes (read._absorb_stream_deltas) — search hits and rendered transcripts
# must agree on whose copy of the text exists.
_ARC_TWIN_TYPES = ("text_complete", "thinking_complete")


def _cached_twin_texts(session: Session, thread_id: str, cache: dict) -> set[str]:
    """The thread's twin-text set via the per-run cache — the one place the
    cache's bound (drop everything past 64 threads; a rebuild walks threads in
    id-clusters, so evicting wholesale is cheap and simple) is enforced."""
    if thread_id not in cache:
        if len(cache) > 64:
            cache.clear()
        cache[thread_id] = _twin_texts(session, thread_id)
    return cache[thread_id]


def _twin_texts(session: Session, thread_id: str) -> set[str]:
    """The (stripped) texts of a thread's granular complete events."""
    rows = session.execute(
        select(Event.payload).where(
            Event.thread_id == thread_id, Event.event_type.in_(_ARC_TWIN_TYPES)
        )
    ).scalars()
    out: set[str] = set()
    for payload in rows:
        p = payload if isinstance(payload, dict) else json.loads(payload)
        t = (p.get("text") or "").strip()
        if t:
            out.add(t)
    return out


def stitch_delta_tuples(session: Session, api_call_id: str) -> list:
    """``(content, content_type, tool_name, anchor_event_id)`` tuples stitched
    from a call's raw delta events — the fallback when its
    ``api_request_completed`` carries no ``content_blocks`` (some recovery paths
    don't reconstruct them), or never arrived at all, so the text exists nowhere
    else. Mirrors the reader's stitch in ``read._synthesize_completes``:
    concatenate per ``block_index``, in order. ``anchor_event_id`` is each
    block's last delta event — the anchor arc-less calls index and render
    under; calls that do have a summary anchor there instead (the caller's
    choice)."""
    rows = session.execute(
        select(Event.id, Event.event_type, Event.payload)
        .where(Event.api_call_id == api_call_id, Event.event_type.in_(("text_delta", "thinking_delta")))
        .order_by(Event.id)
    ).all()
    runs: dict = {}  # block_index -> [event_type, [texts], last_delta_id]
    for eid, et, payload in rows:
        p = payload if isinstance(payload, dict) else json.loads(payload)
        run = runs.setdefault(p.get("block_index", 0), [et, [], eid])
        run[1].append(p.get("text", ""))
        run[2] = eid
    out = []
    for _, (et, texts, anchor) in sorted(runs.items()):
        text = "".join(texts)
        if text.strip():
            out.append((text, "thinking" if et == "thinking_delta" else "text", None, anchor))
    return out


def _gate_arc_tuples(
    session: Session,
    *,
    api_call_id: Optional[str],
    thread_id: str,
    tuples: list,
    twin_text_cache: dict,
    twinned_calls: Optional[set] = None,
) -> list:
    """Apply the twin gate to an ``api_request_completed`` event's extracted
    tuples: nothing when the api_call has a granular twin; otherwise only the
    blocks no twin in the thread already carries. Emitted texts join the cache
    set, so a duplicated summary can't index twice. ``twinned_calls`` is the
    precomputed twin api_call_id set (rebuild); absent, the twin-call check is
    one indexed query (incremental)."""
    if api_call_id is None:
        return []  # can't prove no twin — never risk double-counting
    if twinned_calls is not None:
        if api_call_id in twinned_calls:
            return []
    elif session.execute(
        select(Event.id)
        .where(Event.api_call_id == api_call_id, Event.event_type.in_(_ARC_TWIN_TYPES))
        .limit(1)
    ).first():
        return []
    if not tuples:
        # A summary with no content_blocks: the call's text exists only as raw
        # deltas — stitch them, anchored (by the caller) to this summary event.
        tuples = [(c, ct, tn) for c, ct, tn, _ in stitch_delta_tuples(session, api_call_id)]
    seen = _cached_twin_texts(session, thread_id, twin_text_cache)
    out = []
    for content, content_type, tool_name in tuples:
        key = content.strip()
        if key and key not in seen:
            seen.add(key)
            out.append((content, content_type, tool_name))
    return out


def index_events(session: Session, events: list) -> int:
    """Index a batch of just-written events into the FTS surface (shadow + FTS5).

    Called from the import write seam so FTS stays current without a full rebuild,
    in the same transaction as the event writes. Events are already deduped, so an
    event is indexed exactly once.
    """
    ensure_fts(session)
    indexed = 0
    twin_text_cache: dict = {}
    for ev in events:
        if ev.event_type not in INDEXABLE_EVENT_TYPES:
            continue
        payload = ev.payload if isinstance(ev.payload, dict) else json.loads(ev.payload)
        # Canonical form (naive-UTC, space-separated) so incremental rows collate
        # with rebuilt rows — rebuild_fts copies events.occurred_at as SQLite
        # rendered it, and the since/until bounds are resolved to the same form.
        oa = canonical_time_bound(ev.occurred_at) if ev.occurred_at else None
        tuples = extract_fts_content(ev.event_type, payload)
        if ev.event_type == "api_request_completed":
            # The batch's own twins are visible to the queries via autoflush.
            tuples = _gate_arc_tuples(
                session, api_call_id=ev.api_call_id, thread_id=ev.thread_id,
                tuples=tuples, twin_text_cache=twin_text_cache,
            )
        for content, content_type, tool_name in tuples:
            _write_doc(
                session, event_id=ev.id, thread_id=ev.thread_id, event_type=ev.event_type,
                content=content, content_type=content_type,
                tool_name=tool_name[:200] if tool_name else None, occurred_at=oa,
            )
            indexed += 1
    return indexed


def rebuild_fts(session: Optional[Session] = None) -> int:
    """Rebuild the FTS surface from events: derive the ``events_fts`` shadow from
    the indexable events, then retokenize ``event_search`` from it (the FTS5
    'rebuild' command — an external-content index refills from its content table).

    The sync triggers are dropped for the bulk refill — per-row trigger firings
    would tokenize the corpus once on the DELETE and again on the refill — and
    recreated before the thread-meta sync, which writes through them.

    The derived index is rebuilt from scratch (clear-and-refill) — the simplest
    correct reindex. Returns the number of indexed documents.
    """
    ensure_fts(session)
    own = session is None
    with use_session(session) as s:
        _drop_triggers(s)
        # 1. Derive the events_fts shadow from the events, in id-keyset batches so a
        #    multi-million-event corpus never materializes at once. Each batch's
        #    SELECT is fully read before its Core insert, so there's no open
        #    read-cursor during the writes.
        s.execute(delete(EventFts))
        s.flush()
        fts_table = EventFts
        conn = s.connection()
        # The twin gate's call-id side, precomputed once (one indexed scan)
        # instead of a query per api_request_completed row.
        twinned_calls = set(
            s.execute(
                select(Event.api_call_id)
                .where(Event.event_type.in_(_ARC_TWIN_TYPES), Event.api_call_id.isnot(None))
                .distinct()
            ).scalars()
        )
        twin_text_cache: dict = {}
        last_id = 0
        while True:
            rows = s.execute(
                select(Event.id, Event.thread_id, Event.event_type, Event.payload,
                       Event.api_call_id)
                .where(Event.event_type.in_(INDEXABLE_EVENT_TYPES), Event.id > last_id)
                .order_by(Event.id)
                .limit(5000)
            ).all()
            if not rows:
                break
            batch = []
            for eid, tid, etype, payload, api_call_id in rows:
                last_id = eid
                p = payload if isinstance(payload, dict) else json.loads(payload)
                tuples = extract_fts_content(etype, p)
                if etype == "api_request_completed":
                    tuples = _gate_arc_tuples(
                        s, api_call_id=api_call_id, thread_id=tid, tuples=tuples,
                        twin_text_cache=twin_text_cache, twinned_calls=twinned_calls,
                    )
                for content, content_type, tool_name in tuples:
                    batch.append({
                        "event_id": eid, "thread_id": tid, "event_type": etype,
                        "content": content, "content_type": content_type,
                        "tool_name": tool_name[:200] if tool_name else None,
                    })
            if batch:
                conn.execute(insert(fts_table), batch)

        # 1b. Arc-less calls: a stream killed before its api_request_completed
        # arrived left deltas with no summary event for the main loop to hang
        # rows on. Stitch each such call's text and anchor it at the block's
        # last delta event — the same anchor the reader renders these under.
        # Rebuild-only on purpose: incrementally, "the summary never arrives"
        # is only knowable in hindsight, and indexing early would double up
        # when it does arrive.
        arc_calls = set(
            s.execute(
                select(Event.api_call_id)
                .where(Event.event_type == "api_request_completed", Event.api_call_id.isnot(None))
                .distinct()
            ).scalars()
        )
        orphan_calls = s.execute(
            select(Event.api_call_id, Event.thread_id)
            .where(Event.event_type.in_(("text_delta", "thinking_delta")), Event.api_call_id.isnot(None))
            .distinct()
        ).all()
        orphan_batch = []
        for ac, tid in orphan_calls:
            if ac in arc_calls or ac in twinned_calls:
                continue
            seen = _cached_twin_texts(s, tid, twin_text_cache)
            for content, content_type, tool_name, anchor in stitch_delta_tuples(s, ac):
                key = content.strip()
                if key and key not in seen:
                    seen.add(key)
                    orphan_batch.append({
                        "event_id": anchor, "thread_id": tid,
                        "event_type": "text_delta" if content_type == "text" else "thinking_delta",
                        "content": content, "content_type": content_type,
                        "tool_name": tool_name,
                    })
        if orphan_batch:
            conn.execute(insert(fts_table), orphan_batch)

        # 1c. Stamp occurred_at onto the refilled shadow in one set-based pass —
        #     the stored text copied verbatim, so rebuilt rows collate with the
        #     incremental writer's canonical_time_bound strings (same rendering).
        s.execute(sa_text(
            "UPDATE events_fts SET occurred_at = "
            "(SELECT e.occurred_at FROM events e WHERE e.id = events_fts.event_id)"
        ))

        # 2. Retokenize the FTS5 index from the shadow, then restore the sync
        #    triggers so the thread-meta sync below (and every later writer)
        #    mirrors through them.
        s.execute(sa_text("INSERT INTO event_search(event_search) VALUES('rebuild')"))
        _create_triggers(s)

        # 3. Derive the thread-meta docs (titles + summaries). The shadow refill
        #    above dropped them, so the sync sees a clean slate and writes them all.
        index_thread_meta(s)

        count = s.execute(sa_text("SELECT count(*) FROM event_search")).scalar() or 0
        if own:
            s.commit()
    # A clear-and-refill re-mints the rowids, so the exact-set memo's watermark can
    # land back where it started over a wholly different index. Drop the memo rather
    # than trust a number that just stopped meaning what it meant.
    reset_set_memo()
    logger.info("rebuild_fts: indexed %d documents", count)
    return int(count)


def fts_status(session: Optional[Session] = None) -> dict:
    with use_session(session) as s:
        exists = s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": "event_search"}
        ).scalar()
        count = s.execute(sa_text("SELECT count(*) FROM event_search")).scalar() if exists else 0
    return {"indexed": int(count or 0), "table": "event_search"}
