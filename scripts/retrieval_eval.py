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
during ranking (it quotes the query verbatim). A click is the pick from what
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

Reports MRR and recall@1/5/10/20 at thread-level relevance, overall and
per query-shape (so a lexical regression can't hide behind semantic wins).

Read-only. Run against the live archive:

    .venv/bin/python scripts/retrieval_eval.py --auto-titles 200
    .venv/bin/python scripts/retrieval_eval.py --from-log 500
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text as sa_text  # noqa: E402

from thread_archive import _api as api  # noqa: E402
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


def pair_log_events(events: list[tuple[int, str, object]]) -> list[dict]:
    """Pair search calls with the reads that followed them.

    ``events``: (session_thread_id, kind, value) in occurrence order, where kind
    is 'search' (value: query string) or 'read' (value: thread id). Pure — DB
    filtering (gold existence, exclude_from_search) happens in the caller.
    """
    per_session: dict[int, list[tuple[str, object]]] = {}
    for sess, kind, value in events:
        per_session.setdefault(sess, []).append((kind, value))

    cases: list[dict] = []
    for sess, evs in per_session.items():
        seen_reads: set[int] = set()
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


def mine_log_cases(n: int, seed: int) -> list[dict]:
    """Real search->read pairs from the archive's own tool-use trail."""
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT thread_id, payload FROM events "
            "WHERE event_type = 'tool_use_complete' "
            "AND (payload LIKE '%thread_search%' OR payload LIKE '%thread_read%') "
            "ORDER BY thread_id, id"
        )).all()

        events: list[tuple[int, str, object]] = []
        for sess, payload in rows:
            p = payload if isinstance(payload, dict) else json.loads(payload)
            kind = classify_tool(p.get("tool_name"))
            inp = p.get("input") or {}
            if kind == "search":
                q = inp.get("query")
                if isinstance(q, str) and q.strip():
                    events.append((sess, "search", q.strip()))
            elif kind == "read":
                try:
                    events.append((sess, "read", int(inp.get("thread_id"))))
                except (TypeError, ValueError):
                    continue

        cases = pair_log_events(events)

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
        cases.append({"query": row["query"], "gold": list(row["gold"]),
                      "sessions": list(row.get("sessions", []))})
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
             exclude_content_types: list[str] | None) -> dict:
    per_shape: dict[str, list[float]] = {}
    reciprocal_ranks: list[float] = []
    hits_at: dict[int, int] = {k: 0 for k in RECALL_KS}
    latencies: list[float] = []

    for case in cases:
        gold = set(case["gold"])
        skip = set(case.get("sessions", []))
        t0 = time.monotonic()
        hits = api.search(
            case["query"],
            limit=limit + len(skip),
            content_types=[content_type] if content_type else None,
            exclude_content_types=exclude_content_types,
            rerank=rerank,
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
                    "without librarian summaries)")
    ap.add_argument("--require-semantic", action="store_true",
                    help="exit 1 if the semantic arm is unavailable — without "
                    "this, a dead embeddings model silently degrades the "
                    "'fused' pipeline under test to lexical-only and the "
                    "metrics measure the wrong stack")
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

    if args.lexical_only:
        from thread_archive._retrieval import embed
        from thread_archive._retrieval import rerank as rerank_mod

        embed.is_available = lambda: False  # type: ignore[method-assign]
        rerank_mod.is_available = lambda: False  # type: ignore[method-assign]

    api.open_archive()
    if args.require_semantic:
        from thread_archive._retrieval import embed

        if not embed.is_available():
            print("RETRIEVAL GATE BREACH: semantic arm unavailable "
                  "(embeddings model failed to load?)", file=sys.stderr)
            raise SystemExit(1)
    if args.auto_titles is not None:
        cases = sample_title_cases(args.auto_titles, args.seed)
        exclude = None if args.include_meta else EXCLUDE_META
    elif args.from_log is not None:
        cases = mine_log_cases(args.from_log, args.seed)
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
