"""Render search hits for the CLI / MCP.

Default render is a compact text list, but it carries the **match-quality signal**:
a top-line ``quality=`` verdict (strong / partial / weak / semantic) plus a ``K/N``
per hit — how many of the N query terms actually landed — so a reader can tell a
solid keyword hit from a nearest-neighbour guess before trusting it. ``output``
switches to ``count`` (a per-thread tally over the whole match pool) or ``linkable``
(JSON of event/thread ids for batch linking)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from sqlalchemy import text as sa_text

from .._store import use_session
from . import rank as _rank
from . import subjects as _subjects
from ._types import EventHit
from .rank import (
    term_hit_count,  # noqa: F401 — the match-quality primitive lives in rank; re-exported here for the render-layer's callers
)

# output='count' wants a true tally, so the pipeline over-fetches to this cap; a
# pool that reaches it was truncated and the tally renders as a floor ("N+").
COUNT_FETCH_CAP = 1000


def _hit_text(h: EventHit) -> str:
    return h.get("full_content") or h.get("snippet") or ""


def top_hit(hits: list[EventHit]) -> EventHit:
    """The best-ranked hit — what the match-quality verdict judges. Row order is
    ranked order in every shape but the nested one, which re-sorts hits into
    per-thread event order and leaves ``_rank_pos`` behind to recover the head."""
    if "_rank_pos" in hits[0]:
        return min(hits, key=lambda h: h.get("_rank_pos", 0))
    return hits[0]


def _search_quality(top_hit_count: int, n_terms: int, did_rerank: bool):
    """Verdict for the top hit → ``(quality, note)`` or None. Rerank wins (the order
    is by-meaning, not keyword overlap); else zero overlap is ``weak``, at least
    :func:`_rank.strong_match_floor` terms is ``strong``, in-between is ``partial``."""
    if n_terms <= 0:
        return None
    if did_rerank:
        return ("semantic", "ranked by meaning, not keyword overlap — confirm the top hit "
                            "actually answers the query before trusting it")
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


def _format_browse(hits: list[EventHit]) -> str:
    """Render browse rows (empty-query search) as a thread list: one line per
    thread — id, title, source, type, size, last activity — plus the follow-up
    verbs an agent needs to go deeper."""
    lines = [
        f"{len(hits)} thread(s) · browse (no query) — one row per thread, by last activity",
        "  open one: thread_read(thread_id) · its tail: thread_read(thread_id, around_event=event_id)",
        "  topic tree: thread_read('topics') · list topics: thread_search('', types='topic')",
        "",
    ]
    for h in hits:
        ts = h.get("occurred_at")
        when = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(ts or "")[:16]
        lines.append(
            f"[{h['thread_id']}/{h['event_id']}] {h.get('thread_title')} · "
            f"{h.get('thread_source') or '?'} · {h.get('content_type')} · "
            f"{h.get('n_events', 0)} ev · {when}"
        )
    return "\n".join(lines)


def _thread_row(h: EventHit) -> str:
    """One thread-list line: ``[thread/event] title · source · N ev · when``. The
    event is the thread's *match* anchor, so the row opens where the query landed."""
    ts = h.get("occurred_at")
    when = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(ts or "")[:16]
    hits_in = (h.get("_thread_more") or 0) + 1
    tally = f" · {hits_in} hits" if hits_in > 1 else ""
    return (
        f"[{h['thread_id']}/{h['event_id']}] {h.get('thread_title') or '(untitled)'} · "
        f"{h.get('thread_source') or '?'} · {h.get('n_events', 0)} ev{tally} · {when}"
    )


def _format_thread_list(hits: list[EventHit], lines: list[str]) -> str:
    """group='browse': the matched **threads**, one row each, no messages.
    ``lines`` is the shared prelude (header, quality note, subjects)."""
    lines = lines + [
        "  grouped: one row per matched thread, no messages — group='nested' keeps them, "
        "group='none' for every hit flat",
        "  open one: thread_read(thread_id) · at the match: "
        "thread_read(thread_id, around_event=event_id)",
        "",
    ]
    lines.extend(_thread_row(h) for h in hits)
    return "\n".join(lines)


def _format_nested(hits: list[EventHit], lines: list[str], terms: list[str]) -> str:
    """group='nested': every match, clustered under the thread it came from.
    Hits arrive already clustered and in event order (rank.cluster_by_thread), so
    a thread change is simply the next cluster."""
    n_terms = len(terms)
    sizes = Counter(h["thread_id"] for h in hits)
    lines = lines + [
        "  grouped: hits clustered under their thread, in event order — "
        "group='browse' for threads only, group='none' for every hit flat",
        "  open a hit: thread_read(thread_id, around_event=event_id)",
    ]
    current = None
    for h in hits:
        tid = h["thread_id"]
        if tid != current:
            current = tid
            shown = sizes[tid]
            more = h.get("_thread_more") or 0
            tally = f"{shown}+{more} hits" if more else f"{shown} hit{'s' if shown > 1 else ''}"
            lines.append("")
            lines.append(
                f"[{tid}] {h.get('thread_title') or '(untitled)'} · "
                f"{h.get('thread_source') or '?'} · {tally}"
                + (" (thread_id=… for the rest)" if more else "")
            )
        ct = h.get("content_type") or h["event_type"]
        head = f"  [{h['event_id']}] {ct}"
        if n_terms:
            k = term_hit_count(_hit_text(h), terms)
            head += f" · {k}/{n_terms}" + (" (semantic)" if k == 0 else "")
        lines.append(head)

        context = h.get("context")
        if context:
            lines.extend(f"      {ln}" for ln in context.split("\n"))
        else:
            snippet = " ".join((h.get("snippet") or "").split())
            if snippet:
                lines.append(f"      {snippet}")
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
        if not (query or "").strip():
            return ("No threads matched the browse filters. Widen the window or drop a "
                    "filter (browse lists threads by last activity; topics/system threads "
                    "need an explicit types=…).")
        return f'No results for "{query}".'

    terms = query_terms(query)
    n_terms = len(terms)
    top = top_hit(hits)
    did_rerank = bool(top.get("_did_rerank"))
    verdict = _search_quality(term_hit_count(_hit_text(top), terms), n_terms, did_rerank) if n_terms else None

    # The thread-granular list shapes (search.group='browse'/'nested') count in
    # threads; the ranked shapes count in rows.
    group = hits[0].get("_group")
    n_threads = len({h["thread_id"] for h in hits})
    if group == "browse":
        header = f'{n_threads} thread(s) for "{query}"'
    elif group == "nested":
        header = f'{len(hits)} result(s) in {n_threads} thread(s) for "{query}"'
    else:
        header = f'{len(hits)} result(s) for "{query}"'
    if verdict:
        header += f" · quality={verdict[0]}"

    # Shared prelude: what the caller must read before trusting any shape.
    prelude = [header]
    if verdict and verdict[1]:
        prelude.append(f"  note: {verdict[1]}")
    subj_line = None
    if _subjects.enabled():
        subj_line = _subjects.format_subjects_line(_subjects.subjects_for_results(hits))
        if subj_line:  # the topic graph as orientation: what subjects these hits cluster under
            prelude.append(subj_line)

    if group == "browse":
        return _format_thread_list(hits, prelude)
    if group == "nested":
        return _format_nested(hits, prelude, terms)

    lines = [prelude[0]]
    if verdict and verdict[1]:
        lines.append(f"  note: {verdict[1]}")
    if any(h.get("_thread_more") or h.get("_dup_thread_ids") for h in hits):
        lines.append("  grouped: one row per thread — repeats fold into '+N more in thread' / "
                     "'= same content'; group='browse' for a thread list, group='nested' to keep "
                     "every hit under its thread, group='none' for every hit flat")
    if subj_line:
        lines.append(subj_line)
    lines.append("  open a hit: thread_read(thread_id, around_event=event_id)")
    if subj_line:
        lines.append(
            "  open a subject: thread_read(topic_id) · drill in: thread_search(query, topic_id=…)"
        )
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
        more = h.get("_thread_more")
        if more:
            head += f" · +{more} more in thread"
        lines.append(head)

        context = h.get("context")
        if context:  # context_lines: a numbered multi-line block replaces the snippet
            lines.extend(f"    {ln}" for ln in context.split("\n"))
        else:
            snippet = " ".join((h.get("snippet") or "").split())
            if snippet:
                lines.append(f"    {snippet}")

        dups = h.get("_dup_thread_ids")
        if dups:
            shown = ", ".join(str(t) for t in dups[:3])
            extra = f", +{len(dups) - 3} more" if len(dups) > 3 else ""
            lines.append(f"    = same content in thread(s) {shown}{extra}")

        ctx_events = h.get("context_events") or {}
        for direction in ("before", "after"):
            for ev in ctx_events.get(direction, []):
                body = " ".join((ev.get("content") or "")[:200].split())
                lines.append(f"      ({direction} · {ev.get('content_type')}) {body}")

    return "\n".join(lines)
