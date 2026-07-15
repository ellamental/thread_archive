"""Render search hits for the CLI / MCP.

Default render is a compact text list, but it carries the **match-quality signal**:
a top-line ``quality=`` verdict (strong / partial / weak / semantic) plus a ``K/N``
per hit — how many of the N query terms actually landed — so a reader can tell a
solid keyword hit from a nearest-neighbour guess before trusting it. ``output``
switches to ``count`` (a per-thread tally over the whole match pool) or ``linkable``
(JSON of event/thread ids for batch linking)."""

from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy import text as sa_text

from .._store import use_session
from . import rank as _rank
from ._types import EventHit

# output='count' wants a true tally, so the pipeline over-fetches to this cap; a
# pool that reaches it was truncated and the tally renders as a floor ("N+").
COUNT_FETCH_CAP = 1000


def _hit_text(h: EventHit) -> str:
    return h.get("full_content") or h.get("snippet") or ""


def term_hit_count(content: str, terms: list[str]) -> int:
    """How many of ``terms`` literally appear in ``content``. Terms ≥4 chars match
    by substring; shorter terms must hit a word boundary (so 'go' doesn't match
    'good'). Each term counts at most once."""
    if not terms or not content:
        return 0
    c = content.lower()
    n = 0
    for t in terms:
        if len(t) >= 4:
            if t in c:
                n += 1
        elif re.search(r"\b" + re.escape(t) + r"\b", c):
            n += 1
    return n


def _search_quality(top_hit_count: int, n_terms: int, did_rerank: bool):
    """Verdict for the top hit → ``(quality, note)`` or None. Rerank wins (the order
    is by-meaning, not keyword overlap); else zero overlap is ``weak``, ≥⌈2/3·N⌉
    terms is ``strong``, in-between is ``partial``."""
    if n_terms <= 0:
        return None
    if did_rerank:
        return ("semantic", "ranked by meaning, not keyword overlap — confirm the top hit "
                            "actually answers the query before trusting it")
    if top_hit_count == 0:
        return ("weak", "no query term appears in the top hit — these are nearest-neighbour "
                        "guesses and the log may simply not contain this. Rephrase the concept "
                        "or switch data store; piling on more synonyms won't help")
    strong_at = max(1, -(-2 * n_terms // 3))  # ceil(2/3 · n_terms)
    if top_hit_count >= strong_at:
        return ("strong", None)
    return ("partial", "only some query terms matched the top hit — scan before trusting")


def query_terms(query: str) -> list[str]:
    """Ranking terms minus the pipe-OR token (which isn't a content term)."""
    return [t for t in _rank.search_terms(query) if t and t != "|"]


def _format_count(hits: list[EventHit], query: str) -> str:
    thread_counts = Counter(h["thread_id"] for h in hits)
    with use_session() as s:
        corpus = s.execute(
            sa_text("SELECT count(*), count(DISTINCT thread_id) FROM event_search")
        ).one()
    capped = len(hits) >= COUNT_FETCH_CAP
    total = f"{len(hits)}+ (tally capped)" if capped else str(len(hits))
    lines = [
        f"Total: {total} results across {len(thread_counts)} threads",
        f"  (corpus: {corpus[0]:,} indexed events, {corpus[1]:,} threads)",
    ]
    for tid, count in thread_counts.most_common():
        title = next((h.get("thread_title") for h in hits if h["thread_id"] == tid), None)
        lines.append(f"  [{tid}] {title or '(untitled)'}: {count}")
    return "\n".join(lines)


def _format_linkable(hits: list[EventHit]) -> str:
    out = []
    for h in hits:
        entry = {
            "event_id": h["event_id"],
            "thread_id": h["thread_id"],
            "preview": (h.get("snippet") or "")[:80],
        }
        if "context_events" in h:
            entry["context_events"] = h["context_events"]
        out.append(entry)
    return json.dumps(out, indent=2)


def format_results(hits: list[EventHit], query: str, *, output: str | None = None) -> str:
    if output == "count":
        return _format_count(hits, query)
    if output == "linkable":
        return _format_linkable(hits)
    if not hits:
        return f'No results for "{query}".'

    terms = query_terms(query)
    n_terms = len(terms)
    did_rerank = bool(hits[0].get("_did_rerank"))
    verdict = _search_quality(term_hit_count(_hit_text(hits[0]), terms), n_terms, did_rerank) if n_terms else None

    header = f'{len(hits)} result(s) for "{query}"'
    if verdict:
        header += f" · quality={verdict[0]}"
    lines = [header]
    if verdict and verdict[1]:
        lines.append(f"  note: {verdict[1]}")
    lines.append("  open a hit: thread_read(thread_id, around_event=event_id)")
    lines.append("")

    for h in hits:
        title = h.get("thread_title") or f"thread {h['thread_id']}"
        ct = h.get("content_type") or h["event_type"]
        head = f"[{h['thread_id']}/{h['event_id']}] {title} · {ct}"
        if n_terms:
            k = term_hit_count(_hit_text(h), terms)
            head += f" · {k}/{n_terms}"
            if k == 0:
                head += " (semantic)"
        lines.append(head)

        context = h.get("context")
        if context:  # context_lines: a numbered multi-line block replaces the snippet
            lines.extend(f"    {ln}" for ln in context.split("\n"))
        else:
            snippet = " ".join((h.get("snippet") or "").split())
            if snippet:
                lines.append(f"    {snippet}")

        ctx_events = h.get("context_events") or {}
        for direction in ("before", "after"):
            for ev in ctx_events.get(direction, []):
                body = " ".join((ev.get("content") or "")[:200].split())
                lines.append(f"      ({direction} · {ev.get('content_type')}) {body}")

    return "\n".join(lines)
