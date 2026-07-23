"""Search-quality measurement over the live archive — the scoring core.

This is the reusable engine behind two callers: the ``thread_archive eval`` CLI
command (the shipped, user-facing checkup — "is search working on *my* data")
and the dev bench under ``evals/`` (the full quality ladder — the CI gate, the
experiment runner, the LLM judges). Both build eval *cases* under one of a few
protocols and score a search function against them with the same MRR / success@k
/ recall@k / nDCG@k loop, so the number the CI gate defends and the number a
user sees on their own archive come off the same code path.

Case protocols:

``sample_title_cases`` is the zero-label proxy: sample titled conversation
threads, use each *title* as the query, and score whether the thread's own
content ranks. Thread-meta docs (title/summary) are excluded from the searched
scope so the eval never matches the query against itself. Cheap, stable, and
needs nothing but titled threads — so it works on day one of an import. But
titles are LLM distillations of the thread they name, so vocabulary overlap is
built in: read the numbers as "are my threads findable at all," a regression
ratchet, not a precision score.

``mine_log_cases`` scores against real usage: the archive's own tool-use trail
holds every ``thread_search`` call agents have made (the query) and the
``thread_read`` calls that followed in the same session (the click). Each
search paired with its subsequent reads is a relevance judgment the searcher
made at the moment of searching — real query vocabulary, multi-gold, no
labels. Only top-level conversation sessions count: subagent retrieval fleets
and collection sweeps (archived as ``system``) issue recall-intent queries —
"surface everything in vein X" — which have no single rankable target and whose
clicks are "read everything," so they're excluded (see :func:`_trail_events`).
Pairing rules: a read labels the most recent prior search in its
session; reads of threads the agent had already opened before searching don't
count; the originating session is skipped during ranking (it quotes the query
verbatim). A click is the pick from what past search surfaced, not a
corpus-wide judgment — golds are incumbent-shaped, so a modest movement is
divergence from the old ranking's shape as much as regression. What the bias
can't fake: the golds are relevant threads, so a *collapse* against them means
something real broke. Treat it as a collapse alarm. Needs an accumulated trail,
so it only becomes meaningful after search has been used for a while.

``load_case_file`` reads a JSONL file of ``{"query", "gold": [ids]}`` rows
(optional ``"grades"``: a ``thread id -> 0|1|2`` relevance pool — the ranked
candidate pool a graded metric scores against, not just the one best answer;
optional ``"sessions"``: thread ids to skip while ranking; optional
``"snapshot_id"``: the content fingerprint of the corpus snapshot the golds were
mined against) — the hook for hand-labeled or agent-mined query sets. The
``snapshot_id`` is the eval's staleness guard: ``retrieval_eval.py --cases``
runs over that same snapshot and refuses cases whose id no longer matches, so
golds can't be scored against a corpus that has changed under them.

``behavior_report`` runs no ranking at all: per search, did the agent click a
result, reformulate, or abandon? Zero-label behavioral proxies, not judgments;
their value is the trend.

The scorer, :func:`evaluate`, reports MRR, success@1/5/10/20 (did any grade-2
``gold`` thread rank by k?), true recall@1/5/10/20 (what fraction of every
case's grade-2 gold set ranked by k?), and nDCG@1/5/10/20 (graded, over the
``grades`` pool — so a ranking is rewarded for ordering grade-2 above grade-1
above grade-0, not just for surfacing one right answer; a case with no pool
falls back to binary relevance so the metric stays defined for the title and
log protocols). Reports overall and per query-shape. Read-only against the
archive; the caller opens it (``_api.open_archive``) first.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text as sa_text

from . import _api as api
from ._retrieval.read import resolve_thread_ref
from ._store import use_session

RECALL_KS = (1, 5, 10, 20)

# Meta docs are excluded from every title-protocol search: the query IS the
# title, so a self-match would saturate the metrics. The log protocol searches
# the production scope — its queries aren't derived from any document.
EXCLUDE_META = ["title", "summary"]

# Tool families whose thread ids live in this archive's id space: the archive's
# own MCP server and the legacy thread-commands server it superseded (the id
# numbering carried over). Bare unnamespaced `thread_search` tools exist in the
# trail too but belong to unrelated experiments — excluded by requiring the
# family prefix.
_TOOL_RE = re.compile(r"thread[-_](?:archive|commands)[_:]+thread_(search|read)$")


def classify_tool(name: str | None) -> str | None:
    """'search' / 'read' for a thread-archive-family tool name, else None."""
    m = _TOOL_RE.search(name or "")
    return m.group(1) if m else None


def sample_title_cases(n: int, seed: int) -> list[dict]:
    """Titled conversation threads with enough user content to be findable."""
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT t.id, t.title FROM threads t "
            "WHERE t.thread_type = 'conversation' AND NOT t.exclude_from_search "
            "AND t.title IS NOT NULL AND length(t.title) >= 12 "
            "AND (SELECT count(*) FROM events_fts f WHERE f.thread_id = t.id "
            "     AND f.content_type = 'user') >= 3"
        )).all()
    rows = list(rows)  # .all() types as an immutable Sequence; shuffle needs a MutableSequence
    random.Random(seed).shuffle(rows)
    return [{"query": title, "gold": [tid], "sessions": []} for tid, title in rows[:n]]


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
    behavioral proxies alike. This matches the corpus the ``eval`` CLI already
    counts (``thread_type = 'conversation'``).

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


def mine_log_cases(n: int, seed: int, after: str | None = None) -> list[dict]:
    """Real search->read pairs from the archive's own tool-use trail (top-level
    conversation sessions only — subagent fleets excluded, see
    :func:`_trail_events`)."""
    with use_session() as s:
        cases = pair_log_events(_trail_events(s, after))

        # Keep only golds that are live, searchable threads; merge duplicate
        # queries (same query issued in several sessions) into one multi-gold,
        # multi-session case.
        live = {
            tid for (tid,) in s.execute(sa_text(
                "SELECT id FROM threads WHERE NOT exclude_from_search"
            )).all()
        }
    merged: dict[str, dict] = {}
    for c in cases:
        gold = [t for t in c["gold"] if t in live]
        if not gold:
            continue
        m = merged.setdefault(c["query"], {"query": c["query"], "gold": set(),
                                           "sessions": set()})
        m["gold"].update(gold)
        m["sessions"].update(c["sessions"])
    final = [{"query": m["query"], "gold": sorted(m["gold"]),
              "sessions": sorted(m["sessions"])} for m in merged.values()]
    final.sort(key=lambda c: c["query"])
    random.Random(seed).shuffle(final)
    return final[:n]


def load_case_file(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case = {"query": row["query"], "gold": list(row["gold"]),
                "sessions": list(row.get("sessions", []))}
        # The graded candidate pool (thread id -> 0|1|2), when the case carries
        # one: nDCG scores the whole pool, not just the grade-2 gold. Keys kept
        # as strings to match the thread ids search returns; values coerced to
        # int so a stray float grade can't skew the gain.
        if row.get("grades"):
            case["grades"] = {str(t): int(g) for t, g in row["grades"].items()}
        # Agent-mined cases (the `thread_archive mine` miners) carry the content
        # fingerprint of the corpus snapshot they were mined against; the caller
        # (retrieval_eval.py --cases) refuses to score them against a home whose
        # snapshot_id differs, so a moved corpus invalidates rather than drifts.
        if row.get("snapshot_id"):
            case["snapshot_id"] = row["snapshot_id"]
        cases.append(case)
    return cases


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


def evaluate(cases: list[dict], *, limit: int, rerank, content_type,
             exclude_content_types: list[str] | None, search=None) -> dict:
    """Score ``cases`` against a search function — MRR, success@k, recall@k,
    nDCG@k, per-shape MRR, and latency. ``search`` is the ranker under evaluation
    (default: the archive's own), so a candidate ranking can be measured against
    the same cases as the incumbent. MRR and success use the first grade-2 hit;
    recall averages the fraction of each case's complete grade-2 ``gold`` set
    retrieved by k; nDCG is graded over the case's ``grades`` pool (a case without
    one falls back to binary relevance — its ``gold`` as grade 1 — so nDCG stays
    defined for every protocol).

    Determinism is the caller's job, not a per-case date bound: agent-mined cases
    are scored over the frozen snapshot they were mined against (the caller binds
    the home by ``snapshot_id``), so the corpus can't move underneath the ranking
    and the search runs in its native production shape and latency."""
    if search is None:
        search = api.search
    per_shape: dict[str, list[float]] = {}
    reciprocal_ranks: list[float] = []
    successes_at: dict[int, int] = {k: 0 for k in RECALL_KS}
    recall_at: dict[int, float] = {k: 0.0 for k in RECALL_KS}
    ndcg_at: dict[int, float] = {k: 0.0 for k in RECALL_KS}
    latencies: list[float] = []

    for case in cases:
        gold = set(case["gold"])
        skip = set(case.get("sessions", []))
        # The graded candidate pool; absent one (title/log protocols), the gold
        # stands in as binary relevance so nDCG is still defined and comparable.
        grades = case.get("grades") or {t: 1 for t in case["gold"]}
        pool_rels = [float(g) for g in grades.values()]
        t0 = time.monotonic()
        hits = search(
            case["query"],
            limit=limit + len(skip),
            content_types=[content_type] if content_type else None,
            exclude_content_types=exclude_content_types,
            rerank=rerank,
        )
        latencies.append(time.monotonic() - t0)

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

    n = len(cases)
    return {
        "n": n,
        "mrr": sum(reciprocal_ranks) / n if n else 0.0,
        "success": {k: successes_at[k] / n if n else 0.0 for k in RECALL_KS},
        "recall": {k: recall_at[k] / n if n else 0.0 for k in RECALL_KS},
        "ndcg": {k: ndcg_at[k] / n if n else 0.0 for k in RECALL_KS},
        "per_shape": {
            shape: {"n": len(rrs), "mrr": sum(rrs) / len(rrs)}
            for shape, rrs in sorted(per_shape.items())
        },
        "latency_p50_ms": sorted(latencies)[n // 2] * 1000 if n else 0.0,
    }
