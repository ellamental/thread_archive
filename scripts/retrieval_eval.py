"""Retrieval eval harness — measure search quality so ranking changes are measurable.

Three case protocols, one scoring loop:

``--auto-titles N`` is a zero-curation proxy: sample N titled conversation
threads, use each *title* as the query, and score whether the thread's own
content ranks. Thread-meta docs (title/summary) are excluded from the searched
scope so the eval never matches the query against itself. Cheap and stable, but
titles are LLM distillations of the thread they name, so vocabulary overlap is
built in — treat the numbers as a regression ratchet, not real-world quality.

``--from-log N`` scores against real usage: the archive's own tool-use trail
holds every ``thread_search`` call agents have made (the query) and the
``thread_read`` calls that followed in the same session (the click). Each
search paired with its subsequent reads is a relevance judgment made by the
searcher at the moment of searching — real query vocabulary, multi-gold, no
curation. Pairing rules: a read labels the most recent prior search in its
session; reads of threads the agent had already opened before searching don't
count (it knew them without the search); the originating session is skipped
during ranking (it quotes the query verbatim). Read refs in the trail come in
every shape the read tool accepts — legacy integer ids, ULIDs, provider
session ids — and are canonicalized through the same resolver the tool uses;
reads that resolve to nothing are dropped. A click is the pick from what
past search surfaced, not a corpus-wide judgment: golds are incumbent-shaped,
so credit for surfacing relevant threads past search never reached is
invisible here, and a clicked thread wasn't necessarily satisfying (opened is
not answered). Every cross-stack comparison on these labels favors whatever
resembles the system that generated the log — including the comparison a
regression gate makes. The one reading the bias can't fake: the golds are
(mostly) relevant threads, so a *collapse* against them means something real
broke. Treat the metric as a collapse alarm — a modest drop under a
deliberately reshaped ranker may be divergence from the incumbent's shape,
not regression; no number from this protocol certifies improvement.

``--cases FILE`` evaluates a JSONL file of ``{"query": ..., "gold": [ids]}``
rows (optional ``"sessions"``: thread ids to skip while ranking) — the hook
for hand-curated or generated query sets. Mined and curated case files contain
real usage; keep them out of the repo.

``--behavior`` runs no ranking at all: it reports zero-label behavioral
signals from the whole trail — per search, did the agent click a result,
reformulate, or abandon? Proxies, not judgments; their value is the trend.

Reports MRR and recall@1/5/10/20 at thread-level relevance, overall and
per query-shape (so a lexical regression can't hide behind semantic wins).
``--mined-after`` restricts the log protocols to trail events after a date
(the time-based holdout); ``--trend-out`` appends any run's report as one
JSONL row, turning point measurements into a time series (the CI gate row
writes ~/.thread/archive/retrieval-trend.jsonl; LLM-judged relevance grades
from scripts/retrieval_judge.py land beside it).

Read-only. Run against the live archive:

    .venv/bin/python scripts/retrieval_eval.py --auto-titles 200
    .venv/bin/python scripts/retrieval_eval.py --from-log 500
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text as sa_text  # noqa: E402

from thread_archive import _api as api  # noqa: E402
from thread_archive._retrieval.read import resolve_thread_ref  # noqa: E402
from thread_archive._store import use_session  # noqa: E402

RECALL_KS = (1, 5, 10, 20)

# Meta docs are excluded from every --auto-titles search: the query IS the
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
        # Agent-mined cases (scripts/retrieval_mine_gold.py) carry the corpus
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


# The rerank liveness pair: a query, its answer, and a decoy no working
# cross-encoder confuses with it. Deliberately trivial — the probe asserts the
# model loads and discriminates at all, not that it ranks well (BEIR measures
# that). Failing this pair means the rerank arm is dead or scrambled.
PROBE_QUERY = "what is the largest animal on earth"
PROBE_ANSWER = "The blue whale is the largest animal known to have ever existed."
PROBE_DECOY = "Set the compiler's optimization flags before an incremental build."


def rerank_probe(reranker) -> str | None:
    """Prove the cross-encoder arm is alive: load the real model and score one
    trivial pair. Returns a breach message, or None when the arm works.

    ``is_available()`` alone can't carry this check — it is deliberately cheap
    (never loads the model), so a corrupt model file or a broken torch install
    still reports available and only degrades at call time, exactly the silent
    production failure this probe exists to catch."""
    if not reranker.is_available():
        return ("rerank arm unavailable (switched off, [embeddings] extra "
                "absent, or a prior load failed)")
    scores = reranker.rerank_scores(PROBE_QUERY, [PROBE_ANSWER, PROBE_DECOY])
    if scores is None:
        return "rerank arm degraded at scoring time (model failed to load or predict)"
    if len(scores) != 2 or not all(math.isfinite(s) for s in scores):
        return f"rerank arm returned malformed scores: {scores!r}"
    if scores[0] <= scores[1]:
        return (f"rerank arm cannot discriminate the liveness pair "
                f"(answer {scores[0]:.3f} <= decoy {scores[1]:.3f}) — "
                f"model {reranker.name!r} is loaded but scrambled")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    proto = ap.add_mutually_exclusive_group(required=True)
    proto.add_argument("--auto-titles", type=int, metavar="N",
                       help="sample N thread titles as queries (proxy protocol)")
    proto.add_argument("--from-log", type=int, metavar="N",
                       help="mine up to N real search->read cases from the "
                       "archive's own tool-use trail (click protocol)")
    proto.add_argument("--cases", type=Path, metavar="FILE",
                       help="evaluate a JSONL case file: "
                       '{"query", "gold": [thread ids], "sessions": [...]}')
    proto.add_argument("--behavior", action="store_true",
                       help="no ranking run at all: report zero-label "
                       "behavioral signals from the whole trail — click rate, "
                       "reformulation rate, abandonment rate per search")
    ap.add_argument("--mined-after", metavar="ISO", default=None,
                    help="--from-log/--behavior: only trail events at or after "
                    "this date — the time-based holdout (cases mined after a "
                    "ranking change shipped carry less of the old incumbent's "
                    "shape)")
    ap.add_argument("--trend-out", type=Path, metavar="FILE", default=None,
                    help="append this run's report as one JSONL row (~ ok) — "
                    "the time series that turns a snapshot number into a "
                    "trend; the CI gate row points it at "
                    "~/.thread/archive/retrieval-trend.jsonl")
    ap.add_argument("--seed", type=int, default=7, help="case sampling seed")
    ap.add_argument("--limit", type=int, default=20, help="results per query (recall ceiling)")
    ap.add_argument("--rerank", choices=["auto", "on", "off"], default="auto",
                    help="cross-encoder head re-rank (default: the pipeline's auto-gate)")
    ap.add_argument("--lexical-only", action="store_true",
                    help="evaluate the FTS arm alone (semantic + rerank arms off) — "
                    "the search a core install without the [embeddings] extra gets. "
                    "The CI gate instead runs the fused pipeline with --rerank off: "
                    "the production path minus the cross-encoder, whose per-query "
                    "inference would multiply the row's runtime")
    ap.add_argument("--content-type", default=None,
                    help="restrict the searched scope to one content type")
    ap.add_argument("--include-meta", action="store_true",
                    help="auto-titles only: leave title/summary docs in the "
                    "searched scope (the log protocol always searches them — "
                    "its queries aren't derived from any document)")
    ap.add_argument("--exclude-content-type", action="append", default=None,
                    metavar="TYPE",
                    help="drop a content type from the searched scope "
                    "(repeatable; overrides the protocol's default exclusions "
                    "— e.g. --exclude-content-type summary measures a scope "
                    "without stored summaries)")
    ap.add_argument("--require-semantic", action="store_true",
                    help="exit 1 if the semantic arm is unavailable — without "
                    "this, a dead embeddings model silently degrades the "
                    "'fused' pipeline under test to lexical-only and the "
                    "metrics measure the wrong stack")
    ap.add_argument("--require-rerank", action="store_true",
                    help="exit 1 unless the real cross-encoder loads and "
                    "discriminates a trivial pair — the rerank analog of "
                    "--require-semantic. Costs one model load; the metric run "
                    "itself may still skip per-query rerank (--rerank off), "
                    "so the gate proves the arm is alive without paying "
                    "per-query inference")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--dump-cases", type=Path, metavar="FILE", default=None,
                    help="also write the evaluated cases as JSONL (real usage "
                    "data — keep it out of the repo)")
    gate = ap.add_argument_group(
        "gate", "regression floors — any breach exits 1 (the CI rows set these; "
        "floors are a ratchet calibrated under measured values, not a target)"
    )
    gate.add_argument("--min-mrr", type=float, default=None)
    gate.add_argument("--min-recall10", type=float, default=None)
    gate.add_argument("--min-recall20", type=float, default=None)
    args = ap.parse_args()

    if args.lexical_only and args.require_rerank:
        ap.error("--require-rerank contradicts --lexical-only "
                 "(which switches the rerank arm off for this process)")

    if args.lexical_only:
        # The product's own switch: both model arms report unavailable for the rest
        # of this process, so the measured pipeline is the one a lexical-only box runs.
        os.environ["THREAD_ARCHIVE_EMBED"] = "off"
        os.environ["THREAD_ARCHIVE_RERANK"] = "off"

    api.open_archive()

    def append_trend(row: dict) -> None:
        if not args.trend_out:
            return
        from datetime import datetime, timezone

        out = args.trend_out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        stamped = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   **row}
        with out.open("a") as f:
            f.write(json.dumps(stamped) + "\n")

    if args.behavior:
        with use_session() as s:
            report = behavior_report(_trail_events(s, args.mined_after))
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"searches: {report['n_searches']}   "
                  f"sessions: {report['n_sessions']}   "
                  f"click: {report['click_rate']:.3f}   "
                  f"reformulate: {report['reformulation_rate']:.3f}   "
                  f"abandon: {report['abandonment_rate']:.3f}   "
                  f"reads/click: {report['reads_per_click']:.2f}")
        append_trend({"protocol": "behavior",
                      "mined_after": args.mined_after, **report})
        return

    if args.require_semantic:
        from thread_archive._retrieval import embed

        if not embed.is_available():
            print("RETRIEVAL GATE BREACH: semantic arm unavailable "
                  "(embeddings model failed to load?)", file=sys.stderr)
            raise SystemExit(1)
    if args.require_rerank:
        from thread_archive._retrieval import rerank as rerank_mod

        breach = rerank_probe(rerank_mod.default())
        if breach:
            print(f"RETRIEVAL GATE BREACH: {breach}", file=sys.stderr)
            raise SystemExit(1)
    if args.auto_titles is not None:
        cases = sample_title_cases(args.auto_titles, args.seed)
        exclude = None if args.include_meta else EXCLUDE_META
    elif args.from_log is not None:
        cases = mine_log_cases(args.from_log, args.seed, args.mined_after)
        exclude = None
    else:
        cases = load_case_file(args.cases)
        exclude = None
    if args.exclude_content_type:
        exclude = args.exclude_content_type
    if not cases:
        raise SystemExit("no eval cases")
    if args.dump_cases:
        args.dump_cases.write_text(
            "".join(json.dumps(c) + "\n" for c in cases))
    rerank = None if args.rerank == "auto" else (args.rerank == "on")

    report = evaluate(cases, limit=args.limit, rerank=rerank,
                      content_type=args.content_type, exclude_content_types=exclude)

    protocol = ("auto-titles" if args.auto_titles is not None
                else "from-log" if args.from_log is not None else "cases")
    append_trend({
        "protocol": protocol, "seed": args.seed, "limit": args.limit,
        "rerank": args.rerank, "lexical_only": args.lexical_only,
        "mined_after": args.mined_after,
        "n": report["n"], "mrr": report["mrr"],
        "recall": {str(k): v for k, v in report["recall"].items()},
        "latency_p50_ms": report["latency_p50_ms"],
    })

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"cases: {report['n']}   MRR: {report['mrr']:.3f}   "
              + "   ".join(f"R@{k}: {v:.3f}" for k, v in report["recall"].items()))
        print(f"latency p50: {report['latency_p50_ms']:.0f} ms")
        for shape, stats in report["per_shape"].items():
            print(f"  {shape:>15}: n={stats['n']:<4} MRR={stats['mrr']:.3f}")

    breaches = []
    if args.min_mrr is not None and report["mrr"] < args.min_mrr:
        breaches.append(f"MRR {report['mrr']:.3f} < floor {args.min_mrr}")
    if args.min_recall10 is not None and report["recall"][10] < args.min_recall10:
        breaches.append(f"recall@10 {report['recall'][10]:.3f} < floor {args.min_recall10}")
    if args.min_recall20 is not None and report["recall"][20] < args.min_recall20:
        breaches.append(f"recall@20 {report['recall'][20]:.3f} < floor {args.min_recall20}")
    if breaches:
        for b in breaches:
            print(f"RETRIEVAL GATE BREACH: {b}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
