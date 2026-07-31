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
from datetime import datetime

from sqlalchemy import text as sa_text

from .._store import use_session
from . import rank as _rank
from ._types import EventHit
from .rank import (
    term_hit_count,  # noqa: F401 — the match-quality primitive lives in rank; re-exported here for the render-layer's callers
)

# output='count' wants a true tally, so the pipeline over-fetches to this cap; a
# pool that reaches it was truncated and the tally renders as a floor ("N+").
COUNT_FETCH_CAP = 1000

# Per-line cap on rendered snippets and context windows. The ±N-line context
# window is bounded in *lines*, not characters — a chat message with no newline
# is one "line", so an uncapped render hands an agent the whole message per hit,
# and ten hits over long prose is >100 KB of context. The full text is one
# thread_read away; the render is a preview, and says so with an ellipsis.
SNIPPET_LINE_CHARS = 400


def _clip(line: str, cap: int = SNIPPET_LINE_CHARS) -> str:
    return line if len(line) <= cap else line[: cap - 2].rstrip() + " …"


def subjects_line(hits: list[EventHit]) -> str | None:
    """The ``subjects:`` orientation header over a result set, or None — the
    :mod:`.subjects` lens over the topic-graph data plane; an archive with no
    topic graph renders searches with no subjects line."""
    from . import subjects as _subjects

    if not _subjects.enabled():
        return None
    return _subjects.format_subjects_line(_subjects.subjects_for_results(hits))


def _hit_text(h: EventHit) -> str:
    return h.get("full_content") or h.get("snippet") or ""


def _scale(hits, unit: str) -> str:
    """The ``· N of M · page P/Q`` suffix that turns a result count into a
    position in a set — or ``''`` when the shape can't say.

    A page with nothing naming the whole is the defect this exists to fix: ten
    rows read identically whether they are all of them or ten of nine hundred,
    and an agent trying to enumerate has no way to tell that it stopped early.
    ``+`` marks a floor (the set scan capped), and a set the pipeline cut rather
    than resolved says ``≥`` — the total is real, the walk is what stops short."""
    total = getattr(hits, "total_threads" if unit == "thread" else "total", None)
    if total is None:
        return ""
    page, pages = getattr(hits, "page", 1), getattr(hits, "pages", None)
    exhaustive = getattr(hits, "exhaustive", False)
    mark = "+" if getattr(hits, "capped", False) else ""
    # "≥" over "of": a cut pool knows how far it reached, not how much there was,
    # and rendering that reach as a total is the same silent-truncation lie in a
    # new place.
    out = f" · {len(hits)} of {total}{mark}" if exhaustive else f" · {len(hits)} of ≥{total}"
    if pages and pages > 1:
        out += f" · page {page}/{pages}{mark}"
    if not exhaustive:
        out += " · truncated (widen --limit or page for more)"
    return out


def top_hit(hits: list[EventHit]) -> EventHit:
    """The best-ranked hit — what the match-quality verdict judges. Rows come back
    in ranked order, so it is the first."""
    return hits[0]


def _search_quality(top_hit_count: int, n_terms: int):
    """Verdict for the top hit → ``(quality, note)`` or None. Zero overlap is
    ``weak``, at least :func:`_rank.strong_match_floor` terms is ``strong``,
    in-between is ``partial``."""
    if n_terms <= 0:
        return None
    if top_hit_count == 0:
        return ("weak", "no query term appears in the top hit — these are nearest-neighbour "
                        "guesses and the log may simply not contain this. Rephrase the concept "
                        "or switch data store; piling on more synonyms won't help")
    if top_hit_count >= _rank.strong_match_floor(n_terms):
        return ("strong", None)
    return ("partial", "only some query terms matched the top hit — scan before trusting")


def query_terms(query: str) -> list[str]:
    """Ranking terms minus the pipe-OR token (which isn't a content term)."""
    return [t for t in _rank.search_terms(query) if t and t != "|"]


def _looks_like_a_file(query: str) -> bool:
    """A query shaped like a filename — anything with a path separator, or a bare
    name carrying an extension."""
    return "/" in query or bool(re.fullmatch(r"[\w.-]+\.[A-Za-z]\w{0,4}", query))


def _next_moves(query: str) -> list[str]:
    """Rendered lines offering concrete retries for a search that found nothing —
    or found only nearest-neighbour guesses.

    The tools ship a compact description and keep their manual behind
    ``thread_help`` (see :mod:`thread_archive._tools`), which means the alternatives
    an agent might need are not sitting in its context by default. They arrive here
    instead: at the one moment they are demonstrably relevant, charged only to the
    caller that hit the wall. Each line is a move that would plausibly change this
    result, not a summary of the tool.
    """
    q = (query or "").strip()
    moves = []
    if q and " " not in q:
        moves.append("match='substring' — matches inside longer words (p4 finds mp4); "
                     "the default matches whole words only")
    if _looks_like_a_file(q):
        moves.append(f"path='{q}' — the sessions that *touched* that file; search "
                     f"finds where something was discussed, path where it was done")
    moves.append("a shorter query, or query='' with since='7d' to browse what is there")
    return [f"  try: {m}" for m in moves] + [
        "  thread_help('search') — every filter and its grammar"
    ]


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


def _ops_tally(ops: dict) -> str:
    """``edit 3 · read 12`` in strength order — the verb that matters reads first."""
    from .code import ALL_OPS

    return " · ".join(f"{op} {ops[op]}" for op in ALL_OPS if ops.get(op))


def _format_browse(hits: list[EventHit]) -> str:
    """Render browse rows (empty-query search) as a thread list: one line per
    thread — id, title, source, type, size, last activity — plus the follow-up
    verbs an agent needs to go deeper.

    A ``path``-scoped browse is the same list answering a different question, so it
    says so and each row carries what the thread *did* to the file rather than how
    big it was: the op tally, the window of touches, and an ``event_id`` that opens
    at the work instead of at the thread's tail."""
    code_axis = bool(hits and hits[0].get("_path_ops") is not None)
    scale = _scale(hits, "thread")
    if code_axis:
        lines = [
            f"{len(hits)} thread(s) · touched this path — changes first, then most recent{scale}",
            "  open one at the work: thread_read(thread_id, around_event=event_id, mode='chat')",
            "  what else it changed: thread_read(thread_id, summary='files')",
            "",
        ]
    elif hits and hits[0].get("_browse_order") == "given":
        lines = [
            f"{len(hits)} thread(s) · in the order the scope ranked them "
            f"(see the note above), not by last activity{scale}",
            "  open one: thread_read(thread_id) · its tail: "
            "thread_read(thread_id, around_event=event_id)",
            "",
        ]
    else:
        lines = [
            f"{len(hits)} thread(s) · browse (no query) — one row per thread, "
            f"by last activity{scale}",
            "  open one: thread_read(thread_id) · its tail: "
            "thread_read(thread_id, around_event=event_id)",
            "",
        ]
    for h in hits:
        ts = h.get("occurred_at")
        when = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(ts or "")[:16]
        head = (f"[{h['thread_id']}/{h['event_id']}] {h.get('thread_title')} · "
                f"{h.get('thread_source') or '?'}")
        if not code_axis:
            lines.append(f"{head} · {h.get('content_type')} · {h.get('n_events', 0)} ev · {when}")
            continue
        files = h.get("_path_files") or 0
        lines.append(
            f"{head}\n     {_ops_tally(h['_path_ops'])}"
            + (f" · {files} file(s)" if files > 1 else "")
            + f" · {str(h.get('_path_first') or '')[:16]} → {str(h.get('_path_last') or '')[:16]}"
        )
    return "\n".join(lines)

def format_results(hits: list[EventHit], query: str, *, output: str | None = None) -> str:
    if hits and hits[0].get("_browse"):
        # Browse rows: linkable stays JSON; count is meaningless for a list that
        # is already one row per thread, so every other output renders the list.
        if output == "linkable":
            return _format_linkable(hits)
        return _format_browse(hits)
    if output == "count":
        return _format_count(hits, query)
    if output == "linkable":
        return _format_linkable(hits)
    if not hits:
        # An empty page past the end is not an empty result set, and the two must
        # never render the same: an enumerator that walked off the end would read
        # its own success as "this query matches nothing".
        page, pages = getattr(hits, "page", 1), getattr(hits, "pages", None)
        if page > 1:
            end = f" (the last is {pages})" if pages else ""
            return (f'Page {page} is past the end of the results for "{query}"{end}. '
                    f"Every match has been listed.")
        if not (query or "").strip():
            return ("No threads matched the browse filters. Widen the window or drop a "
                    "filter (browse lists threads by last activity; topics/system threads "
                    "need an explicit types=…).")
        return "\n".join([f'No results for "{query}".', *_next_moves(query)])

    terms = query_terms(query)
    n_terms = len(terms)
    top = top_hit(hits)
    verdict = _search_quality(term_hit_count(_hit_text(top), terms), n_terms) if n_terms else None

    # Every row is one matching message, so the count is in messages — and the
    # conversation count rides along, because "18 messages across 4 threads" and
    # "18 messages across 18 threads" are different answers to the same query.
    n_threads = len({h["thread_id"] for h in hits})
    header = (f'{len(hits)} result(s) in {n_threads} thread(s) for "{query}"'
              + _scale(hits, "row"))
    if verdict:
        header += f" · quality={verdict[0]}"

    subj_line = subjects_line(hits)
    lines = [header]
    if verdict and verdict[1]:
        lines.append(f"  note: {verdict[1]}")
    # Guesses came back where an answer was asked for: the same moment a no-result
    # gets its alternatives, and the same reason.
    if verdict and verdict[0] == "weak":
        lines.extend(_next_moves(query))
    if subj_line:  # the topic graph as orientation: what subjects these hits cluster under
        lines.append(subj_line)
    lines.append("  open a hit: thread_read(thread_id, around_event=event_id)")
    if subj_line:
        lines.append("  open a subject: thread_read(topic_id)")
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
        # Date + provider close each head: "the most recent mention" is answered
        # by reading dates off the hits, so the hits must carry them.
        ts = h.get("occurred_at")
        when = ts.strftime("%Y-%m-%d") if isinstance(ts, datetime) else str(ts or "")[:10]
        tail = [p for p in (h.get("thread_source"), when) if p]
        if tail:
            head += " · " + " · ".join(tail)
        lines.append(head)

        context = h.get("context")
        if context:  # context_lines: a numbered multi-line block replaces the snippet
            lines.extend(f"    {_clip(ln)}" for ln in context.split("\n"))
        else:
            snippet = " ".join((h.get("snippet") or "").split())
            if snippet:
                lines.append(f"    {_clip(snippet)}")

        ctx_events = h.get("context_events") or {}
        for direction in ("before", "after"):
            for ev in ctx_events.get(direction, []):
                body = " ".join((ev.get("content") or "")[:200].split())
                lines.append(f"      ({direction} · {ev.get('content_type')}) {body}")

    return "\n".join(lines)
