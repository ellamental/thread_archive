#!/usr/bin/env python3
"""Plain BM25 over the corpus — the reference a gold file's numbers are read against.

A mined gold file scores the shipped stack and reports, say, MRR 0.55. On its own
that number means nothing: it is not comparable across corpora (a 726-thread
corpus and a 40k-thread one are different problems), and the third-party
yardsticks (``beir_eval`` / ``haystack_eval``) carry published BM25 references for
*their* corpora, not for whatever corpus the golds were mined from. This harness
supplies the missing rung — the same cases, the same metrics, ranked by nothing
but SQLite FTS5's ``bm25()`` over the same searchable scope.

What it establishes is a floor, not a rival: BM25 is what the corpus gives up to
term matching alone. The stack's margin over it is the part of the number that
the fusion, the vector arm, and the cross-encoder actually earned, and a stack
that fails to clear it on a stratum is measuring a corpus where its extra
machinery does not pay.

Deliberately unfused and untuned. It reads the same FTS5 index the lexical arm
reads, but none of the ranking above it — no density or phrase or recency
weighting, no RRF, no coherence pass, no rerank. Query text becomes a bag of
quoted terms OR'd together (the standard baseline reading), a thread scores as
its best-matching chunk, and threads rank by that. Ties break arbitrarily, as
they do in any BM25 run.

The intermediate rungs need no code: ``retrieval_eval.py --lexical-only`` is the
archive's own lexical arm (BM25 plus its weighting, no vector arm, no rerank),
and ``--rerank off`` is the fused pipeline minus the cross-encoder. Together the
four make an ablation ladder over one case file.

Read-only. Runs against whatever ``THREAD_ARCHIVE_HOME`` names, and refuses cases
mined against a different corpus, exactly as the eval does::

    THREAD_ARCHIVE_HOME=<snapshot> python search_lab/bm25_baseline.py \\
        --cases ~/dev/swe-chat-data/gold/commit-cases.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import retrieval_eval as harness  # noqa: E402

from thread_archive import _api as api  # noqa: E402
from thread_archive._eval import EXCLUDE_META, evaluate, load_case_file  # noqa: E402
from thread_archive._store import use_session  # noqa: E402

# FTS5 reads bare punctuation as query syntax, so every term is quoted and the
# terms are OR'd — a bag of words, which is what a BM25 baseline is.
_TERM = re.compile(r"[^\W_]+", re.UNICODE)


def fts_query(text: str) -> str:
    """``text`` as an FTS5 OR-of-quoted-terms match expression, or ``""`` when it
    holds no indexable term (the caller must not run a match on that)."""
    return " OR ".join(f'"{t}"' for t in _TERM.findall(text))


def bm25_search(query: str, *, limit: int = 20, content_types=None,
                exclude_content_types=None, rerank=None) -> list[dict]:
    """Rank threads by SQLite's ``bm25()`` alone, best-matching chunk per thread.

    Signature-compatible with ``api.search`` so :func:`evaluate` can drive it in
    the incumbent's place; ``rerank`` is accepted and ignored, which is the whole
    point of the baseline. Scores are FTS5's own (more negative = better match),
    carried through so a caller can inspect the ranking, and threads with no
    matching chunk simply do not appear."""
    match = fts_query(query)
    if not match:
        return []
    where = ["event_search MATCH :match"]
    params: dict[str, object] = {"match": match, "limit": limit}
    if content_types:
        where.append("content_type IN (%s)" % ", ".join(
            f":ct{i}" for i in range(len(content_types))))
        params.update({f"ct{i}": v for i, v in enumerate(content_types)})
    if exclude_content_types:
        where.append("(content_type IS NULL OR content_type NOT IN (%s))" % ", ".join(
            f":xt{i}" for i in range(len(exclude_content_types))))
        params.update({f"xt{i}": v for i, v in enumerate(exclude_content_types)})

    from sqlalchemy import text as sa_text

    # bm25() is an FTS5 auxiliary function: legal only as a result column of the
    # query that matches the table, never inside an aggregate. A plain subquery
    # does not help — SQLite flattens it into the outer GROUP BY and the function
    # lands in the illegal context anyway — so the match is pinned in a
    # MATERIALIZED CTE, and the roll-up to a thread's best chunk happens outside it.
    sql = sa_text(
        "WITH matches AS MATERIALIZED ("
        "  SELECT thread_id AS tid, bm25(event_search) AS score FROM event_search"
        f"  WHERE {' AND '.join(where)}"
        ") SELECT tid, MIN(score) AS best FROM matches "
        "GROUP BY tid ORDER BY best ASC LIMIT :limit"
    )
    with use_session() as s:
        rows = s.execute(sql, params).all()
    return [{"thread_id": str(tid), "score": float(score)} for tid, score in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", type=Path, required=True, metavar="FILE",
                    help="gold case file to score (same shape retrieval_eval reads)")
    ap.add_argument("--limit", type=int, default=20,
                    help="results per query (metric ceiling; default 20)")
    ap.add_argument("--exclude-meta", action="store_true",
                    help="drop title/summary docs from scope. Off by default, which "
                         "is what `retrieval_eval --cases` searches — a baseline "
                         "reading a narrower scope than the incumbent is not one")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    api.open_archive()
    cases = load_case_file(args.cases)
    harness._require_matching_snapshot(cases, args.cases)

    report = evaluate(
        cases, limit=args.limit, rerank=None, content_type=None,
        exclude_content_types=EXCLUDE_META if args.exclude_meta else None,
        search=bm25_search)

    if args.json:
        print(json.dumps(report, indent=1, default=str))
        return 0
    print(f"bm25 baseline: {args.cases.name}   cases: {report['n']}   "
          f"MRR: {report['mrr']:.3f}   "
          + "   ".join(f"S@{k}: {v:.3f}" for k, v in report["success"].items()))
    print("   ".join(f"R@{k}: {v:.3f}" for k, v in report["recall"].items()))
    print("   ".join(f"nDCG@{k}: {v:.3f}" for k, v in report["ndcg"].items()))
    for tier, b in sorted(report.get("per_difficulty", {}).items()):
        print(f"      {tier:<12s} n {b['n']:>3d}  MRR {b['mrr']:.3f}  "
              f"S@10 {b['success10']:.3f}  R@10 {b['recall10']:.3f}  "
              f"nDCG@10 {b['ndcg10']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
