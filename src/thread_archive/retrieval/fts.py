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

from ..store import Event, EventFts, use_session
from ._classify import classify_query
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
    session: Optional[Session] = None,
) -> list[dict]:
    """Lexical search over the FTS5 index → canonical event-hit dicts.

    Query mode (shared classifier): pipe-OR and code-identifier shapes match by
    substring LIKE (un-stemmed, recency-ordered); natural-language / boolean /
    quoted-phrase queries run FTS5 MATCH (bm25-ranked). ``startswith`` overrides the
    query mode entirely with a structural prefix scan (content LIKE 'prefix%',
    recency-ordered) — the query text is not matched, only the structural filters.
    """
    ensure_fts(session)
    mode, _is_boolean = classify_query(query)

    params: dict = {"lim": limit}
    if startswith is not None:
        # Structural prefix scan — wildcards in the prefix are escaped so it matches
        # a literal prefix (the reference left them unescaped; this hardens it).
        where = ["content LIKE :sw ESCAPE '\\'"]
        params["sw"] = _like_prefix(startswith)
        order, use_match = "occurred_at DESC", False
    elif mode == "or":
        terms = [_clean_query_text(t) for t in (query or "").split("|")]
        terms = [t for t in terms if t]
        if not terms:
            return []
        ors = []
        for i, term in enumerate(terms):
            params["or" + str(i)] = "%" + term + "%"
            ors.append("content LIKE :or" + str(i))
        where = ["(" + " OR ".join(ors) + ")"]
        order, use_match = "occurred_at DESC", False
    elif mode == "code":
        where = ["content LIKE :codepat"]
        params["codepat"] = "%" + _clean_query_text(query) + "%"
        order, use_match = "occurred_at DESC", False
    else:
        where = ["event_search MATCH :q"]
        params["q"] = _to_match_query(query)
        order, use_match = _RANK_EXPR, True

    if thread_id is not None:
        where.append("thread_id = :tid")
        params["tid"] = thread_id
    if tool_name:
        where.append("tool_name = :tool")
        params["tool"] = tool_name
    if content_types:
        where.append(_in_clause("content_type", content_types, "ct", params, negate=False))
    if exclude_content_types:
        where.append(_in_clause("content_type", exclude_content_types, "xct", params, negate=True))
    if source:
        # event_search carries thread_id but not source; constrain to threads of
        # the named provider(s) via an indexed subquery (idx_threads_source). An
        # empty match yields no rows rather than invalid SQL.
        where.append(
            "thread_id IN (SELECT id FROM threads WHERE "
            + _in_clause("source", source, "src", params, negate=False) + ")"
        )
    if since:
        where.append("occurred_at >= :since")
        params["since"] = since
    if until:
        where.append("occurred_at <= :until")
        params["until"] = until

    snippet_expr = (
        "snippet(event_search, 0, '', '', ' … ', 12)" if use_match else "substr(content, 1, 300)"
    )
    sql = sa_text(
        "SELECT event_id, thread_id, event_type, content_type, occurred_at, "
        + snippet_expr + " AS snippet, content AS full_content "
        "FROM event_search WHERE " + " AND ".join(where) +
        " ORDER BY " + order + " LIMIT :lim"
    )
    with use_session(session) as s:
        rows = s.execute(sql, params).mappings().all()

    hits: list[dict] = []
    for r in rows:
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
    return hits


_INSERT_SEARCH = sa_text(
    "INSERT INTO event_search "
    "(content, event_id, thread_id, event_type, content_type, tool_name, occurred_at) "
    "VALUES (:content, :event_id, :thread_id, :event_type, :content_type, :tool_name, :occurred_at)"
)


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
        oa = ev.occurred_at.isoformat() if ev.occurred_at else None
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
        fts_table = EventFts.__table__
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
