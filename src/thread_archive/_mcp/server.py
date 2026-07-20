"""Library-native MCP server.

Exposes ``thread_search`` + ``thread_read`` as MCP tools that call the
:mod:`thread_archive._api` library functions directly — no web framework, no HTTP,
no route layer. The server is library-native: it dispatches straight to the API
functions in-process.

The *tools* are read-only, but the server process also cohosts lazy catch-up
ingest (see :func:`_maybe_catch_up` and :mod:`.._watcher.lazy`): a throttled
background pass at startup and around tool calls keeps the archive current
with no daemon installed, and degrades to a no-op flock probe when the
always-on watcher owns ingest. ``THREAD_ARCHIVE_MCP_INGEST=0`` disables it.

The archive home comes from ``$THREAD_ARCHIVE_HOME`` (set by the MCP client
config), else ``~/.thread/archive``. Run per-client over stdio (the default)::

    python -m thread_archive._mcp.server

or as one shared always-on server over streamable-HTTP, so many agents share a
single resident model instead of one 3 GB process each::

    archive-mcp --http --host 127.0.0.1 --port 8788

``import mcp`` below is the external MCP SDK (top-level absolute import); this
package is ``thread_archive._mcp`` and never shadows it.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .. import _api as api
from .._retrieval import format_results, warm_models
from .._retrieval import usage as _usage
from .._retrieval._types import EventHit
from .._retrieval.format import query_terms, term_hit_count, top_hit

logger = logging.getLogger(__name__)

mcp = FastMCP("thread-archive")

# ── lazy catch-up ingest ──────────────────────────────────────────────────────
# The zero-daemon freshness path: the server runs a background catch-up pass at
# startup and (throttled) around tool calls, so a bare `claude mcp add …
# archive-mcp` searches a current archive without any LaunchAgent installed.
# Cross-process safety lives in the pass itself (see _watcher.lazy): the
# ingest-owner flock makes every pass a no-op probe while the always-on watcher
# daemon — or another server's pass — owns ingest. THREAD_ARCHIVE_MCP_INGEST=0
# turns the whole behaviour off.
_INGEST_MIN_INTERVAL = 300.0  # seconds between catch-up attempts in this process


class IngestThrottle:
    """Per-process lazy-ingest throttle state — public so tests reset it
    (``monkeypatch.setattr(server.INGEST, "last", 0.0)``) instead of poking
    module globals."""

    def __init__(self) -> None:
        self.last = 0.0  # monotonic time of the last attempt (0 = never)
        self.running = threading.Lock()  # one in-flight catch-up per process


INGEST = IngestThrottle()


def _resolve_ref(ref: int | str) -> Optional[str]:
    """Resolve a thread/topic ref — a ULID thread id, a legacy integer alias, or
    a provider session id — to the archive's ULID thread id; None when nothing
    matches. See :func:`thread_archive._retrieval.read.resolve_thread_ref`."""
    from .._retrieval.read import resolve_thread_ref
    from .._store import get_session

    api.open_archive()
    with get_session() as s:
        return resolve_thread_ref(s, ref)


def _maybe_catch_up() -> None:
    """Kick a background catch-up pass, throttled. Never blocks the caller and
    never raises — retrieval must work identically with ingest disabled, owned
    by another process, or broken."""
    if os.environ.get("THREAD_ARCHIVE_MCP_INGEST", "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return
    now = time.monotonic()
    if INGEST.last and now - INGEST.last < _INGEST_MIN_INTERVAL:
        return
    if not INGEST.running.acquire(blocking=False):
        return  # a catch-up is already in flight in this process
    INGEST.last = now

    def _run() -> None:
        try:
            from .._watcher import catch_up_once

            catch_up_once()
        except Exception:  # noqa: BLE001 — advisory; retrieval must not care
            logger.exception("lazy catch-up ingest failed")
        finally:
            INGEST.running.release()

    threading.Thread(target=_run, name="archive-lazy-ingest", daemon=True).start()

# The agent-facing default search scope: USER messages plus the thread-meta docs
# (title + stored summary) — the intentional signals of what a thread was about.
# Assistant text, tool calls/results, and thinking are opt-in (pass an explicit
# content_type), and content_type='all' clears the filter to search everything.
# Mirrors the archive backend's thread_search default.
DEFAULT_SEARCH_CONTENT_TYPES = ("user", "title", "summary")

# Where the default scope widens to when it comes up dry: assistant text is where
# conclusions/decisions live (see thread_read's mode docs), so a query the asking
# side can't answer gets one automatic retry against it. One retry, only on the
# default scope, only when no query term landed — an explicit content_type is a
# deliberate choice and is never second-guessed.
WIDENED_SEARCH_CONTENT_TYPES = DEFAULT_SEARCH_CONTENT_TYPES + ("text",)


def _default_scope_is_weak(hits: "list[EventHit]", query: str) -> bool:
    """True when a default-scope result set warrants the one-shot widen to
    assistant text: no hits at all, or no query term appears in the top hit
    (nearest-neighbour guesses)."""
    terms = query_terms(query)
    if not terms:
        return False
    if not hits:
        return True
    # top_hit, not hits[0] — the nested shape orders rows by thread, not by rank.
    top = top_hit(hits)
    return term_hit_count(top.get("full_content") or top.get("snippet") or "", terms) == 0


# ── degradation notice ────────────────────────────────────────────────────────
# Coverage's per-source degradation verdicts (health.json → coverage_last.degraded)
# surfaced where the user actually is: prepended to search results, naming the
# remedy. Import drift is otherwise operator-shaped state (ledgers, `archive
# coverage`) that a user has no reason to look at — the moment they care about
# their archive is the moment they search it. Unconditional on the result set:
# a degraded source's freshest content is exactly what search CAN'T return, so
# gating the notice on its hits would hide it precisely when it matters most.
# Verdicts older than the cutoff are ignored — a dead nightly must not nag
# forever on a frozen verdict (its own staleness alarm lives elsewhere).
_NOTICE_MAX_AGE_DAYS = 14.0
_DEGRADED_PHRASES = {
    "went_dark": "its store is missing or empty",
    "stale_ingest": "store activity is not becoming events",
    "validation_drift": "the parser no longer fully models its format",
    "capture_skips": "content is being consumed without importing",
}


def _degradation_notices() -> str:
    """One ``note:`` line per currently-degraded source, newline-terminated;
    empty string when all sources are healthy. Fail-soft: retrieval must work
    identically when health.json is absent, stale, or unreadable."""
    try:
        from datetime import datetime, timezone

        from .._ops.health import read_health

        rec = read_health().get("coverage_last") or {}
        degraded = rec.get("degraded") or {}
        if not degraded:
            return ""
        at = datetime.fromisoformat(str(rec.get("at")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - at
        if age.total_seconds() > _NOTICE_MAX_AGE_DAYS * 86400:
            return ""
        lines = []
        for source in sorted(degraded):
            verdict = degraded[source] or {}
            phrase = _DEGRADED_PHRASES.get(str(verdict.get("reason") or ""), "import degraded")
            since = str(verdict.get("since") or "")[:10]
            lines.append(
                f"note: {source} import is degraded ({phrase}"
                + (f" since {since}" if since else "")
                + f") — recent {source} content may be missing from results. "
                f"remedy: archive fix-import {source}"
            )
        return "\n".join(lines) + "\n"
    except Exception:  # noqa: BLE001 — advisory; retrieval must not care
        return ""


@mcp.tool()
def thread_search(
    query: str,
    limit: int = 10,
    thread_id: Optional[int | str] = None,
    topic_id: Optional[int | str] = None,
    content_type: Optional[str] = None,
    exclude_content_type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[str] = None,
    types: Optional[str] = None,
    agents: Optional[str] = None,
    startswith: Optional[str] = None,
    sort: Optional[str] = None,
    group: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
) -> str:
    """Search the local conversation archive (federated: lexical FTS5 + optional
    semantic vectors → fusion → rank → optional cross-encoder re-rank).

    Read the match signal before trusting a result: the header carries
    ``quality=strong|partial|weak|semantic`` for the top hit, and each hit shows
    ``K/N`` (how many query terms landed) — a weak / 0-of-N result means these are
    nearest-neighbour guesses and the log likely lacks it, so rephrase or switch
    store rather than piling on synonyms.

    An **empty query is a browse** — no keywords needed: one row per thread,
    newest activity first, honoring the structural filters. "What happened
    yesterday" is ``query='', since='1d'``; "recent cursor sessions" is
    ``query='', source='cursor'``; "list my topics" is ``query='',
    types='topic'``; ``sort='oldest'`` flips to the earliest threads. Each row
    carries the thread id (open it: ``thread_read``) and its newest event id
    (open at the tail: ``around_event``). A browse hides topic and system
    threads unless ``types``/``agents`` says otherwise; ranking options
    (content_type, context, rerank) don't apply. The curated topic *hierarchy*
    is a read, not a search: ``thread_read('topics')``.

    By default USER messages, thread titles, and stored thread summaries are
    searched — the strongest signals of what a thread was about. When that scope
    comes up dry (no query term in the top hit), the search retries once with
    assistant text included and says so in the output. Assistant text,
    tool calls/results, and thinking are otherwise opt-in: pass
    ``content_type='all'`` to search everything, or a specific ``content_type``
    (text/thinking/tool/tool_result/...) to target one.

    The header's ``subjects:`` line names the curated topics the results cluster
    under, each with its ``[topic <id>]`` — pass the id to ``thread_read`` for the
    topic's curated page (description, links, cited quotes), or to ``topic_id``
    here to scope a follow-up search to that subject's conversations.

    Query grammar: natural language, "quoted phrases", boolean AND/OR/NOT,
    pipe-OR (a|b), and code identifiers (get_session, a.b.c). Filter by
    ``thread_id``, ``topic_id`` (scope to a curated topic's member conversations —
    the threads cited under it or linked to it in the knowledge graph) — both
    accept a ULID thread id, a legacy integer alias, or a provider session id,
    the same ref shapes ``thread_read`` takes —
    ``content_type`` (default user+title+summary; 'all' searches
    everything),
    ``exclude_content_type`` (comma-separated types to drop), ``tool_name``,
    ``source`` (comma-separated providers, e.g. 'claude-code,cursor'),
    ``types`` (comma-separated ``thread_type`` values — 'conversation',
    'topic', 'system'), and a ``since``/``until`` window (ISO timestamp or '7d').

    Agent-run threads — subagent / machinery sessions (🤖-titled) — are
    **excluded by default**: a swarm echoes its spawning prompt verbatim, and
    those copies would drown the conversation that asked. Pass
    ``agents='include'`` to search them alongside conversations, or
    ``agents='only'`` for just them ("what did my subagents do"). An explicit
    ``thread_id``/``topic_id`` scope always reaches them.

    ``group`` chooses how results relate to threads. Ranked results default to
    **one row per thread** — the thread's best hit, with its other hits folded
    into a ``+N more in thread`` note (drill in with a ``thread_id``-scoped
    search) and duplicate content from other threads (forked sessions,
    fleet-spawned copies of one prompt) folded into a ``= same content in
    thread(s) …`` note. Pass ``group='none'`` for every hit as its own row.

    Two modes turn any search into a thread-granular **list** — the shape an
    empty-query browse returns, over your query's matches:
    ``group='browse'`` lists the matched *threads* only (one row each: title,
    provider, size, when — no messages), and ``group='nested'`` keeps the
    messages, clustered under their thread in event order. Both count ``limit``
    in threads and list every matched thread; nested shows up to 5 hits per
    thread, the rest folded into its header. Reach for browse to see *which
    conversations* touched something, nested to read *what they said* about it
    with the thread structure intact.

    ``startswith`` does a structural prefix scan (content LIKE 'prefix%'; query text
    unused). ``sort='oldest'`` returns matches chronologically (find when something
    was first discussed) instead of the default recency-biased ranking.
    ``context_lines`` (default 2; set 0 for the raw FTS snippet) replaces each
    snippet with a numbered ±N-line window around the match; ``context_events``
    ('N' / 'before:after' /
    'before:after:types', e.g. '2' or '0:1:user') appends the neighbouring events.
    ``output='count'`` returns a per-thread tally (no snippets); ``output='linkable'``
    returns JSON of event/thread ids. ``rerank`` forces the cross-encoder head
    re-rank on/off (else auto-gated: conceptual queries whose top hit isn't
    already a strong literal match, when the ``[embeddings]`` extra is installed).
    """
    _maybe_catch_up()
    # Bound caller-supplied sizing before it reaches the engine: limit drives a
    # candidate pool of max(limit*5, 200) rows, so an unclamped value forces a
    # multi-million-row FTS scan. The web layer clamps to the same [1, 500] for
    # exactly this reason; context_lines is a per-hit window, bounded likewise.
    limit = max(1, min(int(limit), 500))
    context_lines = max(0, min(int(context_lines), 50))
    # Resolve id-shaped filters up front (ULID / legacy integer alias / provider
    # session id) so the engine only ever sees canonical ULID thread ids, and a
    # ref that matches nothing says so instead of silently returning zero hits.
    if thread_id is not None:
        resolved = _resolve_ref(thread_id)
        if resolved is None:
            return (f"thread {thread_id} not found — thread_id takes a ULID thread id, "
                    f"a legacy integer id, or a provider session id")
        thread_id = resolved
    if topic_id is not None:
        resolved = _resolve_ref(topic_id)
        if resolved is None:
            return (f"topic {topic_id} not found — topic_id takes a topic's ULID id "
                    f"or its legacy integer id")
        topic_id = resolved
    # Default scope is user messages only; an explicit type targets it, and
    # content_type='all' clears the filter to search everything (see the constant).
    if content_type == "all":
        content_types = None
    elif content_type:
        content_types = [content_type]
    else:
        content_types = list(DEFAULT_SEARCH_CONTENT_TYPES)
    exclude = [c.strip() for c in exclude_content_type.split(",") if c.strip()] if exclude_content_type else None
    sources = [s.strip() for s in source.split(",") if s.strip()] if source else None
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None

    def _run(cts):
        return api.search(
            query,
            limit=limit,
            thread_id=thread_id,
            topic_id=topic_id,
            content_types=cts,
            exclude_content_types=exclude,
            since=since,
            until=until,
            tool_name=tool_name,
            source=sources,
            types=type_list,
            agents=agents,
            startswith=startswith,
            sort=sort,
            group=group,
            output=output,
            context_lines=context_lines,
            context_events=context_events,
            rerank=rerank,
        )

    # Latency covers the retrieval work as the caller felt it — both arms plus
    # any widen retry — but not the catch-up ingest above or rendering below.
    started = time.monotonic()
    hits = _run(content_types)

    # One-shot scope widen: a default-scope search whose top hit contains no query
    # term (or that found nothing) retries once with assistant text included —
    # conclusions live there, and internalizing the retry saves the agent a
    # round-trip the quality note would otherwise ask of it. Ranked/plain output
    # only: structural shapes (browse/startswith/oldest/count/linkable) have no
    # match signal to judge weakness by.
    widened = False
    ranked_shape = bool((query or "").strip()) and startswith is None and sort is None and output is None
    if content_type is None and ranked_shape and _default_scope_is_weak(hits, query):
        wide_hits = _run(list(WIDENED_SEARCH_CONTENT_TYPES))
        if wide_hits and not _default_scope_is_weak(wide_hits, query):
            hits, widened = wide_hits, True

    # Usage ledger (fail-soft, ids only — see _retrieval.usage): the observed
    # ground truth future retrieval evals are built from.
    _usage.record_search(
        query,
        params={
            "limit": limit, "thread_id": thread_id, "topic_id": topic_id,
            "content_type": content_type,
            "exclude_content_type": exclude_content_type, "since": since,
            "until": until, "tool_name": tool_name, "source": source,
            "types": types, "agents": agents,
            "startswith": startswith, "sort": sort, "group": group,
            "output": output, "rerank": rerank,
        },
        hits=hits,
        widened=widened,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )

    rendered = format_results(hits, query, output=output)
    if widened:
        rendered = ("note: no keyword match in the default scope (user/title/summary) — "
                    "results below include assistant text\n" + rendered)
    return _degradation_notices() + rendered


@mcp.tool()
def thread_read(
    thread_id: int | str,
    limit: int = 200,
    offset: int = 0,
    summary: bool | str = False,
    mode: Optional[str] = None,
    user_only: Optional[bool] = None,
    tool_results: bool = False,
    max_chars: int = 0,
    after_event: Optional[int] = None,
    around_event: Optional[int] = None,
    context_turns: int = 1,
) -> str:
    """Read a thread's conversation, reconstructed from the event log.

    ``thread_id`` accepts three ref shapes, distinguished by form alone: the
    archive's own **ULID** thread id (26-char Crockford base32 — what search
    results and topic pages carry); an all-digit **legacy integer id** (a
    permanent alias — integer ids pasted in old conversations keep resolving);
    or a provider **session uuid** (the id a tool like claude-code / cursor /
    codex knows the conversation by — its ``source_id``, newest match wins).
    Any of the three can be passed straight through without looking the ULID up
    first.

    A **topic id** (from a search header's ``subjects:`` line, or a topic link)
    reads as the topic's curated page instead of a transcript: description, links
    into the topic graph, and the cited quotes — each anchored ``[event:N]`` so it
    opens in a focused read via ``around_event``. The reserved ref **'topics'**
    reads the whole curated **topic tree** — the knowledge graph's table of
    contents, an indented forest of every parented topic (budgeted by
    ``max_chars``; unparented topics list via ``thread_search('', types='topic')``).

    ``mode`` picks the view: 'user' (default) = only the USER messages — the real
    signal of what a thread was about and what was wanted, far cheaper than the
    transcript (for research / 'what was this thread about' that IS what you want);
    'chat' = the readable conversation — user turns + the assistant's reasoning/text
    with tool calls stripped out (use when you need what was *decided/concluded/built*,
    which lives in assistant text); 'full' = the whole transcript including every
    tool call (bulky, mostly tool noise — only when you need what the assistant *did*);
    'last' = ONLY the thread's final assistant text — the closing answer/wrap-up, the
    cheapest way to see how a session ended (ignores pagination; the footer names the
    turn, so the surrounding exchange is one mode='chat' read away);
    'ends' = the first and last ``context_turns`` turns chat-style in one read
    (default 1 each end) — "what was this session and how did it end" without paying
    for the middle; a gap marker names the offset that continues past the head.
    Tool *output* is off by default; set ``tool_results=true`` (only meaningful with
    'full', where the calls are shown) to fold each tool's result under its call.

    Images and documents (pasted screenshots, tool-result captures, attached
    PDFs) render as ``[image image/png 48 KB — /path/to/blob]`` markers. The
    path is a real local file — Read it to actually view the image.

    The read is size-budgeted (~48k chars), so it never silently overflows the MCP
    output cap: a thread bigger than one chunk ends in a CHUNKED footer naming the
    exact offset to read next (that's pagination, not lost data — page with
    ``offset``, or resume from an event with ``after_event``). To open a search hit,
    pass its event id as ``around_event``: the read contains that event's whole turn,
    plus ``context_turns`` turns before and after (default 1), and marks the matching
    step with ``match:<event_id>`` (a hit on an event the transcript hides still opens
    the turn at its position, just without the marker). A focused read defaults to
    readable ``chat`` mode;
    choose ``full`` when the hit is thinking/tool content. ``summary`` picks a
    summary view instead of the transcript: ``true``/``'toc'`` = a compact per-message
    TOC; ``'short'`` = the thread's stored short summary (a few sentences);
    ``'indexed'`` = the stored indexed summary (structured, with event anchors) —
    the stored kinds exist only where the librarian has covered the thread.
    ``user_only`` is a back-compat alias for ``mode`` (true→user, false→full);
    prefer ``mode``, which wins if both are set.

    Args:
        thread_id: ULID thread id, legacy integer alias, or a provider session
            uuid (source_id) — all resolved to the thread automatically.
        limit: Max turns per chunk (safety cap; the char budget usually bites first).
            Default: 200.
        offset: Skip first N turns. Use the offset from a CHUNKED footer to read the
            next chunk. Negative counts from end: -20 = last 20 turns. Default: 0.
        summary: Summary view instead of full content — true/'toc' for a compact
            TOC with previews, 'short' or 'indexed' for the stored thread summary.
        mode: View — 'user' (default), 'chat', 'full', 'last' (final assistant
            text only), or 'ends' (first + last turns). Default: user.
        user_only: Back-compat alias for mode (true→user, false→full). Prefer mode.
        tool_results: Include tool output under each call (default off; needs 'full').
        max_chars: Per-chunk character budget; the read stops at a clean turn
            boundary once hit and the footer points at the next offset. Default: ~48k.
            Lower it (e.g. 8000) for a cheap skim of a long thread.
        after_event: Resume reading from the turn AFTER this event id (overrides
            offset). Robust way to continue from where a previous read stopped.
        around_event: Open this search-result event in its containing turn with
            surrounding conversation. Overrides offset and after_event.
        context_turns: Turns to include before and after around_event, or per end
            for mode='ends'. Default: 1.
    """
    _maybe_catch_up()
    # Log in a finally so a raising read still leaves its usage record —
    # a failed read is usage evidence too — with the latency it burned.
    started = time.monotonic()
    try:
        return api.read_thread(
            thread_id,
            limit=limit,
            offset=offset,
            summary=summary,
            mode=mode,
            user_only=user_only,
            tool_results=tool_results,
            max_chars=max_chars,
            after_event=after_event,
            around_event=around_event,
            context_turns=context_turns,
        )
    finally:
        _usage.record_read(
            thread_id,
            params={
                "mode": mode, "summary": summary or None, "offset": offset,
                "limit": limit, "after_event": after_event,
                "around_event": around_event, "tool_results": tool_results,
            },
            duration_ms=(time.monotonic() - started) * 1000.0,
        )


def main() -> None:
    # stdio (default) is one server per client — every connecting agent spawns its own
    # process, and this one loads the ~3 GB embedding + cross-encoder stack. --http instead
    # serves streamable-HTTP on one loopback port so every agent shares a single always-on
    # server (one model resident, not one per client); the LaunchAgent runs that mode and the
    # MCP client config points at the URL. Stdio stays the default so `claude mcp add …
    # archive-mcp` and `python -m thread_archive._mcp.server` keep working with no daemon.
    parser = argparse.ArgumentParser(prog="archive-mcp", description=__doc__)
    parser.add_argument(
        "--http", action="store_true",
        help="serve streamable-HTTP (shared, always-on) instead of per-client stdio",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (--http only)")
    parser.add_argument("--port", type=int, default=8788, help="HTTP bind port (--http only)")
    args = parser.parse_args()

    # Warm the embedding + cross-encoder models on a background daemon thread. The cold load
    # is tens of seconds; when it lands inside the first conceptual search it can exceed the
    # client's MCP request timeout (commonly 60s), which surfaces to the model as a
    # failed tool call. Warming at startup moves that cost off the request path — the models
    # are (usually) resident by the time the first query arrives, and _load()'s lock makes an
    # early query that races the warm wait on one load rather than kick off a second. Daemon
    # so it never holds up interpreter exit; warm_models is fail-soft (a missing extra / load
    # failure just restores the old lazy behaviour).
    #
    # Only the shared HTTP server warms: it's the one hot copy every client shares, so its
    # ~3 GB model stack pays off. A per-client stdio server stays lean (~model unloaded, load
    # is lazy on first use) — a client that only reads, never searches, or whose config was
    # snapshotted to stdio before the HTTP switch shouldn't each hold 3 GB. A standalone stdio
    # deployment with no shared server can opt back into warming with THREAD_ARCHIVE_MCP_WARM=1.
    warm = args.http or os.environ.get("THREAD_ARCHIVE_MCP_WARM", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if warm:
        threading.Thread(target=warm_models, name="archive-warm-models", daemon=True).start()
    # Startup catch-up: whatever landed in the local stores since the last
    # ingest (by any process) is searchable by the time the first query
    # arrives — or shortly after; the pass is additive, never blocking.
    _maybe_catch_up()

    if args.http:
        # Refuse a non-loopback bind unless THREAD_ARCHIVE_MCP_NONLOCAL=1 — the same
        # guard the web viewer applies, for the same reason: this server is
        # unauthenticated full read of the archive, so exposing it beyond the
        # machine must be a deliberate act, not a typo'd --host.
        if args.host not in ("127.0.0.1", "::1", "localhost") and (
            os.environ.get("THREAD_ARCHIVE_MCP_NONLOCAL") != "1"
        ):
            parser.error(
                f"refusing non-loopback bind {args.host!r}: archive-mcp has no auth and "
                f"serves the full archive. Set THREAD_ARCHIVE_MCP_NONLOCAL=1 to expose "
                f"it deliberately."
            )
        # Stateless + JSON responses: each request is self-contained (no held-open per-client
        # SSE stream or server-side session to track across many agents), and the read-only
        # tools have nothing to push back. run() reads these off mcp.settings at start.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.stateless_http = True
        mcp.settings.json_response = True
        mcp.run("streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
