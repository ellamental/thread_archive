"""Search-quality measurement — the scoring core and the trail diagnostics.

Two unrelated jobs share this module because they share a corpus and a session
handle, not because they compose.

**The scoring loop.** :func:`evaluate` runs one MRR / success@k / recall@k /
nDCG@k pass over cases a caller supplies as ``{"query", "gold": [thread ids]}``
rows. Exactly one caller supplies them: the **tier-0 synthetic corpus**
(``tests/quality_corpus.py``), whose labels are nonce terms planted in a
checked-in corpus and therefore true by construction. That is what keeps it clear
of the admission rule — and what bounds it. Tier 0 is near-saturated by design
(MRR ≈ 1.0), so it can only fall: a breakage detector, never an improvement
meter. **Nothing scores this archive's real corpus through here, and nothing
should** — see ``docs/search-quality.md`` → "The admission rule".

The external calibration harnesses (``beir_eval``, ``cdr_eval``,
``haystack_eval``, ``mtrag_eval``, ``perltqa_eval``) do **not** score through
here, and shouldn't: their job is to land beside a published leaderboard, so each
implements that leaderboard's own metric conventions — linear-gain nDCG, the
field's standard — rather than this archive's exponential gain. Two scoring
cores, on purpose, with the boundary at the corpus. What they do share is the
plumbing deciding *which stack* gets measured (``search_lab.eval_home``), plus
the sampling and per-query reporting below (:func:`sample_queries`,
:func:`query_row`, :func:`performance`), so a number's configuration means the
same thing on both sides even where its metric does not.

**The trail diagnostics.** :func:`behavior_report` runs no ranking at all: per
search in the tool-use trail, did the agent click a result, reformulate, or
abandon? Zero-label behavioral proxies, not judgments; their value is the trend.
Only top-level conversation sessions count — subagent retrieval fleets and
collection sweeps (archived as ``system``) issue recall-intent queries whose
"clicks" are *read everything*, which is not the same act (see
:func:`_trail_events`). A read attributes to the most recent prior search in its
session, and reads of threads the agent had already opened don't count.

Deliberately absent: any builder that turns this archive into graded cases. The
clicks :func:`pair_log_events` attributes are a record of what the incumbent
ranker surfaced, so scoring against them measures agreement with the ranker under
test. They feed the behavior rates and nothing else.

Measurement, not product: an install ships no scoring surface at all. Read-only
against the archive; the caller opens it (``_api.open_archive``) first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text as sa_text

from thread_archive import _api as api
from thread_archive._retrieval import _probe
from thread_archive._retrieval.read import resolve_thread_ref

logger = logging.getLogger(__name__)

RECALL_KS = (1, 5, 10, 20)

# Thread-meta docs. A case whose query was derived from a thread's own title or
# summary would match itself, so a caller scoring such cases passes these as
# `exclude_content_types`; the tier-0 corpus does not need it.
EXCLUDE_META = ["title", "summary"]

# Tool families whose thread ids live in this archive's id space: the archive's
# own MCP server and the thread-commands server, which shares its id numbering.
# Bare unnamespaced `thread_search` tools exist in the
# trail too but belong to unrelated experiments — excluded by requiring the
# family prefix.
_TOOL_RE = re.compile(r"thread[-_](?:archive|commands)[_:]+thread_(search|read)$")


def classify_tool(name: str | None) -> str | None:
    """'search' / 'read' for a thread-archive-family tool name, else None."""
    m = _TOOL_RE.search(name or "")
    return m.group(1) if m else None


def resolve_read_refs(
    events: list[tuple[object, str, object]], resolve,
) -> list[tuple[object, str, object]]:
    """Canonicalize read refs to thread ids; drop reads that resolve to nothing.

    The trail holds whatever ref the agent passed to ``thread_read`` — a legacy
    integer id, the ULID primary key, or a provider session id. ``resolve``
    maps a ref to the canonical thread id (None = unresolvable). Search events
    pass through untouched. Pure — the caller supplies the DB-backed resolver.
    """
    out: list[tuple[object, str, object]] = []
    for sess, kind, value in events:
        if kind == "read":
            value = resolve(value)
            if value is None:
                continue
        out.append((sess, kind, value))
    return out


def pair_log_events(events: list[tuple[object, str, object]]) -> list[dict]:
    """Pair search calls with the reads that followed them.

    ``events``: (session_thread_id, kind, value) in occurrence order, where
    kind is 'search' (value: query string) or 'read' (value: canonical thread
    id — refs already resolved, e.g. via :func:`resolve_read_refs`). Pure — DB
    filtering (gold existence, exclude_from_search) happens in the caller.
    """
    per_session: dict[object, list[tuple[str, object]]] = {}
    for sess, kind, value in events:
        per_session.setdefault(sess, []).append((kind, value))

    cases: list[dict] = []
    for sess, evs in per_session.items():
        seen_reads: set[object] = set()
        # gold ids are opaque and heterogeneous — legacy int ids in unit cases,
        # ULID strings in production — homogeneous within a single case.
        current: tuple[Any, set[Any]] | None = None

        def flush() -> None:
            if current and current[1]:
                cases.append({"query": current[0], "gold": sorted(current[1]),
                              "sessions": [sess]})

        for kind, value in evs:
            if kind == "search":
                flush()
                current = (value, set())
            else:
                tid = value
                if current is not None and tid != sess and tid not in seen_reads:
                    current[1].add(tid)
                seen_reads.add(tid)
        flush()
    return cases


def behavior_report(events: list[tuple[object, str, object]]) -> dict:
    """Zero-label quality signals from the trail: how searches actually end.

    Per search, the outcome is ``clicked`` (a later read in the session was
    attributed to it), ``reformulated`` (no click, and another search followed
    in the same session — the agent tried again), or ``abandoned`` (no click
    and the session's trail ends there — the agent gave up or went elsewhere).
    Attribution follows the pairing rules: self-session reads and threads the
    agent had already opened don't count as clicks. Pure — takes the resolved
    event stream :func:`_trail_events` produces (conversation sessions only, so
    programmatic subagent-fleet behavior can't skew the rates).

    These are behavioral proxies, not judgments: a click isn't satisfaction
    and an abandonment isn't always failure (the answer may have been in the
    search snippets themselves). Their value is the trend — a ranking change
    that moves click-through or abandonment moved something real.
    """
    per_session: dict[object, list[tuple[str, object]]] = {}
    for sess, kind, value in events:
        per_session.setdefault(sess, []).append((kind, value))

    outcomes = {"clicked": 0, "reformulated": 0, "abandoned": 0}
    clicked_read_counts: list[int] = []
    sessions_with_search = 0
    for sess, evs in per_session.items():
        seen: set[object] = set()
        pending: int | None = None  # click count of the currently open search
        had_search = False
        for kind, value in evs:
            if kind == "search":
                had_search = True
                if pending is not None:
                    outcomes["clicked" if pending else "reformulated"] += 1
                    if pending:
                        clicked_read_counts.append(pending)
                pending = 0
            else:
                if pending is not None and value != sess and value not in seen:
                    pending += 1
                seen.add(value)
        if pending is not None:
            outcomes["clicked" if pending else "abandoned"] += 1
            if pending:
                clicked_read_counts.append(pending)
        if had_search:
            sessions_with_search += 1

    n = sum(outcomes.values())
    return {
        "n_searches": n,
        "n_sessions": sessions_with_search,
        **outcomes,
        "click_rate": outcomes["clicked"] / n if n else 0.0,
        "reformulation_rate": outcomes["reformulated"] / n if n else 0.0,
        "abandonment_rate": outcomes["abandoned"] / n if n else 0.0,
        "reads_per_click": (
            sum(clicked_read_counts) / len(clicked_read_counts)
            if clicked_read_counts else 0.0
        ),
    }


def _trail_events(s, after: str | None = None) -> list[tuple[object, str, object]]:
    """(session, kind, value) events from the tool-use trail, refs resolved.

    Scoped to ``thread_type='conversation'`` sessions — top-level agent/operator
    work, where a search has a specific target the ranking can be judged against.
    Subagent sessions (the retrieval fleets and collection sweeps, archived as
    ``system``) are excluded: their searches are recall-intent ("surface
    everything in vein X"), which has no single rankable gold, and their clicks
    are "open everything to collect it" — both poison for a ranking eval and the
    behavioral proxies alike.

    ``after`` (ISO date/datetime) keeps only trail events that occurred at or
    after it — the time-based holdout: cases mined strictly after a ranking
    change shipped carry less of the old incumbent's shape.
    """
    sql = (
        "SELECT e.thread_id, e.payload FROM events e "
        "JOIN threads t ON t.id = e.thread_id "
        "WHERE e.event_type = 'tool_use_complete' "
        "AND (e.payload LIKE '%thread_search%' OR e.payload LIKE '%thread_read%') "
        "AND t.thread_type = 'conversation' "
    )
    params: dict[str, str] = {}
    if after:
        sql += "AND e.occurred_at >= :after "
        params["after"] = after
    sql += "ORDER BY e.thread_id, e.id"
    rows = s.execute(sa_text(sql), params).all()

    events: list[tuple[object, str, object]] = []
    for sess, payload in rows:
        p = payload if isinstance(payload, dict) else json.loads(payload)
        kind = classify_tool(p.get("tool_name"))
        inp = p.get("input") or {}
        if kind == "search":
            q = inp.get("query")
            if isinstance(q, str) and q.strip():
                events.append((sess, "search", q.strip()))
        elif kind == "read":
            ref = inp.get("thread_id")
            if isinstance(ref, (int, str)) and str(ref).strip():
                events.append((sess, "read", ref))

    memo: dict[str, str | None] = {}

    def _resolve(ref: object) -> str | None:
        key = str(ref).strip()
        if key not in memo:
            memo[key] = resolve_thread_ref(s, key)
        return memo[key]

    return resolve_read_refs(events, _resolve)


#: What a harness prints where the published-baseline verdict would go when the
#: run is not the measurement the baseline describes.
NOT_COMPARABLE = ("  NOT COMPARABLE — the published number is scored over the whole "
                  "query set and corpus; this run is a subset. Reference shown for "
                  "scale only.")


def comparable_to_published(*, sample: Optional[int] = None,
                            max_docs: Optional[int] = None) -> bool:
    """Whether this run may be scored *against* its published baseline, rather
    than merely printed beside it.

    A published nDCG@10 describes one measurement: every query, over the whole
    corpus. Narrow either and the number is still useful — it is the same ranker
    on the same data — but the comparison is not, and a verdict is worse than no
    verdict because it reads as a finding. A ``BELOW BM25 — investigate`` earned on
    300 of 1,583 sampled queries sends someone after a regression that is sampling
    error, and the quick tier exists precisely to be run often.

    The sampled row still keeps its own history: the ledger records it under its
    own name, so it is compared against *itself* across passes, which is what a
    fast tier is for."""
    return not sample and not max_docs


def sample_queries(items: list, n: Optional[int], key) -> list:
    """A deterministic subset of ``n`` items, or all of them.

    The bench runs at two depths — every query for a release, a sample for a
    quick check — and the sample has to hold three properties or the fast tier is
    worse than no tier at all.

    **Deterministic.** Two runs of identical code over identical data must score
    identically, or the whole freshness-and-delta machinery reports noise as
    movement. So the choice is a hash of each item's own id, not a PRNG draw and
    not a shuffle: no seed to thread through, no dependence on iteration order.

    **Unbiased.** The obvious implementation — take the first ``n`` — is the wrong
    one everywhere it matters here, because none of these query files are in
    random order. MTRAG's are grouped by domain, PerLTQA's by person and then by
    memory type, BEAM's by memory-ability category. Head-``n`` on any of them
    samples one stratum and reports it as the corpus. Hashing the id spreads the
    draw across whatever the ordering happened to encode.

    **Nested.** Ranking by hash means the sample at ``n`` is a subset of the
    sample at any larger ``n`` — so widening a quick check adds queries rather
    than swapping them, and a row's history stays readable across a change of
    depth.

    ``key`` extracts an item's stable id. It must be the *dataset's* id rather
    than a position, or the draw moves whenever the file does.

    What this cannot fix is resolution: on ``n`` sampled queries a single query
    moving from rank 1 to unfound shifts any of these metrics by at most ``1/n``,
    so a quick tier at n=100 cannot read a delta finer than 0.01. That is a floor
    on what the fast tier may be used to claim, not a reason to distrust it."""
    if not n or n >= len(items):
        return list(items)
    ranked = sorted(
        items,
        key=lambda item: hashlib.sha256(str(key(item)).encode("utf-8")).hexdigest())
    # Back into the caller's original order: the sample is *which* queries, never
    # what sequence they run in, and a hash-ordered loop would report per-query
    # detail in an order matching nothing in the source file.
    chosen = {id(item) for item in ranked[:n]}
    return [item for item in items if id(item) in chosen]


def query_shape(q: str) -> str:
    if "|" in q:
        return "pipe-or"
    if any(c in q for c in ('"',)) or any(w in q.split() for w in ("AND", "OR", "NOT")):
        return "boolean/phrase"
    if "_" in q or "::" in q or any("." in w and not w.endswith(".") for w in q.split()):
        return "code"
    return "natural" if len(q.split()) >= 2 else "single-term"


def _dcg(rels: list[float]) -> float:
    """Discounted cumulative gain with the standard exponential gain
    ``2**rel - 1`` and a log2 position discount (rank i, 1-based, discounted by
    ``log2(i + 1)``). A grade-0 doc contributes nothing, so returned
    non-relevant docs matter only through the positions they push relevant docs
    down to."""
    return sum((2.0 ** r - 1.0) / math.log2(i + 2) for i, r in enumerate(rels))


def ndcg_at_k(ranked_rels: list[float], pool_rels: list[float], k: int) -> float:
    """nDCG@k: the ranking's DCG over the ideal DCG (the pool's grades sorted
    best-first). ``ranked_rels`` is the relevance of the returned docs in rank
    order; ``pool_rels`` is every graded relevance in the case's candidate pool.
    0.0 when the pool holds nothing relevant (ideal DCG is 0)."""
    ideal = _dcg(sorted(pool_rels, reverse=True)[:k])
    return _dcg(ranked_rels[:k]) / ideal if ideal else 0.0


@dataclass
class EvalProgress:
    """What an :func:`evaluate` run knows partway through — the state an
    ``early_stop`` predicate decides on.

    ``sums`` holds running totals under the headline metric names (``mrr``,
    ``success10``, ``recall10``, ``ndcg10``), each a per-case value in
    [0, 1] summed over the cases scored so far. That shape is what makes a *sound*
    early exit possible: every remaining case can contribute at most 1.0, so
    ``(sums[m] + (n - scored)) / n`` is the best final value still reachable, and
    a predicate comparing it to a threshold aborts only runs that could not have
    reached it. ``case_rr`` is the reciprocal rank of the case just scored — 0.0
    meaning its gold never surfaced — for predicates that watch individual
    failures rather than the aggregate.
    """

    n: int
    scored: int
    sums: dict[str, float]
    query: str
    case_rr: float

    def best_possible(self, metric: str) -> float:
        """The highest final value ``metric`` can still reach if every unscored
        case is perfect. Monotonically non-increasing as a run proceeds, so a
        floor it has already fallen under can never be met."""
        if not self.n:
            return 0.0
        return (self.sums[metric] + (self.n - self.scored)) / self.n


def warm_for_scoring() -> None:
    """Build the corpus graph inline, before any case is scored.

    The community-coherence re-rank reads a cached graph and returns ``None``
    while a background build is still running, so on the request path a cold
    process simply ranks without the boost for its first queries. Under a scoring
    loop that same behaviour is a race against the scorer: the build lands partway
    through, the cases before it are ranked without coherence and the cases after
    it with, and where the boundary falls depends on wall-clock — how fast the box
    is, whether the pools came from a cache. Two runs of identical code then
    disagree (~0.02 window fill on a file, concentrated in whatever ran first),
    and every floor calibrated from them inherits the split.

    Building inline first costs one build and makes a run a function of the code
    and the snapshot alone. Fail-soft, and a no-op when coherence is off or the
    graph can't be built — both leave every case ranked the same way, which is the
    property that matters. Idempotent: the graph is cached, so the scoring loops
    that call this per file pay for it once."""
    from thread_archive._retrieval import embed_graph

    if embed_graph.coherence_gamma() <= 0.0:
        return
    try:
        embed_graph.get(block=True)
    except Exception:  # noqa: BLE001 — scoring without it beats not scoring
        logger.debug("warm_for_scoring: corpus graph unavailable", exc_info=True)


def evaluate(cases: list[dict], *, limit: int, content_type,
             exclude_content_types: list[str] | None, search=None,
             early_stop=None) -> dict:
    """Score ``cases`` against a search function — MRR, success@k, recall@k,
    nDCG@k, per-shape MRR, per-difficulty-tier metrics (for cases that carry a
    ``difficulty``), and latency. ``search`` is the ranker under evaluation
    (default: the archive's own), so a candidate ranking can be measured against
    the same cases as the incumbent. MRR and success use the first grade-2 hit;
    recall averages the fraction of each case's complete grade-2 ``gold`` set
    retrieved by k; nDCG is graded over the case's ``grades`` pool (a case without
    one falls back to binary relevance — its ``gold`` as grade 1 — so nDCG stays
    defined for every protocol).

    ``early_stop`` is an optional predicate taking an :class:`EvalProgress` after
    each case and returning an abort reason (or ``None`` to continue) — the seam
    that lets a caller stop paying for a run whose verdict is already decided.
    Every average still divides by the **full** case count, so an aborted report's
    metrics are lower bounds (the unscored cases counted as zero) rather than
    averages over a prefix, which would read as ordinary numbers while being
    scored on different cases. The report carries ``aborted`` (the reason, or
    ``None``) and ``scored`` so a caller can tell the two apart.

    Binding the corpus is the caller's job: agent-mined cases are scored over the
    frozen snapshot they were mined against (the caller binds the home by
    ``snapshot_id``), so the corpus can't move underneath the ranking and the
    search runs in its native production shape and latency. Holding the *ranker*
    still is this function's job, via :func:`warm_for_scoring` below."""
    warm_for_scoring()
    if search is None:
        search = api.search
    per_shape: dict[str, list[float]] = {}
    # Per-difficulty-tier accumulators, populated only for cases that carry a
    # ``difficulty`` (a case file's query-difficulty ladder). Each tier keeps the same
    # four headline signals, so a stratum can be reported apart from the aggregate
    # that would otherwise mask the weakest one.
    per_difficulty: dict[str, dict[str, float]] = {}
    reciprocal_ranks: list[float] = []
    successes_at: dict[int, int] = {k: 0 for k in RECALL_KS}
    recall_at: dict[int, float] = {k: 0.0 for k in RECALL_KS}
    ndcg_at: dict[int, float] = {k: 0.0 for k in RECALL_KS}
    latencies: list[float] = []
    stage_samples: list[dict] = []
    per_case: list[dict] = []
    aborted: str | None = None

    for case in cases:
        gold = set(case["gold"])
        skip = set(case.get("sessions", []))
        # The graded candidate pool; absent one (title/log protocols), the gold
        # stands in as binary relevance so nDCG is still defined and comparable.
        grades = case.get("grades") or {t: 1 for t in case["gold"]}
        pool_rels = [float(g) for g in grades.values()]
        # The stage breakdown rides along for free: these are the same searches a
        # latency run would pay for again, so a quality pass that recorded only a
        # total would throw away the profile it already produced. The probe is a
        # context-local slot and a search nobody measures never checks it, so
        # installing one here costs the eval nothing it wasn't already spending.
        with _probe.install() as probe:
            t0 = time.monotonic()
            hits = search(
                case["query"],
                limit=limit + len(skip),
                content_types=[content_type] if content_type else None,
                exclude_content_types=exclude_content_types,
            )
            elapsed = time.monotonic() - t0
        latencies.append(elapsed)
        if probe.ran:
            stage_sample = probe.as_record()
            stage_sample["total_ms"] = elapsed * 1000.0
            stage_samples.append(stage_sample)

        rank = 0  # 0 = not found within limit
        gold_positions: dict[object, int] = {}
        pos = 0
        ranked_rels: list[float] = []  # relevance of each returned doc, in rank order
        for h in hits:
            tid = h["thread_id"]
            if tid in skip:
                continue
            pos += 1
            if pos > limit:
                break
            ranked_rels.append(float(grades.get(tid, 0)))
            if rank == 0 and tid in gold:
                rank = pos
            if tid in gold and tid not in gold_positions:
                gold_positions[tid] = pos
        rr = 1.0 / rank if rank else 0.0
        reciprocal_ranks.append(rr)
        per_shape.setdefault(query_shape(case["query"]), []).append(rr)
        for k in RECALL_KS:
            found = sum(position <= k for position in gold_positions.values())
            if found:
                successes_at[k] += 1
            recall_at[k] += found / len(gold) if gold else 0.0
            ndcg_at[k] += ndcg_at_k(ranked_rels, pool_rels, k)
        difficulty = case.get("difficulty")
        if difficulty is not None:
            bucket = per_difficulty.setdefault(
                difficulty, {"n": 0, "mrr": 0.0, "success10": 0.0,
                             "recall10": 0.0, "ndcg10": 0.0})
            found10 = sum(position <= 10 for position in gold_positions.values())
            bucket["n"] += 1
            bucket["mrr"] += rr
            bucket["success10"] += 1.0 if found10 else 0.0
            bucket["recall10"] += found10 / len(gold) if gold else 0.0
            bucket["ndcg10"] += ndcg_at_k(ranked_rels, pool_rels, 10)
        case_row = {"query": case["query"], "rr": round(rr, 4),
                    "latency_ms": round(elapsed * 1000.0, 1)}
        if difficulty is not None:
            case_row["difficulty"] = difficulty
        per_case.append(case_row)

        if early_stop is not None:
            aborted = early_stop(EvalProgress(
                n=len(cases), scored=len(reciprocal_ranks),
                sums={
                    "mrr": sum(reciprocal_ranks),
                    "success10": float(successes_at[10]),
                    "recall10": recall_at[10],
                    "ndcg10": ndcg_at[10],
                },
                query=case["query"], case_rr=rr,
            ))
            if aborted:
                break

    n = len(cases)
    return {
        "n": n,
        "scored": len(reciprocal_ranks),
        "aborted": aborted,
        "mrr": sum(reciprocal_ranks) / n if n else 0.0,
        "success": {k: successes_at[k] / n if n else 0.0 for k in RECALL_KS},
        "recall": {k: recall_at[k] / n if n else 0.0 for k in RECALL_KS},
        "ndcg": {k: ndcg_at[k] / n if n else 0.0 for k in RECALL_KS},
        "per_shape": {
            shape: {"n": len(rrs), "mrr": sum(rrs) / len(rrs)}
            for shape, rrs in sorted(per_shape.items())
        },
        "per_difficulty": {
            tier: {
                "n": b["n"],
                "mrr": b["mrr"] / b["n"],
                "success10": b["success10"] / b["n"],
                "recall10": b["recall10"] / b["n"],
                "ndcg10": b["ndcg10"] / b["n"],
            }
            for tier, b in sorted(per_difficulty.items())
        },
        "per_case": per_case,
        "latency_p50_ms": (
            sorted(latencies)[len(latencies) // 2] * 1000 if latencies else 0.0
        ),
        "latency": latency_profile(latencies, stage_samples),
    }


def percentiles(values: list[float]) -> dict[str, float]:
    """``{p50, p95, p99}`` over ``values``, nearest-rank. Empty is all zeros.

    Nearest-rank rather than interpolated because a gold file is tens of cases:
    at n=75 the p99 is one sample either way, and interpolating between two of
    them invents a number that no search actually took."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    ordered = sorted(values)
    def at(q: float) -> float:
        idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
        return round(ordered[idx], 1)
    return {"p50": at(0.50), "p95": at(0.95), "p99": at(0.99)}


def latency_profile(latencies: list[float],
                    stage_samples: list[dict]) -> dict:
    """Where a scoring run's time went, from the probes installed around its own
    searches — the free half of a quality pass.

    A quality run executes exactly the workload a latency run would execute again,
    so recording only a median throws away a per-stage profile already paid for.
    This is not a substitute for ``speed.py``: that measures warm steady-state over
    ``query × rep`` with the pool cache suspended, and controls for the things a
    distribution needs controlled. This is one sample per case under whatever
    conditions the scoring run had, which makes it a *lead* — "the intent tier
    spends its time in the re-rank" — not a benchmark number.

    Stage times are durations, not shares: the two pool arms run concurrently, so
    ``fts_ms`` and ``semantic_ms`` cover overlapping wall-clock and can sum past
    the total. Only the shape stages sum. ``n`` is how many searches carried a
    breakdown, which is below the case count when a pool-cache hit sat the arms
    out — the cases that did no retrieval are excluded rather than averaged in as
    fast ones."""
    stages: dict[str, list[float]] = {}
    for sample in stage_samples:
        for name, value in sample.items():
            if name.endswith("_ms") and name != "total_ms":
                stages.setdefault(name, []).append(float(value))
    return {
        "n": len(stage_samples),
        "total": percentiles([v * 1000.0 for v in latencies]),
        "stages": {name: percentiles(vs)
                   for name, vs in sorted(stages.items()) if any(vs)},
        "cold": sum(1 for s in stage_samples if s.get("cold")),
        "pool_p50": (sorted(p := [s.get("pool_size", 0) for s in stage_samples])
                     [len(p) // 2] if stage_samples else 0),
    }


#: How much of a query's text a report keeps. Enough to recognise which query a
#: row is, and short enough that a conversational benchmark — whose "query" is a
#: whole multi-turn context — cannot make the per-query file the largest thing in
#: the archive home.
QUERY_TEXT_CAP = 240


def query_row(*, qid: Any, query: str, latency_s: float, rank: Optional[int],
              n_gold: int, found: int, measures: dict,
              group: Optional[str] = None) -> dict:
    """One scored query, in the shape every harness reports and the bench keeps.

    The aggregate says a row scores 0.494. This says *which* queries it failed
    and how badly, which is the only form of the result anybody can act on: an
    nDCG that moved 0.02 is a number, and the eleven queries that stopped
    retrieving their gold document at all are a bug with a shape.

    ``rank`` is the 1-based position of the first gold document in the returned
    ranking, or None when no gold document came back at all. That distinction is
    the whole reason to keep this: "the answer was ranked 40th" is a ranking
    problem and "the answer was never retrieved" is a recall one, they are fixed
    in different places, and a score of 0.0 reports them identically.

    ``group`` is whatever stratum the harness knows the query by — a LoCoMo
    category, a difficulty tier — so a failure can be read as belonging
    to a kind rather than as one bad query."""
    text = (query or "").strip().replace("\n", " ")
    row = {
        "qid": str(qid),
        "query": text[:QUERY_TEXT_CAP] + ("…" if len(text) > QUERY_TEXT_CAP else ""),
        "latency_ms": round(latency_s * 1000.0, 1),
        "rank": rank,
        "n_gold": n_gold,
        "found": found,
        "measures": {k: (round(v, 4) if isinstance(v, float) else v)
                     for k, v in measures.items()},
    }
    if group is not None:
        row["group"] = group
    return row


def performance(latencies: list[float], stage_samples: list[dict], *,
                scoring_s: float, corpus_docs: Optional[int] = None,
                arms: Optional[list[str]] = None) -> dict:
    """What a benchmark run *cost*, beside what it scored — the shape every
    harness's ``--json-out`` reports and the ledger keeps.

    A benchmark row already runs the exact workload a latency measurement would
    run again, so a report carrying only a median throws away a distribution and
    a per-stage profile that were already paid for. Once it is in the ledger, a
    tuning pass that lifts nDCG by 0.004 and doubles p99 is legible as the trade
    it is, rather than as a win.

    Read as a *lead*, not a benchmark: this is one sample per query under
    whatever conditions the scoring run had — a shared machine, a cold model on
    the first query, whatever else was running. ``speed.py`` is what controls for
    those. What this answers is "did the shape of the cost change between these
    two configurations", which no controlled run of only the newest one can.

    ``scoring_s`` is the scored loop alone. The runner's own ``elapsed_s`` covers
    the whole process, so the difference between them is what the row spent
    getting ready — an ingest, an embed pass, a model load — which is most of a
    cold row's wall-clock and none of its search cost."""
    profile = latency_profile(latencies, stage_samples)
    n = len(latencies)
    ms = [v * 1000.0 for v in latencies]
    return {
        "queries": n,
        "scoring_s": round(scoring_s, 1),
        # Queries per second over the scored loop — the throughput a caller would
        # actually see, so it includes whatever the loop did between searches.
        "qps": round(n / scoring_s, 2) if scoring_s > 0 and n else None,
        "mean_ms": round(sum(ms) / n, 1) if n else 0.0,
        "max_ms": round(max(ms), 1) if n else 0.0,
        "total": profile["total"],
        "stages": profile["stages"],
        # How many searches carried a stage breakdown. Below the query count when
        # a pool-cache hit sat the arms out, and the gap is itself the signal —
        # those searches did no retrieval rather than doing it instantly.
        "staged": profile["n"],
        "cold": profile["cold"],
        "pool_p50": profile["pool_p50"],
        "corpus_docs": corpus_docs,
        "arms": list(arms) if arms else None,
    }
