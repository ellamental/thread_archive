"""The retrieval-usage ledger: ``<home>/retrieval-usage.jsonl``.

Retrieval quality has exactly one honest ground truth: what agents actually
search for and which results they go on to read. This ledger captures that
real task — every ``thread_search`` and ``thread_read`` the tools serve, over
MCP or from the CLI verbs — so evals (and the knowledge-layer verdict) can be
built from observed behaviour instead of intuition: it is the sampling frame of
real query shapes, and the population ``latency_replay.py`` measures speed over.
A read joins to the searches before it by thread id. A ``surface`` field names
the front door a call came through; a row without one is *unattributed* rather
than any particular door (see :data:`UNATTRIBUTED`).

Records hold query text, filter parameters, result *ids*, and the call's
wall-clock latency (``duration_ms``) — never event content, snippets, or
transcripts — so a leaked ledger names conversations without quoting them.
Latency rides along because it is the one regression class result-quality evals
can't see: a search that returns the right hits ever slower looks perfect until
someone measures. The file lives beside the other home-root ledgers
(``capture-skips.jsonl``, ``validation-drift.jsonl``), outside ``truth/`` — it is
operational telemetry, not archive data, and no backup/verify path depends on it.

Four record kinds, distinguished by ``kind``: ``search`` and ``read`` for the two
tools, ``warm`` for one :func:`thread_archive._retrieval.warm_models` pass, and
``refresh`` for one background rebuild of the vector matrix or the corpus graph.
The warm row is here rather than in its own file because it is the other half of
the same latency story — the startup cost the model arms carry, recorded where it
is paid on purpose, against the cold flags that mark a request unlucky enough to
pay it inside the call. The refresh row is the third: work a process does *between*
requests that every request beside it pays for.

(A fifth, ``serve``, is written by a serving surface rather than by the engine —
what the front door cost around a tool call, see :func:`record_serve`.)

Append-only JSONL, advisory, fail-soft — a ledger write must never break the
retrieval call it describes. Recorded only on an install being developed on
(:mod:`.._ops.telemetry`); ``THREAD_ARCHIVE_USAGE_LOG`` overrides either way. At
``max_bytes()`` the file rotates to a stamped segment and a fresh one starts;
every segment is retained and every reader here walks all of them
(:mod:`.._ops.ledger`), so the eval population is the whole history rather than
whatever fit in the current file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .._config import resolve_paths
from .._ops import ledger as _ledger
from .._ops import telemetry as _telemetry

logger = logging.getLogger(__name__)

LEDGER_FILE = "retrieval-usage.jsonl"

#: What ``surface`` means when it is absent: nothing claimed the call. A row that
#: names no door is *unattributed* — a reader must not resolve it to one, because
#: any door that fails to stamp itself lands here alongside the rest.
#: Defined here rather than beside the code that stamps it because the ledger owns
#: its own field's vocabulary, and a reader should be able to learn it without
#: importing the tool surface (:mod:`.._tools`, ~400 ms of engine).
UNATTRIBUTED = "mcp"

#: Query text a bench or a smoke test left in the ledger rather than an agent asking
#: something. Every reader that computes a distribution over this file drops them:
#: they return in ~1 ms and there is no question they are the honest answer to, so
#: leaving them in pulls every percentile toward the trivial.
#:
#: Defined here for the same reason :data:`UNATTRIBUTED` is — the ledger owns the
#: vocabulary of its own fields, and one definition is what keeps its readers
#: agreeing: a copy per reader drifts into a different list each, and two reports
#: over one file then disagree about which rows were traffic. Exact text, never a
#: shape heuristic: a short query is not automatically a probe (agents really do
#: search ``p50``, ``EDS``, ``mps``), and a filter that guessed would silently drop
#: the real ones.
PROBE_QUERIES = ("x", "test", "warmup", "hello", "bogus")

_MAX_RESULT_IDS = 20  # per-search result ids retained — enough to judge rank quality


def max_bytes() -> int:
    """Size at which the ledger rotates to a new segment (32 MB by default).

    Read per call from ``THREAD_ARCHIVE_USAGE_MAX_BYTES``, like ``enabled()``
    beside it: a constant would answer once at import and ignore any later word
    on it.
    """
    return _ledger.env_max_bytes("THREAD_ARCHIVE_USAGE_MAX_BYTES", 32 * 1024 * 1024)


def enabled(home: Any = None) -> bool:
    """Whether this install records retrieval usage at all.

    Off unless the install is being developed on (:mod:`.._ops.telemetry`);
    ``THREAD_ARCHIVE_USAGE_LOG`` overrides in either direction. Public because the
    callers that *assemble* a record — the contention sample most of all — should
    not pay for one nothing will write, and because a reader of this ledger has to
    be able to tell an empty window from an install that writes nothing.
    """
    return _telemetry.recording("THREAD_ARCHIVE_USAGE_LOG", home)


def _append(record: dict) -> None:
    """Append one record, rotating first when the file is at cap. Fail-soft."""
    try:
        _ledger.append(resolve_paths().home / LEDGER_FILE, record, max_bytes=max_bytes())
    except Exception:  # noqa: BLE001 — telemetry must never break a retrieval call
        logger.warning("could not record retrieval usage", exc_info=True)


#: The recorded parameters a replay reproduces. Every one of these changes what a
#: search costs, so replaying a call without them measures a workload nobody ran —
#: a ``match='substring'`` ask replayed as bare text at the default
#: limit understates it by 12x on this corpus. ``page`` is here because a walk's
#: later pages are the expensive ones. Filters that only narrow (``thread_id``,
#: ``path``) are deliberately absent: they bind to ids that may no longer exist, and
#: a replay that raises measures nothing at all.
REPLAYED_PARAMS = (
    "limit", "page", "match", "since", "until", "source", "agents",
    "types", "sort", "startswith", "tool_name",
)


def read_calls(
    home: Any = None, *, limit: Optional[int] = None, exclude: tuple[str, ...] = (),
) -> list[tuple[str, dict]]:
    """The searches agents actually ran, newest first, as ``(query, kwargs)``.

    The observed distribution, which is a different population from any curated
    case file. A case file is built to be gradeable — a query with a knowable right
    answer — and that selection quietly excludes most of the shapes a change
    touches: over this ledger, time-scoped asks, browse walks, and the sentence
    punctuation an agent writes with are all common in traffic and near-absent from
    any curated set.

    Deduped on the whole call, not the query text: the same words asked at page 1
    and at page 30 are two workloads and the second is the expensive one, while a
    query an agent repeated verbatim while paging is one measurement rather than
    forty. Newest first so a ``limit`` takes the current distribution rather than an
    archaeological one; queries drift as the corpus and the tools do. ``exclude``
    drops queries by exact text, for the throwaway probes a bench or a smoke test
    leaves behind. Empty when the ledger is missing or unreadable — a replay with
    nothing to replay is not an error, it is a young archive."""
    path = (resolve_paths(home).home if home is None else home) / LEDGER_FILE
    seen: dict[tuple, dict] = {}
    # Across every retained segment, newest first — a rotation must not truncate
    # the observed distribution a replay is built from.
    for rec in _ledger.iter_rows(path, newest_first=True):
        if rec.get("kind") != "search":
            continue
        query = rec.get("query")
        if not isinstance(query, str) or not query.strip() or query in exclude:
            continue
        kwargs = {k: rec[k] for k in REPLAYED_PARAMS if rec.get(k) is not None}
        key = (query, tuple(sorted((k, str(v)) for k, v in kwargs.items())))
        seen.setdefault(key, {"query": query, "kwargs": kwargs})
        if limit is not None and len(seen) >= limit:
            break
    return [(c["query"], c["kwargs"]) for c in seen.values()]


def record_search(
    query: str,
    *,
    params: dict[str, Any],
    hits: object,
    duration_ms: Optional[float] = None,
    render_ms: Optional[float] = None,
    failed: bool = False,
    timings: Optional[dict[str, Any]] = None,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Record one ``thread_search`` call: the query, the non-default parameters,
    how many hits came back, the top result ids (``[event_id, thread_id]``
    pairs) for later join against reads, and the call's latency. ``hits`` is
    whatever the engine returned — result ids are extracted defensively, so a
    non-ranked output shape (count/linkable) records its parameters and count
    without ids.

    Latency comes in two numbers because they answer different questions.
    ``duration_ms`` is the retrieval work as the agent felt it;
    ``render_ms`` is the formatting that turns those hits into the text the
    agent reads. Their sum is the tool call's wall-clock, and keeping them apart is
    what distinguishes a slow *search* from a slow *answer* — a wide result set can
    make the second large while the first is unchanged. ``render_ms`` is absent on a
    search that never reached the render.

    ``timings`` is the optional per-stage breakdown of ``duration_ms`` (the engine's
    :class:`thread_archive._retrieval._probe.SearchProbe` record — the arm
    totals, the vector arm's sub-stages when it ran, ``pool_size``,
    and the cold/``matrix_built`` flags when they apply). Total latency alone can't
    see which stage regressed; this makes the ledger self-diagnosing — still ids and
    timings only, never content.

    A search that raised is recorded too — the caller passes ``failed`` — with the
    time it burned before it did: an error that takes a minute to arrive is latency
    evidence, and dropping it would bias every percentile computed off this file
    toward the searches that happened to succeed. Stated by the caller rather than
    inferred from a missing ``render_ms``, so a surface that legitimately records no
    render (anything serving hits as data) isn't read as a failure.

    ``context`` is the conditions the call ran under
    (:mod:`thread_archive._retrieval._contention`): concurrent calls, background
    rebuilds, how recently another process wrote the index, and how long the serving
    process had been alive. Timings say where a search spent its time; this says
    whether it had the machine to itself while spending it, and whether it had its
    caches yet — the difference between a slow pipeline, a busy box, and a cold
    start, which a duration alone cannot tell apart. Without the last of those no
    before/after over this file means anything: retrieval's caches are all
    process-local, restarts are frequent, and a comparison that cannot exclude a
    cold process is comparing cache states rather than code."""
    if not enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "search",
        "query": query,
    }
    record.update({k: v for k, v in params.items() if v is not None})
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 1)
    if render_ms is not None:
        record["render_ms"] = round(render_ms, 1)
    if failed:
        record["failed"] = True
    if timings:
        record.update(timings)
    if context:
        record.update(context)
    results: list[list[int | str]] = []
    if isinstance(hits, list):
        record["n_hits"] = len(hits)
        for hit in hits[:_MAX_RESULT_IDS]:
            if isinstance(hit, dict) and "event_id" in hit and "thread_id" in hit:
                try:
                    results.append([int(hit["event_id"]), str(hit["thread_id"])])
                except (TypeError, ValueError):
                    continue
    if results:
        record["results"] = results
    _append(record)


def record_read(
    thread_id: object,
    *,
    params: Optional[dict[str, Any]] = None,
    duration_ms: Optional[float] = None,
    chars: Optional[int] = None,
    failed: bool = False,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Record one ``thread_read`` call: the id as the caller passed it (thread
    id, legacy integer id, or provider session uuid — searches log thread ids,
    so joins work for the id-from-search path) plus the non-default view
    parameters and the read's latency.

    ``chars`` is the size of what came back. A read's cost tracks how much
    conversation it materialized far more than which thread it opened, so latency
    without size is a distribution with its main explanatory variable missing — the
    reason a median read is milliseconds and the worst is seconds is mostly that
    they are not the same amount of work. Recorded as the denominator that makes the
    two comparable.

    ``failed`` marks a read that raised, for the same reason searches record it: the
    slow failures are evidence, and dropping them flatters every percentile.
    ``context`` is the same contention sample searches carry — a read hydrates from
    the same store ingest is writing, and its tail (milliseconds at the median,
    seconds at the worst) is exactly where that would show."""
    if not enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "read",
        "thread_id": thread_id if isinstance(thread_id, int) else str(thread_id),
    }
    if params:
        record.update({k: v for k, v in params.items() if v not in (None, False, 0)})
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 1)
    if chars is not None:
        record["chars"] = chars
    if failed:
        record["failed"] = True
    if context:
        record.update(context)
    _append(record)


def record_serve(record: dict[str, Any]) -> None:
    """Record one ``serve`` row: what a serving surface cost around a tool call.

    The surface builds the row (it is the only party that knows what its own layer
    is made of) and this stamps it and appends, so the ledger keeps one writer and
    one rotation policy. A ``serve`` row is the outside of a call whose inside is
    the ``search`` or ``read`` row written microseconds earlier — the pair is what
    separates a slow pipeline from a slow front door, which no single number
    can."""
    if not enabled():
        return
    _append({"at": datetime.now(timezone.utc).isoformat(), **record})


def record_warm(
    *,
    duration_ms: float,
    stages: dict[str, float],
    failed: Optional[list[str]] = None,
    surface: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Record one :func:`thread_archive._retrieval.warm_models` pass — how long a
    process took to become useful, split by stage (``embed_ms``, ``matrix_ms``,
    ``graph_ms``, ``search_ms``, and ``wait_ms`` for the queue in front of them).

    ``duration_ms`` covers the wait as well as the work, because the question it
    answers is when the process started being useful and a queued process is not
    useful yet. ``wait_ms`` is what separates the two readings of a slow pass — work
    that got slower against a turn that came late — which want opposite fixes and are
    indistinguishable in a total.

    A ``warm`` row is the counterpart to the cold flags on a search: those say a
    request paid a load, this says what the load costs when it is paid where it
    should be. Together they answer the question neither can alone — whether a
    slow first search means warming is broken or merely that a query arrived
    before it finished. ``failed`` names the stages that raised; a warm pass is
    best-effort, so a partial one is normal and worth distinguishing from a
    complete one that was simply slow.

    ``surface`` names the process that paid it, on the same vocabulary the search
    rows use. These rows are the only count of process starts there is, and
    several daemons warm independently — without it a restart rate is a total over
    services that restart for unrelated reasons, and cannot be lined up with the
    latency of the one front door a reader is looking at.

    ``context`` is the same contention sample searches and reads carry. A stage
    total says how long the model took to load; it cannot say whether that number
    is the load or the machine, and the two want opposite fixes. The spread is not
    subtle — the same load measures seconds on a quiet box and over a minute beside
    a test suite — so without this a regression in the load and an afternoon of
    heavy traffic are the same row. ``wait_ms`` does not cover it: that separates
    work from *this* queue, and the competition worth naming here is mostly not
    other warm passes.

    It also carries ``uptime_s``, which is what joins a warm row to the searches of
    its own process — ``at - uptime_s`` is the process start, shared by every row
    that process writes. That join is the only way to ask whether a slow search ran
    before its own warm pass finished, which is a different fault from a slow
    search on a warmed process."""
    if not enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "warm",
        "duration_ms": round(duration_ms, 1),
    }
    if surface:
        record["surface"] = surface
    record.update({k: round(v, 1) for k, v in stages.items()})
    if failed:
        record["failed_stages"] = failed
    if context:
        record.update(context)
    _append(record)


def record_refresh(
    what: str,
    *,
    duration_ms: float,
    failed: bool = False,
    detail: Optional[dict[str, Any]] = None,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Record one background rebuild — the vector matrix (``what="matrix"``) or the
    corpus graph (``what="graph"``) — and what it cost.

    These are the two pieces of work in a serving process that are neither a
    request nor a startup, and until they are recorded they exist in this ledger
    only as somebody else's problem: a search that ran beside one carries
    ``refreshing`` in its contention sample, which names the rebuild and says
    nothing about it. How long it ran, how often it runs, and whether it is
    getting slower are all invisible from the field that reports it — so the one
    thing in the process most able to make a search slow is the one thing with no
    series of its own.

    ``uptime_s`` (from ``context``) is what makes the pair readable: a refresh row
    and the search rows around it share a process, so a rebuild's window can be
    laid over the searches it overlapped rather than inferred from a boolean on
    each of them.

    ``detail`` is whatever the rebuild can say about its own size — the row count
    it packed, the nodes and edges it built. A duration without it is the same
    trap ``chars`` exists to close on reads: the corpus grows, so a rebuild that
    costs more may be doing more, and only the size says which.

    Cheap to write (one row per rebuild, not per request) and fail-soft like every
    other writer here: a background thread's telemetry must never take a search's
    process down with it."""
    if not enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "refresh",
        "what": what,
        "duration_ms": round(duration_ms, 1),
    }
    if failed:
        record["failed"] = True
    if detail:
        record.update(detail)
    if context:
        record.update(context)
    _append(record)
