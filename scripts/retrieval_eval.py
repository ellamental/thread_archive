"""Retrieval eval harness — measure search quality so ranking changes are measurable.

Two golden-set modes:

- ``--golden golden.jsonl``: curated or usage-mined pairs, one JSON object per
  line — ``{"query": "...", "thread_id": N}`` / ``{"query": "...",
  "thread_ids": [N, ...]}`` (thread-level relevance, any listed thread counts)
  or ``{"query": "...", "event_id": N}`` (event-level). Comments (#) allowed.
- ``--auto-titles N``: a zero-curation protocol — sample N titled conversation
  threads, use each *title* as the query, and score whether the thread's own
  content ranks. Thread-meta docs (title/summary) are excluded from the searched
  scope so the eval never matches the query against itself; what's measured is
  whether the thread's *messages* are reachable from its title's vocabulary.

Reports MRR and recall@1/5/10/20 over the chosen relevance level, overall and
per query-shape (so a lexical regression can't hide behind semantic wins).

Read-only. Run against the live archive:

    .venv/bin/python scripts/retrieval_eval.py --auto-titles 200
    .venv/bin/python scripts/retrieval_eval.py --golden ~/.thread/archive/golden-queries.jsonl

The golden set is mined from real usage by ``golden_from_usage.py`` (and lives in
the archive home because its queries are real, sometimes personal, data).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text as sa_text  # noqa: E402

from thread_archive import api  # noqa: E402
from thread_archive.store import use_session  # noqa: E402

RECALL_KS = (1, 5, 10, 20)

# Meta docs are excluded from every eval search: in --auto-titles the query IS the
# title (self-match would saturate the metrics), and a hand-golden set should
# measure message reachability on both sides of the meta-doc feature.
EXCLUDE_META = ["title", "summary"]


def load_golden(path: str) -> list[dict]:
    cases = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        if "query" not in row or not ("thread_id" in row or "thread_ids" in row or "event_id" in row):
            raise SystemExit(f"golden row needs 'query' and 'thread_id(s)' or 'event_id': {row}")
        cases.append(row)
    return cases


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
    return [{"query": title, "thread_id": tid} for tid, title in rows[:n]]


def query_shape(q: str) -> str:
    if "|" in q:
        return "pipe-or"
    if any(c in q for c in ('"',)) or any(w in q.split() for w in ("AND", "OR", "NOT")):
        return "boolean/phrase"
    if "_" in q or "::" in q or any("." in w and not w.endswith(".") for w in q.split()):
        return "code"
    return "natural" if len(q.split()) >= 2 else "single-term"


def evaluate(cases: list[dict], *, limit: int, rerank, content_type) -> dict:
    per_shape: dict[str, list[float]] = {}
    reciprocal_ranks: list[float] = []
    hits_at: dict[int, int] = {k: 0 for k in RECALL_KS}
    latencies: list[float] = []

    for case in cases:
        t0 = time.monotonic()
        hits = api.search(
            case["query"],
            limit=limit,
            content_types=[content_type] if content_type else None,
            exclude_content_types=EXCLUDE_META,
            rerank=rerank,
        )
        latencies.append(time.monotonic() - t0)

        relevant_threads = set(case.get("thread_ids") or
                               ([case["thread_id"]] if "thread_id" in case else []))
        rank = 0  # 0 = not found within limit
        for i, h in enumerate(hits, start=1):
            if "event_id" in case:
                found = h["event_id"] == case["event_id"]
            else:
                found = h["thread_id"] in relevant_threads
            if found:
                rank = i
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
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--golden", help="path to a golden-set JSONL")
    src.add_argument("--auto-titles", type=int, metavar="N",
                     help="sample N thread titles as queries (thread-level relevance)")
    ap.add_argument("--seed", type=int, default=7, help="sampling seed for --auto-titles")
    ap.add_argument("--limit", type=int, default=20, help="results per query (recall ceiling)")
    ap.add_argument("--rerank", choices=["auto", "on", "off"], default="auto",
                    help="cross-encoder head re-rank (default: the pipeline's auto-gate)")
    ap.add_argument("--content-type", default=None,
                    help="restrict the searched scope to one content type")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args()

    api.open_archive()
    cases = load_golden(args.golden) if args.golden else sample_title_cases(args.auto_titles, args.seed)
    if not cases:
        raise SystemExit("no eval cases")
    rerank = None if args.rerank == "auto" else (args.rerank == "on")

    report = evaluate(cases, limit=args.limit, rerank=rerank, content_type=args.content_type)

    if args.json:
        print(json.dumps(report, indent=2))
        return
    print(f"cases: {report['n']}   MRR: {report['mrr']:.3f}   "
          + "   ".join(f"R@{k}: {v:.3f}" for k, v in report["recall"].items()))
    print(f"latency p50: {report['latency_p50_ms']:.0f} ms")
    for shape, stats in report["per_shape"].items():
        print(f"  {shape:>15}: n={stats['n']:<4} MRR={stats['mrr']:.3f}")


if __name__ == "__main__":
    main()
