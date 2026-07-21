"""Search-quality measurement over the live archive — the scoring core.

This is the reusable engine behind two callers: the ``archive eval`` CLI
command (the shipped, user-facing checkup — "is search working on *my* data")
and the dev bench under ``evals/`` (the full quality ladder — the CI gate, the
experiment runner, the LLM judges). Both build eval *cases* under one of a few
protocols and score a search function against them with the same MRR / recall@k
loop, so the number the CI gate defends and the number a user sees on their own
archive come off the same code path.

Case protocols:

``sample_title_cases`` is the zero-curation proxy: sample titled conversation
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
curation. Pairing rules: a read labels the most recent prior search in its
session; reads of threads the agent had already opened before searching don't
count; the originating session is skipped during ranking (it quotes the query
verbatim). A click is the pick from what past search surfaced, not a
corpus-wide judgment — golds are incumbent-shaped, so a modest movement is
divergence from the old ranking's shape as much as regression. What the bias
can't fake: the golds are relevant threads, so a *collapse* against them means
something real broke. Treat it as a collapse alarm. Needs an accumulated trail,
so it only becomes meaningful after search has been used for a while.

``load_case_file`` reads a JSONL file of ``{"query", "gold": [ids]}`` rows
(optional ``"sessions"``: thread ids to skip while ranking; optional ``"until"``:
a corpus snapshot date the golds were mined under) — the hook for hand-curated
or agent-mined query sets.

``behavior_report`` runs no ranking at all: per search, did the agent click a
result, reformulate, or abandon? Zero-label behavioral proxies, not judgments;
their value is the trend.

The scorer, :func:`evaluate`, reports MRR and recall@1/5/10/20 at thread-level
relevance, overall and per query-shape. Read-only against the archive; the
caller opens it (``_api.open_archive``) first.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

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
        current: tuple[str, set[int]] | None = None

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
    event stream :func:`_trail_events` produces.

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

    ``after`` (ISO date/datetime) keeps only trail events that occurred at or
    after it — the time-based holdout: cases mined strictly after a ranking
    change shipped carry less of the old incumbent's shape.
    """
    sql = (
        "SELECT thread_id, payload FROM events "
        "WHERE event_type = 'tool_use_complete' "
        "AND (payload LIKE '%thread_search%' OR payload LIKE '%thread_read%') "
    )
    params: dict[str, str] = {}
    if after:
        sql += "AND occurred_at >= :after "
        params["after"] = after
    sql += "ORDER BY thread_id, id"
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
    """Real search->read pairs from the archive's own tool-use trail."""
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
        # Agent-mined cases (evals/retrieval_mine_gold.py) carry the corpus
        # snapshot date the golds were mined under; the scoring search honors
        # it so post-mining threads can't perturb the case's ranking.
        if row.get("until"):
            case["until"] = row["until"]
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


def evaluate(cases: list[dict], *, limit: int, rerank, content_type,
             exclude_content_types: list[str] | None, search=None) -> dict:
    """Score ``cases`` against a search function — MRR, recall@k, per-shape MRR and
    latency. ``search`` is the ranker under evaluation (default: the archive's own),
    so a candidate ranking can be measured against the same cases as the incumbent."""
    if search is None:
        search = api.search
    per_shape: dict[str, list[float]] = {}
    reciprocal_ranks: list[float] = []
    hits_at: dict[int, int] = {k: 0 for k in RECALL_KS}
    latencies: list[float] = []

    for case in cases:
        gold = set(case["gold"])
        skip = set(case.get("sessions", []))
        # A case mined under a corpus snapshot (see load_case_file) is scored
        # under it too; the kwarg is omitted otherwise so experiment SEARCH
        # callables that predate it stay compatible.
        extra = {"until": case["until"]} if case.get("until") else {}
        t0 = time.monotonic()
        hits = search(
            case["query"],
            limit=limit + len(skip),
            content_types=[content_type] if content_type else None,
            exclude_content_types=exclude_content_types,
            rerank=rerank,
            **extra,
        )
        latencies.append(time.monotonic() - t0)

        rank = 0  # 0 = not found within limit
        pos = 0
        for h in hits:
            if h["thread_id"] in skip:
                continue
            pos += 1
            if pos > limit:
                break
            if h["thread_id"] in gold:
                rank = pos
                break
        rr = 1.0 / rank if rank else 0.0
        reciprocal_ranks.append(rr)
        per_shape.setdefault(query_shape(case["query"]), []).append(rr)
        for k in RECALL_KS:
            if rank and rank <= k:
                hits_at[k] += 1

    n = len(cases)
    return {
        "n": n,
        "mrr": sum(reciprocal_ranks) / n if n else 0.0,
        "recall": {k: hits_at[k] / n if n else 0.0 for k in RECALL_KS},
        "per_shape": {
            shape: {"n": len(rrs), "mrr": sum(rrs) / len(rrs)}
            for shape, rrs in sorted(per_shape.items())
        },
        "latency_p50_ms": sorted(latencies)[n // 2] * 1000 if n else 0.0,
    }
