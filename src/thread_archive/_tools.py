"""The retrieval tools — ``thread_search`` and ``thread_read``, one implementation
behind both front doors.

Retrieval is served two ways: as MCP tools (:mod:`._mcp.server`, what an agent
calls mid-conversation) and as the ``thread-archive search`` / ``thread_archive
read`` CLI verbs (what a person types at a terminal). Both call the functions
here, so there is one contract rather than two that drift: the same default
scope, the same ref resolution, the same degradation notice, the same rendered
text, and one usage-ledger record per call.

These signatures *are* the MCP tool schema — FastMCP builds it from the
annotations — so a parameter added here reaches both surfaces, and the CLI's
flags mirror it one for one (:func:`.cli.cmd_search`, :func:`.cli.cmd_read`).

What an agent *reads* is tiered, because a tool description is paid for out of
every session's context whether or not the tool is ever called. The compact
contract each tool ships over the wire is its ``*_DESCRIPTION`` constant below:
enough to call it correctly, naming every parameter it takes. The long form is
the function's own docstring, which :func:`thread_help` serves on demand — the
manual stays beside the code that answers it and costs nothing until an agent
asks for it. A parameter documented in neither is one no caller can find, and
``tests/test_mcp.py`` holds that line.

What is *not* shared is anything a front door owns: the MCP server's cohosted
catch-up ingest is kicked by the server's own tool wrappers (a one-shot CLI
process would be killed mid-pass), and exit codes are the CLI's.
"""

from __future__ import annotations

import inspect
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional

from . import _api as api
from ._retrieval import (
    DEFAULT_CONTENT_TYPES,
    _contention,
    _probe,
    format_results,
)
from ._retrieval import usage as _usage

# Which front door a call is being served through, re-exported from the ledger that
# carries the field. Both the vocabulary and the ambient value live beside the
# ``surface`` column (:mod:`.._retrieval.usage`), because the retrieval package
# writes rows of its own — the background warm pass — and must be able to name a
# door without importing this module. The front doors themselves declare through
# these names: the MCP server and the web app call :func:`set_default_surface` once
# at startup, the CLI wraps each verb in :func:`serving`.
UNATTRIBUTED = _usage.UNATTRIBUTED
set_default_surface = _usage.set_default_surface
serving = _usage.serving
current_surface = _usage.current_surface
_served_by = _usage.served_by


# What the tool itself measured on this call, for a surface that wraps it and
# wants to know its own overhead. A mutable slot the tool fills on its way out and
# the wrapper reads after — see :func:`call_span`.
_CALL_MS: ContextVar[Optional[dict]] = ContextVar("thread_archive_call_ms", default=None)


@contextmanager
def call_span() -> Iterator[dict]:
    """Collect what the tool call inside this block measured of itself.

    A surface that wraps a tool — the MCP server, which kicks a catch-up ingest
    first and hands the result to a transport afterwards — can time its own call
    easily enough, but that number alone cannot say whether a slow call was slow
    *in retrieval* or slow in everything around it. The tool already computes
    exactly that (the retrieval and render halves it puts in the usage ledger);
    this is how it gets handed outward, without the tool learning who is calling
    or growing a second return value.

    Yields a dict the block fills with ``tool_ms`` — read it after. Empty when the
    call raised before it measured anything, which is itself the answer: the time
    went somewhere other than the work."""
    slot: dict = {}
    token = _CALL_MS.set(slot)
    try:
        yield slot
    finally:
        _CALL_MS.reset(token)


def _publish_call_ms(ms: float) -> None:
    """Hand this call's measured wall time to an enclosing :func:`call_span`, if
    any. Fail-soft and a no-op when nobody is listening."""
    slot = _CALL_MS.get()
    if slot is None:
        return
    try:
        slot["tool_ms"] = round(slot.get("tool_ms", 0.0) + ms, 1)
    except Exception:  # noqa: BLE001 — advisory; never break a tool call
        pass


def _resolve_ref(ref: int | str) -> Optional[str]:
    """Resolve a thread ref — a ULID thread id, a legacy integer alias, or
    a provider session id — to the archive's ULID thread id; None when nothing
    matches. See :func:`thread_archive._retrieval.read.resolve_thread_ref`."""
    from ._retrieval.read import resolve_thread_ref
    from ._store import get_session

    api.open_archive()
    with get_session() as s:
        return resolve_thread_ref(s, ref)


# The agent-facing default search scope: the whole conversation — what anyone
# said, thought, or ran. An answer lives wherever it happens to live, and a scope
# narrower than the transcript makes "not found" mean "not found *here*", which
# reads identically to the conversation not existing.
#
# Nothing is excluded at query time. What a search must not answer from is kept
# out of the index instead: what a tool handed *back* (see
# :data:`._retrieval._extract.UNINDEXED_CONTENT_TYPES` — a ranking decision as much
# as a cost one, since the ranker's density term is IDF-blind and cannot discount a
# grep dump itself), and stored thread summaries, which are derived text a curation
# tool wrote over the archive rather than the record.
#
# The scope itself lives in the retrieval layer, which shares it with the warm pass.
DEFAULT_SEARCH_CONTENT_TYPES = DEFAULT_CONTENT_TYPES


def _commit_note(scope: dict) -> str:
    """What a ``commit=`` scope resolved to, as the note printed above the results.

    Every other scope filters by one fixed relation — ``path`` means "touched this
    file", ``thread_id`` means "inside this conversation". This one is a *set* of
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


def _pr_note(scope: dict) -> str:
    """What a ``pr=`` scope resolved to, as the note printed above the results.

    Shorter than its commit sibling because the answer is: the sessions declared
    the association, so there is no share-of-the-work to apportion and no evidence
    to caveat. What the rows still cannot say is *which* pull request a bare number
    landed on when several repositories have one.
    """
    if scope["resolution"] == "invalid":
        return f"note: '{scope['ref']}' is not a pull request — {scope['note']}\n"
    if scope["resolution"] == "unknown":
        return (f"note: pull request {scope['ref']} — no match. {scope['note']}.\n"
                f"      Only sessions whose harness records the link contribute here.\n")
    url = scope.get("url")
    note = (f"note: pull request {scope['ref']} — {scope['total_threads']} session(s). "
            f"{scope['note']}.\n")
    if url:
        note += f"      {url}\n"
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

        from ._ops.health import read_health

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
        from ._ops.coverage import remedy_for

        lines = []
        for source in sorted(degraded):
            verdict = degraded[source] or {}
            reason = str(verdict.get("reason") or "")
            phrase = _DEGRADED_PHRASES.get(reason, "import degraded")
            since = str(verdict.get("since") or "")[:10]
            lines.append(
                f"note: {source} import is degraded ({phrase}"
                + (f" since {since}" if since else "")
                + f") — recent {source} content may be missing from results. "
                f"remedy: {remedy_for(reason, source)}"
            )
        return "\n".join(lines) + "\n"
    except Exception:  # noqa: BLE001 — advisory; retrieval must not care
        return ""


# ── the wire descriptions ─────────────────────────────────────────────────────
# What an agent is handed when it lists the tools, as opposed to the manual it can
# ask for (see this module's docstring, and :func:`thread_help`). Every parameter
# is named here even when its grammar is not — a caller that knows a filter exists
# can ask what it takes, but one that has never heard of it cannot.

SEARCH_DESCRIPTION = """\
Search the local conversation archive — every AI chat this machine has had, across \
providers, full transcripts.

`query` is natural language, "quoted phrases", boolean AND/OR/NOT, pipe-OR (a|b), or \
code identifiers (get_session, a.b.c). An **empty query browses** instead: one row \
per thread, newest activity first, honoring the filters — query='', since='1d' is \
"what happened yesterday".

**Every matching message is a row.** A thread matching eight times returns eight \
rows, `limit` counts messages rather than threads, `page=N` walks them, and the \
header says `12 of 340 · page 1/29`. Read the header's \
`quality=strong|partial|weak|semantic` and each hit's `K/N` term count before \
trusting a result: weak / 0-of-N means these are nearest-neighbour guesses and the \
archive may simply not hold it, so rephrase the concept rather than piling on \
synonyms. Open a hit with `thread_read(thread_id, around_event=<event_id>)`.

Filters: `thread_id` (a ULID, a legacy integer id, or a provider session id), \
`content_type` (user/text/thinking/tool/title; default: everything indexed), \
`source` ('claude-code,cursor'), `since`/`until` ('2h'/'7d'/'2w' or an ISO \
timestamp), \
`tool_name`, `types`, `agents` ('include'/'only' — agent-run subagent threads are \
excluded by default), `sort='oldest'`, `match='substring'` (uncapped infix scan: \
finds p4 inside mp4), `output` ('count'/'linkable'), `exclude_content_type`, \
`startswith`, `context_lines`/`context_events`, and **the code axis** — `path` + \
`path_ops` for which sessions touched a file (bare name, partial path, directory \
subtree, or glob), `commit` + `repo` for the sessions a commit is made of, and `pr` \
for the sessions that worked a pull request.

What a tool handed *back* is not indexed — it replays in `thread_read` — and neither \
are stored thread summaries.

`thread_help('search')` is the full manual: the code axis and commit blame, complete \
enumeration and paging, browse recipes, every filter's grammar."""


def thread_search(
    query: str,
    limit: int = 10,
    thread_id: Optional[int | str] = None,
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
    pr: Optional[str] = None,
    repo: Optional[str] = None,
    sort: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    match: Optional[str] = None,
    page: int = 1,
) -> str:
    """Search the local conversation archive (federated: lexical FTS5 + optional
    semantic vectors → fusion → rank → community-coherence head re-rank).

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
    newest event id (open at the tail: ``around_event``). A browse hides
    non-conversation threads unless ``types``/``agents`` says otherwise; ranking
    options (content_type, context) don't apply.

    The whole conversation is searched by default — user messages, thread titles,
    assistant text, its reasoning, and the tool calls that were run; a specific
    ``content_type`` (user/text/thinking/tool/title) narrows to one. Two things are
    not searchable at all, by design. What a tool handed **back**: tool output is
    preserved in full and replays in ``thread_read``, but it is left out of the
    index, where it buried real answers under grep dumps and re-read files. And
    stored thread summaries: they are derived text a curation tool wrote *over* the
    archive, not the record, so a search must not answer from a machine's
    description of a conversation — read one deliberately with ``thread_read(...,
    summary='short')``.

    Query grammar: natural language, "quoted phrases", boolean AND/OR/NOT,
    pipe-OR (a|b), and code identifiers (get_session, a.b.c). Filter by
    ``thread_id`` — a ULID thread id, a legacy integer alias, or a provider
    session id, the same ref shapes ``thread_read`` takes —
    ``content_type`` (default: everything indexed),
    ``exclude_content_type`` (comma-separated types to drop), ``tool_name``,
    ``source`` (comma-separated providers, e.g. 'claude-code,cursor'),
    ``types`` (comma-separated ``thread_type`` values — 'conversation',
    'system'), and a ``since``/``until`` window (an ISO timestamp, or a relative
    age: '2h', '7d', '2w').

    Agent-run threads — subagent / machinery sessions (🤖-titled) — are
    **excluded by default**: a swarm echoes its spawning prompt verbatim, and
    those copies would drown the conversation that asked. Pass
    ``agents='include'`` to search them alongside conversations, or
    ``agents='only'`` for just them ("what did my subagents do"). An explicit
    ``thread_id`` scope always reaches them.

    **Every matching message is a row.** Results are not grouped or folded by
    thread: a conversation matching eight times returns eight rows, each with its
    own snippet and context, and ``limit`` counts messages rather than threads. Ask
    for more with ``limit``, or walk with ``page``; the header names how many
    matched in total and says ``truncated`` when the walk stops short of them.

    **The code axis.** Search finds where something was *discussed*; ``path``,
    ``commit`` and ``pr`` find where it was *done*. Every path the archive's tools
    named — each ``Edit``, ``Read``, ``Write``, ``apply_patch`` header, and
    path-shaped shell argument, in every provider's spelling — is indexed
    structurally, so these are lookups rather than text searches that happen to
    match a path.

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

    ``pr`` is the loop back from a pull request, and it is not a commit lookup by
    another name: a PR is a unit of *intent* and a commit is a unit of *change*, so
    the two scopes disagree on purpose. A commit's contributors are inferred from
    file overlap inside its authorship window; a PR's are stated — the harness
    recorded which one the session was on — so ``pr`` needs no window, no
    corroboration, and no reachable repository, and it reaches the sessions that
    left no commit in the branch at all (the review round, the approach that was
    abandoned, the one that only wrote the description). Takes a bare number
    (``pr='4'``), a repo-qualified ref (``pr='ellamental/thread_archive#4'``), or the
    URL off the address bar. A bare number matching several repositories returns all
    of them and says so — narrow with ``repo='thread_archive'`` (a suffix match, so
    the owner is optional). Empty query lists those sessions; a query searches inside
    them. Only harnesses that record the link contribute, so a PR worked on before
    that existed reads as unknown rather than as unworked.

    ``startswith`` does a structural prefix scan (content LIKE 'prefix%'; query text
    unused). ``sort='oldest'`` returns matches chronologically (find when something
    was first discussed) instead of the default recency-biased ranking; it is the
    only sort, and any other value is an error. For the most *recent* mention,
    enumerate the matches and read the latest date off them — there is no newest
    sort to ask for.
    ``context_lines`` (default 2; set 0 for the raw FTS snippet) replaces each
    snippet with a numbered ±N-line window around the match; ``context_events``
    ('N' / 'before:after' /
    'before:after:types', e.g. '2' or '0:1:user') appends the neighbouring events.
    ``output='count'`` returns a per-thread tally (no snippets); ``output='linkable'``
    returns JSON of event/thread ids.

    **Listing every match.** Results are a page, and the header says which:
    ``12 of 340 · page 1/29``. ``page=N`` (1-based) walks them. Pages are slices
    of one ordering, so walking them never repeats or skips a row, and a page past
    the end says so instead of looking like a query that matched nothing.

    The thread list **enumerates completely**: membership comes from the whole
    match set, not from the ranked candidate pool, so ``N of M`` is a real total
    and paging to the last page reaches every matched thread — for "find me every
    thread that mentions X", just page to the end. A ``+`` on a total (``≥5000+``)
    means even the set scan stopped early, so it is a floor. The hit-granular
    shapes rank a bounded pool and report
    ``N of ≥M`` with ``truncated``, since a hit list has no set to reconcile
    against.

    ``match`` picks what counts as a match. ``'token'`` (default) is the indexed
    search described above: it matches whole words, so ``p4`` finds ``p4`` and not
    ``mp4``. ``'substring'`` matches raw text anywhere inside a word — ``p4`` then
    also finds ``mp4``, ``p400``, ``gcp4`` — which no index can do, so it pays a
    full-table scan (seconds on a large archive) and runs no fallback tiers.
    ``OR`` and ``|`` separate alternative substrings (``"foo=" OR "foo axis"``
    matches rows containing either literal). Reach
    for it when enumerating every occurrence of an identifier, a fragment, or a
    string that lives inside longer words; leave it alone otherwise.
    """
    # Bound caller-supplied sizing before it reaches the engine: limit drives a
    # candidate pool of max(limit*5, 200) rows, so an unclamped value forces a
    # multi-million-row FTS scan. The web layer clamps to the same [1, 500] for
    # exactly this reason; context_lines is a per-hit window, bounded likewise.
    limit = max(1, min(int(limit), 500))
    context_lines = max(0, min(int(context_lines), 50))
    # Every page is a slice of ONE pool, sized independently of ``page``
    # (max(limit*5, 200) rows — see :func:`.._retrieval.search`), so a large page
    # number costs nothing: it slices past the end and says so. 200 is the deepest
    # page any limit can reach — at limit=1 the 200-row pool floor is exactly 200
    # pages, and every larger limit reaches fewer — so the bound cuts off nothing
    # a walk could have returned.
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
    # Default scope is everything indexed; an explicit type targets one. ``'all'``
    # is accepted as a spelling of that default rather than rejected: it is a
    # content type no doc carries, so treating it literally would filter the search
    # down to nothing and return a confident "No results".
    content_types: Optional[list[str]]
    if content_type and content_type != "all":
        content_types = [content_type]
    else:
        content_types = DEFAULT_SEARCH_CONTENT_TYPES
    exclude = [c.strip() for c in exclude_content_type.split(",") if c.strip()] if exclude_content_type else None
    sources = [s.strip() for s in source.split(",") if s.strip()] if source else None
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    op_list = [o.strip() for o in path_ops.split(",") if o.strip()] if path_ops else None

    # Ordinary thread scopes — but resolved here rather than in the
    # engine, because the miss has to explain itself: a sha in no session and no
    # known repo scopes to nothing, and bare zero rows would read as "no session
    # touched this commit" when the truth is "that sha was never found". A pull
    # request nobody recorded reads the same way. This is the layer that renders
    # notes (the widen retry sits here for the same reason).
    scope_note = ""
    scoped_threads: Optional[list[str]] = None
    if commit:
        verdict = api.blame(commit=commit, repo=repo, limit=max(limit, 10))
        scope_note = _commit_note(verdict)
        scoped_threads = [t["thread_id"] for t in verdict["threads"]]
        if not scoped_threads:
            return _degradation_notices() + scope_note
    if pr:
        verdict = api.blame(pr=pr, repo=repo, limit=max(limit, 10))
        scope_note += _pr_note(verdict)
        pr_threads = [t["thread_id"] for t in verdict["threads"]]
        # Both scopes given is an intersection, not a replacement: "the sessions in
        # this commit that were also on that PR" is the only reading under which
        # both arguments still mean what they mean alone.
        scoped_threads = (pr_threads if scoped_threads is None
                          else [t for t in scoped_threads if t in set(pr_threads)])
        if not scoped_threads:
            return _degradation_notices() + scope_note

    def _run(cts):
        return api.search(
            query,
            limit=limit,
            thread_id=thread_id,
            content_types=cts,
            exclude_content_types=exclude,
            since=since,
            until=until,
            tool_name=tool_name,
            source=sources,
            types=type_list,
            agents=agents,
            startswith=startswith,
            path=path,
            path_ops=op_list,
            thread_ids=scoped_threads,
            sort=sort,
            output=output,
            context_lines=context_lines,
            context_events=context_events,
            match=match or "token",
            page=page,
        )

    # ``duration_ms`` covers the retrieval work as the caller felt it — both arms —
    # and ``render_ms`` the formatting that turns hits into the text the agent
    # actually reads. Both are the caller's wait; the MCP server's background
    # catch-up is excluded, because it does not block.
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
            context = _contention.sample() if _usage.enabled() else {}
            hits = _run(content_types)

        retrieval_ms = (time.monotonic() - started) * 1000.0
        _t_render = time.monotonic()
        rendered = format_results(hits, query, output=output)
        render_ms = (time.monotonic() - _t_render) * 1000.0
        return _degradation_notices() + scope_note + rendered
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
                "limit": limit, "thread_id": thread_id,
                "content_type": content_type,
                "exclude_content_type": exclude_content_type, "since": since,
                "until": until, "tool_name": tool_name, "source": source,
                "types": types, "agents": agents, "path": path,
                "path_ops": path_ops, "commit": commit, "pr": pr, "repo": repo,
                "startswith": startswith, "sort": sort,
                "output": output, "match": match, "page": page,
                "surface": _served_by(),
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
        # Published last, so the tool's own ledger write is charged to the tool. An
        # enclosing surface subtracts this from its clock to name its overhead, and
        # a telemetry append it does not perform must not land in that difference.
        _publish_call_ms((time.monotonic() - started) * 1000.0)


READ_DESCRIPTION = """\
Read a thread from the archive, reconstructed from the event log. `thread_id` takes \
any of three ref shapes and resolves it for you: the archive's own ULID (what search \
results carry), a legacy integer id, or a provider session uuid.

`mode` picks the view. 'chat' = the readable conversation, user turns plus the \
assistant's text and reasoning with tool calls stripped — what was decided or \
concluded. 'user' (default) = only the user messages, the cheapest read of what a \
thread was about and what was wanted. 'last' = only the thread's final assistant \
text, how the session ended. 'ends' = the first and last `context_turns` turns. \
'full' = the whole transcript including every tool call (bulky; `tool_results=true` \
folds each tool's output under its call).

To open a search hit, pass its event id as `around_event`: you get that event's whole \
turn plus `context_turns` turns either side, with the step marked `match:<event_id>`. \
Reads are size-budgeted (~48k chars — lower it with `max_chars` for a cheap skim, and \
`limit` caps turns per chunk), so a long thread ends in a CHUNKED footer naming the \
exact next `offset`. That is pagination, not lost data: page with `offset` (negative \
counts from the end), or resume from a known event with `after_event`. `summary=` \
replaces the transcript: 'toc', 'short', 'indexed', or 'files' (the files this \
session touched).

Images and documents render as `[image image/png 48 KB — /path/to/blob]`; the path is \
a real local file, Read it to view.

`thread_help('read')` is the full manual."""


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
    results carry); an all-digit **legacy integer id** (a
    permanent alias — integer ids pasted in old conversations keep resolving);
    or a provider **session uuid** (the id a tool like claude-code / cursor /
    codex knows the conversation by — its ``source_id``, newest match wins).
    Any of the three can be passed straight through without looking the ULID up
    first.

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

    The sizing arguments: ``limit`` caps turns per chunk (default 200 — a safety
    cap, since the char budget usually bites first), ``max_chars`` is that budget
    (default ~48k; lower it, say 8000, for a cheap skim of a long thread — the read
    stops at a clean turn boundary once it is hit), and a negative ``offset``
    counts from the end, so ``-20`` is the last 20 turns.
    """
    # Log in a finally so a raising read still leaves its usage record —
    # a failed read is usage evidence too — with the latency it burned.
    started = time.monotonic()
    out: object = None
    context: dict = {}
    span: Any = None
    try:
        with _contention.in_flight() as span:
            context = _contention.sample() if _usage.enabled() else {}
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
                "surface": _served_by(),
            },
            duration_ms=(time.monotonic() - started) * 1000.0,
            chars=len(out) if isinstance(out, str) else None,
            failed=sys.exc_info()[0] is not None,
            context=context,
        )
        # Last, for the same reason as in thread_search: the tool owns the cost of
        # recording itself.
        _publish_call_ms((time.monotonic() - started) * 1000.0)


HELP_DESCRIPTION = """\
The full manual for a retrieval tool, on demand: every filter and view with its \
grammar, the code axis, and worked recipes — the detail thread_search and \
thread_read's own descriptions leave out. `topic` is 'search' or 'read'."""

#: The manual for each topic is the tool's own docstring — one long form, living
#: beside the code that answers it rather than copied into a document that drifts.
_HELP_TOPICS = {"search": thread_search, "read": thread_read}


def thread_help(topic: str) -> str:
    """The long-form manual for ``thread_search`` or ``thread_read``.

    The tools ship a compact contract (:data:`SEARCH_DESCRIPTION`,
    :data:`READ_DESCRIPTION`) because a description is charged to every session
    that lists the tools, most of which never call them. The detail an agent needs
    once it is actually working — a filter's grammar, the code axis, what a browse
    can do — is real, so it lives here instead of being cut: one call, paid by the
    caller that wants it.

    An unknown topic is answered rather than raised, like the tools' other
    out-of-contract arguments: the caller is a model, and an exception is a failed
    tool call it has to guess its way out of.
    """
    # 'search' and 'thread_search' are the same ask — an agent reading the tool
    # list has the qualified name in front of it.
    fn = _HELP_TOPICS.get((topic or "").strip().lower().removeprefix("thread_"))
    if fn is None:
        return (f"no manual for {topic!r} — topic is 'search' (thread_search) or "
                f"'read' (thread_read)")
    return inspect.cleandoc(fn.__doc__ or "")
