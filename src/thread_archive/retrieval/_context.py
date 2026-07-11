"""Per-hit context: surrounding *lines* within a hit, and surrounding *events*
in the same thread.

Two independent enrichments the search pipeline attaches to its final hits:

- :func:`extract_context_lines` (the ``context_lines`` arg) renders a numbered
  window of lines around the first query-term match inside one event's content.
- :func:`get_context_events` (the ``context_events`` arg) fetches the N events
  immediately before/after each hit in its thread, nearest-first, optionally
  filtered by content type. Neighbours come from the ``event_search`` FTS table
  (which already carries the extracted content/type/tool per indexed event) and
  are ordered by ``event_id`` — the same ordering the importer assigns in
  conversation order.
"""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from ..store import use_session
from .fts import _in_clause

# Dropped before picking the line to centre context on, so a query like "how does
# auth work" centres on the line with 'auth', not the first 'how'.
_CONTEXT_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "and", "but", "or", "not", "no", "it", "this", "that", "how", "what",
    "when", "where", "why", "which", "who", "i", "we", "you", "he", "she",
    "they", "me", "my", "our", "your", "so", "if", "about", "up", "just",
}


def extract_context_lines(content: str, query: str, num_lines: int) -> str:
    """A numbered window of ``±num_lines`` around the first line of ``content``
    containing a (non-stopword) query term. The matched line is prefixed ``>>>``,
    the rest three spaces; lines are 1-based. No match → the first ``2N+1`` lines,
    unnumbered."""
    lines = content.split("\n")
    query_terms = [t for t in re.findall(r"\w+", query.lower()) if t not in _CONTEXT_STOPWORDS]
    if not query_terms:  # all stopwords → fall back to every term
        query_terms = re.findall(r"\w+", query.lower())

    match_line_idx = None
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(term in low for term in query_terms):
            match_line_idx = idx
            break

    if match_line_idx is None:
        return "\n".join(lines[: num_lines * 2 + 1])

    start = max(0, match_line_idx - num_lines)
    end = min(len(lines), match_line_idx + num_lines + 1)
    numbered = []
    for i, line in enumerate(lines[start:end]):
        prefix = ">>>" if (start + i) == match_line_idx else "   "
        numbered.append(f"{prefix} {start + i + 1}: {line}")
    return "\n".join(numbered)


def parse_context_events_spec(spec: str) -> tuple[int, int, Optional[list[str]]]:
    """Parse the ``context_events`` arg: ``N`` (symmetric) / ``before:after`` /
    ``before:after:ct1,ct2``. Raises ValueError on a non-numeric before/after."""
    parts = spec.split(":")
    if len(parts) == 1:
        n = int(parts[0])
        return n, n, None
    if len(parts) == 2:
        return int(parts[0]), int(parts[1]), None
    cts = [t.strip() for t in parts[2].split(",") if t.strip()]
    return int(parts[0]), int(parts[1]), cts or None


def _neighbors(s: Session, tid: int, eid: int, op: str, order: str,
               lim: int, content_types: Optional[list[str]]) -> list[dict]:
    params: dict = {"tid": tid, "eid": eid, "lim": lim}
    # thread-meta docs (title/summary) share the first event's id — they're not
    # conversation neighbours.
    where = ["thread_id = :tid", "event_id " + op + " :eid", "event_type != 'thread_meta'"]
    if content_types:
        where.append(_in_clause("content_type", content_types, "ct", params, negate=False))
    sql = sa_text(
        "SELECT event_id, content_type, substr(content, 1, 500) AS content, tool_name "
        "FROM event_search WHERE " + " AND ".join(where) +
        " ORDER BY event_id " + order + " LIMIT :lim"
    )
    return [dict(r) for r in s.execute(sql, params).mappings().all()]


def get_context_events(
    hits: list[dict],
    before: int,
    after: int,
    content_types: Optional[list[str]] = None,
    *,
    session: Optional[Session] = None,
) -> dict[int, dict]:
    """Map each hit's ``event_id`` → ``{"before": [...], "after": [...]}`` of the
    nearest neighbouring events in the same thread. ``before`` lists are reversed
    to chronological (oldest→newest) order. Empty when both counts are ≤ 0."""
    if not hits or (before <= 0 and after <= 0):
        return {}
    out: dict[int, dict] = {}
    with use_session(session) as s:
        for h in hits:
            tid, eid = h["thread_id"], h["event_id"]
            entry: dict = {}
            if before > 0:
                rows = _neighbors(s, tid, eid, "<", "DESC", before, content_types)
                rows.reverse()
                entry["before"] = rows
            if after > 0:
                entry["after"] = _neighbors(s, tid, eid, ">", "ASC", after, content_types)
            if entry:
                out[eid] = entry
    return out
