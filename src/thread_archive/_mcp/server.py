"""Library-native MCP server.

Exposes ``thread_search`` + ``thread_read`` as MCP tools that call the
:mod:`thread_archive._api` library functions directly — no web framework, no HTTP,
no route layer. The server is library-native: it dispatches straight to the API
functions in-process.

The tools and, by default, the server process are read-only. An operator may
explicitly set ``THREAD_ARCHIVE_MCP_INGEST=1`` to cohost lazy catch-up ingest
(see :class:`IngestThrottle` and :mod:`.._watcher.lazy`): a throttled background
pass at startup and around tool calls keeps the archive current with no daemon
installed, and degrades to a no-op flock probe when the always-on watcher owns
ingest. Setup-generated stdio client entries carry that explicit opt-in.

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
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

from .. import _api as api
from .._config import ENV_MCP_INGEST
from .._retrieval import (
    DEFAULT_CONTENT_TYPES,
    DEFAULT_EXCLUDE_CONTENT_TYPES,
    _contention,
    _probe,
    format_results,
    warm_models,
)
from .._retrieval import usage as _usage

logger = logging.getLogger(__name__)

mcp = FastMCP("thread-archive")

# ── lazy catch-up ingest ──────────────────────────────────────────────────────
# The opted-in zero-daemon freshness path: the server runs a background catch-up
# pass at startup and (throttled) around tool calls when its client config sets
# THREAD_ARCHIVE_MCP_INGEST=1.
# Cross-process safety lives in the pass itself (see _watcher.lazy): the
# ingest-owner flock makes every pass a no-op probe while the always-on watcher
# daemon — or another server's pass — owns ingest. The behavior is off unless
# THREAD_ARCHIVE_MCP_INGEST explicitly opts in.
_INGEST_MIN_INTERVAL = 300.0  # seconds between catch-up attempts in this process


def ingest_enabled() -> bool:
    """Whether cohosted catch-up ingest has been explicitly enabled.

    Read per call so a value set after import is honored. Missing, malformed,
    and negative values are all read-only; only an affirmative value opts in.
    """
    return os.environ.get(ENV_MCP_INGEST, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class IngestThrottle:
    """The gate on one process's cohosted catch-up ingest: at most one pass in
    flight, at most one attempt per ``_INGEST_MIN_INTERVAL``, and none at all
    while the kill-switch is off.

    The server's own gate is the module-level :data:`INGEST`; the state is held
    on the instance rather than in module globals, so a caller that wants an
    independent gate constructs its own.
    """

    def __init__(self) -> None:
        self.last = 0.0  # monotonic time of the last attempt (0 = never)
        self.running = threading.Lock()  # one in-flight catch-up per process

    def claim(self) -> bool:
        """Take the in-flight slot for a catch-up pass, or refuse it. A true
        return means the caller owns the slot and must :meth:`release` it."""
        if not ingest_enabled():
            return False
        now = time.monotonic()
        if self.last and now - self.last < _INGEST_MIN_INTERVAL:
            return False
        if not self.running.acquire(blocking=False):
            return False  # a catch-up is already in flight in this process
        self.last = now
        return True

    def release(self) -> None:
        """Hand the in-flight slot back, so the next attempt past the interval
        can claim it."""
        self.running.release()

    def run_pass(self) -> None:
        """Run one catch-up pass on a claimed slot, releasing it whatever
        happens. Never raises — ingest is advisory to retrieval."""
        try:
            from .._watcher import catch_up_once

            catch_up_once()
        except Exception:  # noqa: BLE001 — advisory; retrieval must not care
            logger.exception("lazy catch-up ingest failed")
        finally:
            self.release()

    def maybe_catch_up(self) -> None:
        """Kick a background catch-up pass, throttled. Never blocks the caller and
        never raises — retrieval must work identically with ingest disabled, owned
        by another process, or broken."""
        if not self.claim():
            return
        threading.Thread(
            target=self.run_pass, name="archive-lazy-ingest", daemon=True
        ).start()


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


# The agent-facing default search scope: the whole conversation — what anyone
# said, thought, or ran. An answer lives wherever it happens to live, and a scope
# narrower than the transcript makes "not found" mean "not found *here*", which
# reads identically to the conversation not existing.
#
# What tool handed *back* is not in scope, because it is not in the index at all
# (see :data:`.._retrieval._extract.UNINDEXED_CONTENT_TYPES`) — the one exclusion
# that measured better rather than merely cheaper.
#
# Stored thread summaries stay opt-in: they are derived text (the librarian writes
# them over the archive), not the record, so a search should not answer from them
# unless asked — content_type='summary' targets them, content_type='all' includes
# them.
#
# The scope itself lives in the retrieval layer, which shares it with the warm pass.
DEFAULT_SEARCH_CONTENT_TYPES = DEFAULT_CONTENT_TYPES
DEFAULT_SEARCH_EXCLUDE = DEFAULT_EXCLUDE_CONTENT_TYPES


def _commit_note(scope: dict) -> str:
    """What a ``commit=`` scope resolved to, as the note printed above the results.

    Every other scope filters by one fixed relation — ``path`` means "touched this
    file", ``topic_id`` means "cited under this topic". This one is a *set* of
    contributing sessions assembled from an authorship window, so the note carries
    what the rows cannot: how much of the commit each accounts for, which of them
    actually ran it, and the fact that file overlap is evidence rather than proof.
    """
    sha, resolution = scope["sha"], scope["resolution"]
    if resolution == "invalid":
        return f"note: '{sha}' is not a commit sha — {scope['note']}\n"
    if resolution == "unknown":
        searched = ", ".join(scope.get("searched_repos") or []) or "(none found)"
        return (f"note: commit {sha} — no match. {scope['note']}.\n"
                f"      repos searched: {searched}\n"
                f"      If the repository is elsewhere, pass repo='/path/to/repo'.\n")
    if resolution == "recorded-only":
        c = scope["commit"]
        return (f"note: commit {c['sha']} — only the committing session is known. "
                f"{scope['note']}.\n"
                f"      \"{c['subject'] or '(no subject)'}\" · "
                f"{str(c['occurred_at'] or '')[:16]}\n")
    c = scope["commit"]
    committed = scope["committed_by"]
    who = (", ".join(committed) if committed
           else "nobody in this archive (committed outside any session)")
    shares = " · ".join(
        f"{t['thread_id']} {len(t['matched_files'])}/{len(c['files'])}"
        + (" (ran it)" if t["committed"] else "")
        for t in scope["threads"][:6]
    )
    note = (f"note: commit {c['sha'][:12]} — {scope['total_threads']} contributing "
            f"session(s). {scope['note']}.\n"
            f"      \"{c['subject']}\" · {c['committed_at'][:16]} · "
            f"{len(c['files'])} file(s) · {c['repo']}\n"
            f"      ran the commit: {who}\n"
            f"      share of its files: {shares}\n")
    if scope.get("window_capped"):
        note += ("      (history walk hit its bound — some files have no lower bound "
                 "and may over-credit)\n")
    return note


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
                f"remedy: thread_archive fix-import {source}"
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
    path: Optional[str] = None,
    path_ops: Optional[str] = None,
    commit: Optional[str] = None,
    repo: Optional[str] = None,
    sort: Optional[str] = None,
    group: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
    match: Optional[str] = None,
    page: int = 1,
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
    ``query='', source='cursor'``; ``sort='oldest'`` flips to the earliest
    threads. Each row carries the thread id (open it: ``thread_read``) and its
    newest event id (open at the tail: ``around_event``). A browse hides topic
    and system threads unless ``types``/``agents`` says otherwise; ranking
    options (content_type, context, rerank) don't apply.

    The whole conversation is searched by default — user messages, thread titles,
    assistant text, its reasoning, and the tool calls that were run. What a tool
    handed **back** is not searchable at all: tool output is preserved in full and
    replays in ``thread_read``, but it is deliberately left out of the index, where
    it buried real answers under grep dumps and re-read files. Stored thread
    summaries (librarian-derived text, not the record) are the one opt-in scope:
    pass ``content_type='summary'`` to target them or ``content_type='all'`` to
    fold them in; a specific ``content_type`` (user/text/thinking/tool/title/...)
    narrows to one.

    Query grammar: natural language, "quoted phrases", boolean AND/OR/NOT,
    pipe-OR (a|b), and code identifiers (get_session, a.b.c). Filter by
    ``thread_id`` or ``topic_id`` (a topic's member conversations) —
    both accept a ULID thread id, a legacy integer alias, or a provider session
    id, the same ref shapes ``thread_read`` takes —
    ``content_type`` (default: everything but derived summaries; 'all' folds
    those in too),
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

    **The code axis.** Search finds where something was *discussed*; ``path`` and
    ``commit`` find where it was *done*. Every path the archive's tools named — each
    ``Edit``, ``Read``, ``Write``, ``apply_patch`` header, and path-shaped shell
    argument, in every provider's spelling — is indexed structurally, so these are
    lookups rather than text searches that happen to match a path.

    ``path`` takes a **bare name** (``rank.py``), a **partial path**
    (``_retrieval/rank.py``), an **absolute path** — a file, or a directory whose
    whole subtree matches, which is how you ask about a repo or a module
    (``path='/repo', path_ops='edit,write,delete'`` = "which sessions changed
    anything in this repo") — or a **glob** (``*.py``). ``path_ops`` narrows the
    verbs: ``edit`` / ``write`` / ``delete`` are changes, ``read`` is a look, and
    ``search`` (a grep's scope) / ``run`` (a path inside a shell command) are
    incidental mentions, kept distinguishable rather than dropped.

    With an **empty query** that is the whole "who worked on this file" answer: one
    row per conversation, ordered changes-before-looks, each carrying its op tally,
    the window of touches, and an ``event_id`` that opens at the work rather than at
    the thread's tail. With a query it scopes the search instead — "retry backoff" +
    ``path='rank.py'`` is what we said about retries while working on that file. The
    inverse ("what did this session change") is
    ``thread_read(thread_id, summary='files')``.

    ``commit`` is the loop back from ``git blame``: git names the commit, this names
    the conversations it is **made of** — every session whose edits to its files fall
    inside its authorship window (after each file was last committed, up to this
    commit). Usually more than one: a commit carries work from several sittings. The
    session that *ran* ``git commit`` is flagged among them, not substituted for
    them — wherever a human commits out of band it is nobody, and where an agent
    commits it is usually just the session that typed the command. The note above the
    results carries what the rows can't: each session's share of the commit's files,
    which of them ran it, and that file overlap is evidence rather than proof.
    ``repo='/path'`` points at the repository when it isn't one the archive has seen
    sessions run in; without a reachable repo only a recorded committer can be named.
    Empty query lists those sessions; a query searches inside them.

    ``startswith`` does a structural prefix scan (content LIKE 'prefix%'; query text
    unused). ``sort='oldest'`` returns matches chronologically (find when something
    was first discussed) instead of the default recency-biased ranking; it is the
    only sort, and any other value is an error. For the most *recent* mention,
    enumerate the matches (``group='browse'``) and read the latest date off them —
    there is no newest sort to ask for.
    ``context_lines`` (default 2; set 0 for the raw FTS snippet) replaces each
    snippet with a numbered ±N-line window around the match; ``context_events``
    ('N' / 'before:after' /
    'before:after:types', e.g. '2' or '0:1:user') appends the neighbouring events.
    ``output='count'`` returns a per-thread tally (no snippets); ``output='linkable'``
    returns JSON of event/thread ids. ``rerank`` forces the cross-encoder head
    re-rank on/off (else auto-gated: conceptual queries whose top hit isn't
    already a strong literal match, when the ``[embeddings]`` extra is installed).

    **Listing every match.** Results are a page, and the header says which:
    ``12 of 340 · page 1/29``. ``page=N`` (1-based) walks them. Pages are slices
    of one ordering, so walking them never repeats or skips a row, and a page past
    the end says so instead of looking like a query that matched nothing.

    ``group='browse'`` is the shape that enumerates **completely**: its thread
    list is resolved from the whole match set rather than cut from the ranked
    candidate pool, so ``N of M`` is a real total and paging to the last page
    reaches every matched thread. The other shapes rank a bounded pool, so they
    report ``N of ≥M`` and say ``truncated`` — for "find me every thread that
    mentions X", use ``group='browse'`` and page to the end. A ``+`` on a total
    (``≥5000+``) means even the set scan stopped early, so it is a floor.

    ``match`` picks what counts as a match. ``'token'`` (default) is the indexed
    search described above: it matches whole words, so ``p4`` finds ``p4`` and not
    ``mp4``. ``'substring'`` matches raw text anywhere inside a word — ``p4`` then
    also finds ``mp4``, ``p400``, ``gcp4`` — which no index can do, so it pays a
    full-table scan (seconds on a large archive) and runs no fallback tiers. Reach
    for it when enumerating every occurrence of an identifier, a fragment, or a
    string that lives inside longer words; leave it alone otherwise.
    """
    INGEST.maybe_catch_up()
    # Bound caller-supplied sizing before it reaches the engine: limit drives a
    # candidate pool of max(limit*5, 200) rows, so an unclamped value forces a
    # multi-million-row FTS scan. The web layer clamps to the same [1, 500] for
    # exactly this reason; context_lines is a per-hit window, bounded likewise.
    limit = max(1, min(int(limit), 500))
    context_lines = max(0, min(int(context_lines), 50))
    # The pool is sized from page*limit, so an unbounded page is an unbounded
    # scan by another name. 200 pages of the 500-row max is far past any real
    # enumeration and still a bounded worst case.
    page = max(1, min(int(page), 200))
    if match is not None and match not in ("token", "substring"):
        return ("match must be 'token' (indexed, default) or 'substring' "
                "(uncapped infix scan — finds p4 inside mp4)")
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
    # Default scope is the whole conversation minus derived summaries; an explicit
    # type targets one, and content_type='all' drops even the summary exclusion
    # (see the constants).
    default_exclude: tuple[str, ...] = ()
    if content_type == "all":
        content_types = None
    elif content_type:
        content_types = [content_type]
    else:
        content_types = DEFAULT_SEARCH_CONTENT_TYPES
        default_exclude = DEFAULT_SEARCH_EXCLUDE
    exclude = [c.strip() for c in exclude_content_type.split(",") if c.strip()] if exclude_content_type else None
    sources = [s.strip() for s in source.split(",") if s.strip()] if source else None
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    op_list = [o.strip() for o in path_ops.split(",") if o.strip()] if path_ops else None

    # An ordinary thread scope, like topic_id — but resolved here rather than in the
    # engine, because the miss has to explain itself: a sha in no session and no
    # known repo scopes to nothing, and bare zero rows would read as "no session
    # touched this commit" when the truth is "that sha was never found". This is the
    # layer that renders notes (the widen retry sits here for the same reason).
    commit_note = ""
    commit_threads: Optional[list[str]] = None
    if commit:
        verdict = api.blame(commit=commit, repo=repo, limit=max(limit, 10))
        commit_note = _commit_note(verdict)
        commit_threads = [t["thread_id"] for t in verdict["threads"]]
        if not commit_threads:
            return _degradation_notices() + commit_note

    def _run(cts, extra_exclude=()):
        return api.search(
            query,
            limit=limit,
            thread_id=thread_id,
            topic_id=topic_id,
            content_types=cts,
            exclude_content_types=[*(exclude or []), *extra_exclude] or None,
            since=since,
            until=until,
            tool_name=tool_name,
            source=sources,
            types=type_list,
            agents=agents,
            startswith=startswith,
            path=path,
            path_ops=op_list,
            thread_ids=commit_threads,
            sort=sort,
            group=group,
            output=output,
            context_lines=context_lines,
            context_events=context_events,
            rerank=rerank,
            match=match or "token",
            page=page,
        )

    # ``duration_ms`` covers the retrieval work as the caller felt it — both arms —
    # and ``render_ms`` the formatting that turns hits into the text the agent
    # actually reads. Both are the agent's wait; only the background catch-up above
    # is excluded, because it does not block.
    # None, not [], so a search that raised records no ``n_hits`` at all rather
    # than an empty one — "it failed" and "it found nothing" are different facts.
    hits: Any = None
    probe = None
    retrieval_ms: Optional[float] = None
    render_ms: Optional[float] = None
    context: dict = {}
    span: Any = None
    started = time.monotonic()
    # Log in a finally so a raising search still leaves its usage record — a search
    # that failed slowly is the most important latency evidence there is, and it is
    # exactly the one an exception would otherwise erase.
    try:
        # Contention is sampled inside the in-flight span (so this call counts
        # itself) and at the *start* of the work: what the machine was doing when
        # this search began is what shaped its latency. Sampling after would report
        # a background rebuild that finished during the search as absent. The
        # concurrency peak is the exception and is folded in below, because the
        # peers that slow a search include the ones that arrive while it runs.
        with _contention.in_flight() as span, _probe.install() as probe:
            context = _contention.sample()
            hits = _run(content_types, extra_exclude=default_exclude)

        retrieval_ms = (time.monotonic() - started) * 1000.0
        _t_render = time.monotonic()
        rendered = format_results(hits, query, output=output)
        render_ms = (time.monotonic() - _t_render) * 1000.0
        return _degradation_notices() + commit_note + rendered
    finally:
        # Usage ledger (fail-soft, ids + timings only — see _retrieval.usage): the
        # observed ground truth future retrieval evals are built from, carrying a
        # per-stage latency breakdown (which stage a slow search spent its time in).
        # The concurrency peak lands here rather than beside the sample so that a
        # search which *raised* still reports how busy the process was — the slow
        # failures are the rows a contention question most needs.
        context.update(_contention.peak_inflight(span))
        _usage.record_search(
            query,
            params={
                "limit": limit, "thread_id": thread_id, "topic_id": topic_id,
                "content_type": content_type,
                "exclude_content_type": exclude_content_type, "since": since,
                "until": until, "tool_name": tool_name, "source": source,
                "types": types, "agents": agents, "path": path,
                "path_ops": path_ops, "commit": commit,
                "startswith": startswith, "sort": sort, "group": group,
                "output": output, "rerank": rerank, "match": match, "page": page,
            },
            hits=hits,
            # Retrieval alone, so the field keeps meaning what every recorded
            # search so far has meant; render is its own number beside it. A search
            # that raised never rendered, so its whole elapsed time is retrieval.
            duration_ms=(retrieval_ms if retrieval_ms is not None
                         else (time.monotonic() - started) * 1000.0),
            render_ms=render_ms,
            failed=sys.exc_info()[0] is not None,
            timings=probe.as_record() if probe is not None else None,
            context=context,
        )


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

    A **topic id** (from a topic link in an old conversation) reads as the
    topic's page instead of a transcript — a render of existing
    topic-graph records; this server only reads them.

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
    the stored kinds exist only where a thread has one; ``'files'`` = the **files
    this session touched**, tallied per file with changes first and an event id per
    file to open the transcript where it was last worked on. That is the code axis
    read backwards — ``thread_search(path=…)`` asks which sessions touched a file,
    this asks which files a session touched.
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
            TOC with previews, 'short' or 'indexed' for the stored thread summary,
            'files' for the files this session touched.
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
    INGEST.maybe_catch_up()
    # Log in a finally so a raising read still leaves its usage record —
    # a failed read is usage evidence too — with the latency it burned.
    started = time.monotonic()
    out: object = None
    context: dict = {}
    span: Any = None
    try:
        with _contention.in_flight() as span:
            context = _contention.sample()
            out = api.read_thread(
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
        return out
    finally:
        context.update(_contention.peak_inflight(span))
        _usage.record_read(
            thread_id,
            params={
                "mode": mode, "summary": summary or None, "offset": offset,
                "limit": limit, "after_event": after_event,
                "around_event": around_event, "tool_results": tool_results,
            },
            duration_ms=(time.monotonic() - started) * 1000.0,
            chars=len(out) if isinstance(out, str) else None,
            failed=sys.exc_info()[0] is not None,
            context=context,
        )


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# The transports FastMCP.run() serves.
Transport = Literal["stdio", "sse", "streamable-http"]


@dataclass(frozen=True)
class ServePlan:
    """What one ``archive-mcp`` invocation decided to do.

    ``transport`` is the argument :meth:`FastMCP.run` takes, or ``None`` for the
    stdio default. ``host``/``port`` are the parsed bind and apply to the HTTP
    transport only.
    """

    transport: Optional[Transport] = None
    warm: bool = False
    host: str = "127.0.0.1"
    port: int = 8788


def _parser() -> argparse.ArgumentParser:
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
    return parser


def plan_serve(argv: Optional[Sequence[str]] = None) -> ServePlan:
    """Read a command line (the process's when ``argv`` is None) into the plan
    :func:`main` executes — transport, bind, and whether to warm the models.

    Exits through the parser's usage error when the requested bind is not
    loopback and ``THREAD_ARCHIVE_MCP_NONLOCAL=1`` is unset — the same guard
    the web viewer applies, for the same reason: this server is unauthenticated
    full read of the archive, so exposing it beyond the machine must be a
    deliberate act, not a typo'd ``--host``.
    """
    parser = _parser()
    args = parser.parse_args(argv)

    # Only the shared HTTP server warms the model stack: it's the one hot copy every client
    # shares, so its ~3 GB of models pays off. A per-client stdio server stays lean (models
    # unloaded, load is lazy on first use) — a client that only reads, never searches, or
    # whose config points at stdio rather than the shared server, shouldn't each hold 3 GB. A
    # standalone stdio deployment with no shared server opts back in with
    # THREAD_ARCHIVE_MCP_WARM=1.
    warm = args.http or os.environ.get("THREAD_ARCHIVE_MCP_WARM", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not args.http:
        return ServePlan(transport=None, warm=warm, host=args.host, port=args.port)
    if args.host not in LOOPBACK_HOSTS and os.environ.get(
        "THREAD_ARCHIVE_MCP_NONLOCAL"
    ) != "1":
        parser.error(
            f"refusing non-loopback bind {args.host!r}: archive-mcp has no auth and "
            f"serves the full archive. Set THREAD_ARCHIVE_MCP_NONLOCAL=1 to expose "
            f"it deliberately."
        )
    return ServePlan(transport="streamable-http", warm=warm, host=args.host, port=args.port)


def apply_settings(plan: ServePlan) -> None:
    """Point the server at the plan's bind, ready for its HTTP transport.

    Stateless + JSON responses: each request is self-contained (no held-open
    per-client SSE stream or server-side session to track across many agents),
    and the read-only tools have nothing to push back. ``run()`` reads these off
    ``mcp.settings`` when it starts, so they are set before it is called.
    """
    mcp.settings.host = plan.host
    mcp.settings.port = plan.port
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Serve, per ``argv`` (the process's command line by default). Blocks in the
    transport's run loop until the client disconnects or the process is stopped."""
    plan = plan_serve(argv)
    # Warm the embedding + cross-encoder models on a background daemon thread. The cold load
    # is tens of seconds; when it lands inside the first conceptual search it can exceed the
    # client's MCP request timeout (commonly 60s), which surfaces to the model as a
    # failed tool call. Warming at startup moves that cost off the request path — the models
    # are (usually) resident by the time the first query arrives, and _load()'s lock makes an
    # early query that races the warm wait on one load rather than kick off a second. Daemon
    # so it never holds up interpreter exit; warm_models is fail-soft (a missing extra / load
    # failure just restores the lazy behaviour).
    if plan.warm:
        # Defer model construction to this warm: until it lands, a query serves
        # lexical-only (fast) instead of blocking on the tens-of-seconds cold load
        # — the arms rejoin automatically once the models are resident. Without
        # this a query racing the warm waits out the whole load in-request.
        from .._retrieval.model_slot import set_defer_construction

        set_defer_construction(True)
        threading.Thread(target=warm_models, name="archive-warm-models", daemon=True).start()
    # Startup catch-up: whatever landed in the local stores since the last
    # ingest (by any process) is searchable by the time the first query
    # arrives — or shortly after; the pass is additive, never blocking.
    INGEST.maybe_catch_up()
    if plan.transport is None:
        mcp.run()
        return
    apply_settings(plan)
    mcp.run(plan.transport)


if __name__ == "__main__":
    main()
