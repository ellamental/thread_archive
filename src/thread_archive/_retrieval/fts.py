"""SQLite FTS5 lexical search.

The embedded FTS arm, SQLite-native throughout. One in-DB FTS5 virtual table
(``event_search``) in **external-content** mode over the ``events_fts`` shadow,
which is in turn derived from the events: the FTS table holds only the inverted
index — column reads, snippets, and LIKE scans resolve through the shadow by
rowid, so the corpus text is stored once, not twice. Shadow→index sync is
trigger-based (``events_fts_ai``/``_ad``/``_au``): every writer — incremental
import, thread-meta sync, redaction — writes the shadow alone and the triggers
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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, insert, select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .._store import Event, EventFts, use_session
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


def build_event_hit(
    *,
    event_id: int,
    thread_id: str,
    event_type: str,
    content_type: Optional[str],
    snippet: str,
    full_content: str,
    occurred_at: Optional[str],
) -> EventHit:
    """One event search hit in the canonical shape. ``thread_title`` is enriched
    by the caller. ``occurred_at`` is the stored column text (canonical naive
    form); it parses to a naive datetime — a stray offset-carrying value is
    normalized to local-naive so every hit's datetime compares against the rest."""
    dt: Optional[datetime] = None
    if occurred_at:
        try:
            dt = datetime.fromisoformat(str(occurred_at))
        except ValueError:
            dt = None
        else:
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
    return {
        "event_id": event_id,
        "thread_id": thread_id,
        "thread_title": None,
        "event_type": event_type,
        "content_type": content_type,
        "snippet": snippet,
        "full_content": full_content,
        "occurred_at": dt,
    }


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


def _quote_all_tokens(text_: str) -> str:
    """Every whitespace token as a quoted FTS5 phrase term — no operators, no
    syntax, so the expression can never raise. The demotion target for malformed
    boolean shapes and the retry form for a residual fts5 syntax error."""
    toks = []
    for tok in re.findall(r"\S+", text_ or ""):
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
    for tok in re.findall(r'"[^"]*"|\S+', query or ""):
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
    return text_.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    return '"' + text_.replace('"', "") + '"'


@dataclass(frozen=True)
class _Pass:
    """One candidate-gathering pass of :func:`search_events`: a WHERE fragment
    plus its bound params, the ORDER BY, whether the fragment is an FTS5 MATCH
    (drives the snippet expression and the fts5-syntax-error retry), and whether
    the pass is a fallback — a substring LIKE full-table scan that only runs when
    the passes before it left the candidate pool short."""

    where: str
    params: dict = field(default_factory=dict)
    order: str = _RANK_EXPR
    use_match: bool = True
    is_fallback: bool = False


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
    *,
    thread_ids: Optional[list[str]] = None,
    agents: str = "exclude",
    oldest_first: bool = False,
    or_fallback: bool = True,
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
    """
    ensure_fts(session)
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
                            order="occurred_at DESC", use_match=False, is_fallback=True))
    elif mode == "code":
        clean = _clean_query_text(query)
        passes.append(_Pass("event_search MATCH :q", {"q": _quote_phrase(clean)}))
        passes.append(_Pass("content LIKE :codepat ESCAPE '\\'", {"codepat": _like_substring(clean)},
                            order="occurred_at DESC", use_match=False, is_fallback=True))
    else:
        passes.append(_Pass("event_search MATCH :q", {"q": to_match_query(query)}))

    shared: list[str] = []
    shared_params: dict = {"lim": limit}
    if thread_id is not None:
        shared.append("thread_id = :tid")
        shared_params["tid"] = thread_id
    elif thread_ids is not None:
        # A resolved id-set scope (e.g. a topic's member threads). Like a single
        # explicit thread_id, the scope is deliberate and bypasses the blacklist.
        shared.append(_in_clause("thread_id", thread_ids, "tids", shared_params, negate=False))
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
        shared_params["tool"] = tool_name
    if types:
        # event_search carries thread_id but not thread_type; constrain via the
        # threads table (idx_threads_type), same pattern as the source filter.
        shared.append(
            "thread_id IN (SELECT id FROM threads WHERE "
            + _in_clause("thread_type", types, "tt", shared_params, negate=False) + ")"
        )
    if content_types:
        shared.append(_in_clause("content_type", content_types, "ct", shared_params, negate=False))
    if exclude_content_types:
        shared.append(_in_clause("content_type", exclude_content_types, "xct", shared_params, negate=True))
    if source:
        # event_search carries thread_id but not source; constrain to threads of
        # the named provider(s) via an indexed subquery (idx_threads_source). An
        # empty match yields no rows rather than invalid SQL.
        shared.append(
            "thread_id IN (SELECT id FROM threads WHERE "
            + _in_clause("source", source, "src", shared_params, negate=False) + ")"
        )
    if since:
        shared.append("occurred_at >= :since")
        shared_params["since"] = since
    if until:
        shared.append("occurred_at <= :until")
        shared_params["until"] = until

    hits: list[EventHit] = []
    seen: set[tuple[int, Optional[str]]] = set()
    with use_session(session) as s:
        def run_pass(p: _Pass) -> None:
            order = "occurred_at ASC" if oldest_first else p.order
            snippet_expr = (
                "snippet(event_search, 0, '', '', ' … ', 12)" if p.use_match
                else "substr(content, 1, 300)"
            )
            sql = sa_text(
                "SELECT event_id, thread_id, event_type, content_type, occurred_at, "
                + snippet_expr + " AS snippet, content AS full_content "
                "FROM event_search WHERE " + " AND ".join([p.where] + shared) +
                " ORDER BY " + order + " LIMIT :lim"
            )
            try:
                rows = s.execute(sql, {**shared_params, **p.params}).mappings().all()
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
                         for k, v in p.params.items()}
                rows = s.execute(sql, {**shared_params, **retry}).mappings().all()
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
                ))

        for p in passes:
            if p.is_fallback and len(hits) >= limit:
                continue
            run_pass(p)

        # Fallback OR tier for plain natural-language queries (see the docstring):
        # only when the strict all-terms pass left the pool short, and only over
        # the meaningful terms (stopwords carry no retrieval signal). Explicit
        # boolean / quoted / pipe-OR / identifier queries asked for their own
        # semantics and are left alone.
        if (or_fallback and startswith is None and mode == "tsquery" and not is_boolean
                and '"' not in (query or "") and len(hits) < limit):
            from .rank import search_terms

            terms = [t for t in search_terms(query) if " " not in t]
            or_q = " OR ".join(_quote_phrase(t) for t in terms)
            # Skip when the tier would be the strict pass verbatim (single term,
            # nothing dropped) — same MATCH, nothing new to add.
            if terms and or_q.lower() != to_match_query(query).lower():
                run_pass(_Pass("event_search MATCH :orq", {"orq": or_q}))
    return hits


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
    Conversations only (topics have their own librarian search surface), search-
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
    logger.info("rebuild_fts: indexed %d documents", count)
    return int(count)


def fts_status(session: Optional[Session] = None) -> dict:
    with use_session(session) as s:
        exists = s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": "event_search"}
        ).scalar()
        count = s.execute(sa_text("SELECT count(*) FROM event_search")).scalar() if exists else 0
    return {"indexed": int(count or 0), "table": "event_search"}
