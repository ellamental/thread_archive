"""SQLite FTS5 lexical search.

The embedded FTS arm, SQLite-native throughout. One in-DB FTS5 virtual table
(``event_search``) over event content, derived from the ``events_fts`` shadow,
which is in turn derived from the events. ``rebuild_fts`` does both derivations
and is the FTS half of ``reindex``.

SQL is composed with literal table names + bound params (never f-strings) — the
no-f-string-SQL convention.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, insert, select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from .._store import Event, EventFts, use_session
from ._classify import canonical_time_bound, classify_query
from ._extract import INDEXABLE_EVENT_TYPES, extract_fts_content

logger = logging.getLogger(__name__)

# Content is indexed; everything else is UNINDEXED so it can still be filtered in
# WHERE (thread/type/tool/time) without bloating the index.
_CREATE_FTS = (
    "CREATE VIRTUAL TABLE event_search USING fts5("
    "content, event_id UNINDEXED, thread_id UNINDEXED, event_type UNINDEXED, "
    "content_type UNINDEXED, tool_name UNINDEXED, occurred_at UNINDEXED, "
    "tokenize = 'porter unicode61')"
)

# Plain FTS5 bm25 (more-negative = better).
_RANK_EXPR = "bm25(event_search)"


def build_event_hit(
    *,
    event_id: int,
    thread_id: int,
    event_type: str,
    content_type: Optional[str],
    snippet: str,
    full_content: str,
    occurred_at_ts: int,
) -> dict:
    """One event search hit in the canonical shape. ``thread_title`` is enriched
    by the caller."""
    return {
        "event_id": event_id,
        "thread_id": thread_id,
        "thread_title": None,
        "event_type": event_type,
        "content_type": content_type,
        "snippet": snippet,
        "full_content": full_content,
        "occurred_at": datetime.fromtimestamp(occurred_at_ts) if occurred_at_ts else None,
    }


def ensure_fts(session: Optional[Session] = None) -> None:
    """Create the FTS5 virtual table if absent. Idempotent."""
    with use_session(session) as s:
        exists = s.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": "event_search"}
        ).scalar()
        if not exists:
            s.execute(sa_text(_CREATE_FTS))
            if session is None:
                s.commit()


def _to_match_query(query: str) -> str:
    """Translate a natural-language / boolean / quoted-phrase query into a safe
    FTS5 MATCH expression. AND/OR/NOT and "quoted phrases" pass through; every
    other token is emitted quoted so stray punctuation can't raise a syntax error."""
    out: list[str] = []
    for tok in re.findall(r'"[^"]*"|\S+', query or ""):
        if tok in ("AND", "OR", "NOT") or tok.startswith('"'):
            out.append(tok)
        else:
            out.append('"' + tok.replace('"', "") + '"')
    return " ".join(out) or '""'


def _clean_query_text(query: str) -> str:
    """Strip quotes + boolean keywords and collapse whitespace — the substring a
    code-identifier / pipe-OR query matches."""
    clean = re.sub(r'["\']', "", query or "")
    clean = re.sub(r"\b(AND|OR|NOT)\b", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _like_prefix(prefix: str) -> str:
    """Escape LIKE wildcards (``\\`` ``%`` ``_``) so ``startswith`` matches a literal
    prefix; pair with ``ESCAPE '\\'`` in the SQL."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


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


def search_events(
    query: str,
    thread_id: Optional[int] = None,
    content_types: Optional[list[str]] = None,
    limit: int = 50,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    exclude_content_types: Optional[list[str]] = None,
    source: Optional[list[str]] = None,
    startswith: Optional[str] = None,
    *,
    oldest_first: bool = False,
    or_fallback: bool = True,
    session: Optional[Session] = None,
) -> list[dict]:
    """Lexical search over the FTS5 index → canonical event-hit dicts.

    Query mode (shared classifier): natural-language / boolean / quoted-phrase
    queries run FTS5 MATCH (bm25-ranked). Pipe-OR and code-identifier shapes run
    TWO passes, merged: a phrase MATCH (the tokenizer splits ``get_session`` into
    ``get session``, so the quoted phrase rides the index, bm25-ranked over the
    whole corpus) plus a substring LIKE over the most recent matches (catches
    within-token substrings MATCH can't see). The MATCH pass is what keeps *old*
    hits reachable for common identifiers — a single recency-ordered LIKE pass
    caps out on the newest ``limit`` matches. ``startswith`` overrides the query
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
    """
    ensure_fts(session)
    mode, is_boolean = classify_query(query)

    # Each pass is (match_where, match_params, order, use_match); shared filters
    # are appended to every pass.
    passes: list[tuple[str, dict, str, bool]] = []
    if startswith is not None:
        # Structural prefix scan — wildcards in the prefix are escaped so it matches
        # a literal prefix (the reference left them unescaped; this hardens it).
        passes.append(("content LIKE :sw ESCAPE '\\'", {"sw": _like_prefix(startswith)},
                       "occurred_at DESC", False))
    elif mode == "or":
        terms = [_clean_query_text(t) for t in (query or "").split("|")]
        terms = [t for t in terms if t]
        if not terms:
            return []
        match_q = " OR ".join(_quote_phrase(t) for t in terms)
        passes.append(("event_search MATCH :q", {"q": match_q}, _RANK_EXPR, True))
        like_params: dict = {}
        ors = []
        for i, term in enumerate(terms):
            like_params["or" + str(i)] = "%" + term + "%"
            ors.append("content LIKE :or" + str(i))
        passes.append(("(" + " OR ".join(ors) + ")", like_params, "occurred_at DESC", False))
    elif mode == "code":
        clean = _clean_query_text(query)
        passes.append(("event_search MATCH :q", {"q": _quote_phrase(clean)}, _RANK_EXPR, True))
        passes.append(("content LIKE :codepat", {"codepat": "%" + clean + "%"},
                       "occurred_at DESC", False))
    else:
        passes.append(("event_search MATCH :q", {"q": _to_match_query(query)}, _RANK_EXPR, True))

    shared: list[str] = []
    shared_params: dict = {"lim": limit}
    if thread_id is not None:
        shared.append("thread_id = :tid")
        shared_params["tid"] = thread_id
    else:
        # Honor the per-thread search blacklist (threads.exclude_from_search).
        # An explicit thread_id scope is deliberate and bypasses it.
        shared.append("thread_id NOT IN (SELECT id FROM threads WHERE exclude_from_search)")
    if tool_name:
        shared.append("tool_name = :tool")
        shared_params["tool"] = tool_name
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

    hits: list[dict] = []
    seen: set[tuple[int, Optional[str]]] = set()
    with use_session(session) as s:
        def run_pass(match_where: str, match_params: dict, order: str, use_match: bool) -> None:
            if oldest_first:
                order = "occurred_at ASC"
            snippet_expr = (
                "snippet(event_search, 0, '', '', ' … ', 12)" if use_match
                else "substr(content, 1, 300)"
            )
            sql = sa_text(
                "SELECT event_id, thread_id, event_type, content_type, occurred_at, "
                + snippet_expr + " AS snippet, content AS full_content "
                "FROM event_search WHERE " + " AND ".join([match_where] + shared) +
                " ORDER BY " + order + " LIMIT :lim"
            )
            rows = s.execute(sql, {**shared_params, **match_params}).mappings().all()
            for r in rows:
                key = (r["event_id"], r["content_type"])
                if key in seen:
                    continue
                seen.add(key)
                ts = 0
                oa = r["occurred_at"]
                if oa:
                    try:
                        ts = int(datetime.fromisoformat(str(oa)).timestamp())
                    except ValueError:
                        ts = 0
                hits.append(build_event_hit(
                    event_id=r["event_id"],
                    thread_id=r["thread_id"],
                    event_type=r["event_type"],
                    content_type=r["content_type"],
                    snippet=r["snippet"] or "",
                    full_content=r["full_content"] or "",
                    occurred_at_ts=ts,
                ))

        for p in passes:
            run_pass(*p)

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
            if terms and or_q.lower() != _to_match_query(query).lower():
                run_pass("event_search MATCH :orq", {"orq": or_q}, _RANK_EXPR, True)
    return hits


_INSERT_SEARCH = sa_text(
    "INSERT INTO event_search "
    "(content, event_id, thread_id, event_type, content_type, tool_name, occurred_at) "
    "VALUES (:content, :event_id, :thread_id, :event_type, :content_type, :tool_name, :occurred_at)"
)

# Thread-meta docs: the thread's title and short summary, indexed as searchable
# docs so "find the thread about X" works when X never appears verbatim in a
# message. Rows carry event_type='thread_meta' and content_type 'title'/'summary',
# anchored to the thread's first indexed event so every hit keeps a real
# [thread/event] anchor (reading from it lands at the thread's opening).
THREAD_META_EVENT_TYPE = "thread_meta"
THREAD_META_CONTENT_TYPES = ("title", "summary")


def _thread_meta_desired(s: Session, thread_ids: Optional[list[int]]) -> dict[tuple[int, str], str]:
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
    desired: dict[tuple[int, str], str] = {}
    for tid, title, summary in rows:
        if title and title.strip():
            desired[(tid, "title")] = title.strip()
        if summary and summary.strip():
            desired[(tid, "summary")] = summary.strip()
    return desired


def index_thread_meta(session: Optional[Session] = None, thread_ids: Optional[list[int]] = None) -> int:
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
        anchors: dict[int, tuple[int, Optional[str]]] = {}
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
            s.execute(sa_text("DELETE FROM events_fts WHERE id = :rid"), {"rid": rid})
            s.execute(
                sa_text(
                    "DELETE FROM event_search WHERE event_type = :met "
                    "AND thread_id = :tid AND content_type = :ct"
                ),
                {"met": THREAD_META_EVENT_TYPE, "tid": tid, "ct": ct},
            )
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
            content = desired[key]
            s.add(EventFts(
                event_id=eid, thread_id=tid, event_type=THREAD_META_EVENT_TYPE,
                content=content, content_type=ct, tool_name=None,
            ))
            s.execute(_INSERT_SEARCH, {
                "content": content, "event_id": eid, "thread_id": tid,
                "event_type": THREAD_META_EVENT_TYPE, "content_type": ct,
                "tool_name": None, "occurred_at": oa,
            })
            written += 1
        if session is None:
            s.commit()
    if written or stale:
        logger.info("index_thread_meta: wrote %d meta docs (%d removed)", written, len(stale))
    return written


def index_events(session: Session, events: list) -> int:
    """Index a batch of just-written events into the FTS surface (shadow + FTS5).

    Called from the import write seam so FTS stays current without a full rebuild,
    in the same transaction as the event writes. Events are already deduped, so an
    event is indexed exactly once.
    """
    ensure_fts(session)
    indexed = 0
    for ev in events:
        if ev.event_type not in INDEXABLE_EVENT_TYPES:
            continue
        payload = ev.payload if isinstance(ev.payload, dict) else json.loads(ev.payload)
        # Canonical form (naive-UTC, space-separated) so incremental rows collate
        # with rebuilt rows — rebuild_fts copies events.occurred_at as SQLite
        # rendered it, and the since/until bounds are resolved to the same form.
        oa = canonical_time_bound(ev.occurred_at) if ev.occurred_at else None
        for content, content_type, tool_name in extract_fts_content(ev.event_type, payload):
            tn = tool_name[:200] if tool_name else None
            session.add(EventFts(
                event_id=ev.id, thread_id=ev.thread_id, event_type=ev.event_type,
                content=content, content_type=content_type, tool_name=tn,
            ))
            session.execute(_INSERT_SEARCH, {
                "content": content, "event_id": ev.id, "thread_id": ev.thread_id,
                "event_type": ev.event_type, "content_type": content_type,
                "tool_name": tn, "occurred_at": oa,
            })
            indexed += 1
    return indexed


def rebuild_fts(session: Optional[Session] = None) -> int:
    """Rebuild the FTS surface from events: derive the ``events_fts`` shadow from
    the indexable events, then (re)populate the ``event_search`` FTS5 table from it.

    The derived index is rebuilt from scratch (clear-and-refill) — the simplest
    correct reindex. Returns the number of indexed documents.
    """
    ensure_fts(session)
    own = session is None
    with use_session(session) as s:
        # 1. Derive the events_fts shadow from the events, in id-keyset batches so a
        #    multi-million-event corpus never materializes at once. Each batch's
        #    SELECT is fully read before its Core insert, so there's no open
        #    read-cursor during the writes.
        s.execute(delete(EventFts))
        s.flush()
        fts_table = EventFts
        conn = s.connection()
        last_id = 0
        while True:
            rows = s.execute(
                select(Event.id, Event.thread_id, Event.event_type, Event.payload)
                .where(Event.event_type.in_(INDEXABLE_EVENT_TYPES), Event.id > last_id)
                .order_by(Event.id)
                .limit(5000)
            ).all()
            if not rows:
                break
            batch = []
            for eid, tid, etype, payload in rows:
                last_id = eid
                p = payload if isinstance(payload, dict) else json.loads(payload)
                for content, content_type, tool_name in extract_fts_content(etype, p):
                    batch.append({
                        "event_id": eid, "thread_id": tid, "event_type": etype,
                        "content": content, "content_type": content_type,
                        "tool_name": tool_name[:200] if tool_name else None,
                    })
            if batch:
                conn.execute(insert(fts_table), batch)

        # 2. (Re)build the FTS5 table from the shadow.
        s.execute(sa_text("DELETE FROM event_search"))
        s.execute(sa_text(
            "INSERT INTO event_search "
            "(content, event_id, thread_id, event_type, content_type, tool_name, occurred_at) "
            "SELECT f.content, f.event_id, f.thread_id, f.event_type, f.content_type, "
            "f.tool_name, e.occurred_at "
            "FROM events_fts f JOIN events e ON e.id = f.event_id "
            "WHERE f.content IS NOT NULL AND f.content != ''"
        ))

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
