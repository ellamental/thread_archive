#!/usr/bin/env python3
"""How much of what's relevant lands in the window an agent actually reads.

The ordering metrics ask where the first right answer sits. That is not how agents
use search: they fire several queries carrying terms that surround what they want,
dedupe the results by hand, and read around whatever looks worth opening. What that
workflow needs is a window full of relevant material, which is what this scores.

Two measures, both ceiling-aware — raw ``recall@k`` on a multi-answer case scores
the size of the gold set as much as the ranking, because a case with 23 relevant
threads cannot exceed 0.43 recall@10 however well it ranks:

- **window fill** — ``hits@k / min(k, |gold|)``. Of the relevant threads that could
  physically fit the window, what share did. 1.0 means the window is as full of
  relevant material as it can be. Reported over grade-2 (answers) and grade>=1
  (answers plus partials, which an agent skimming the window still finds worth
  opening).
- **union coverage** — fire every query a case file carries, union the windows,
  dedupe, and measure the share of the file's whole grade-2 set assembled. The
  fan-out workflow scored end to end, and the number that shows how much of a
  subject an agent can reach at all.

Runs the shipped stack and, for the reference every fill number needs,
``bm25_baseline``'s plain BM25 over the same snapshot. A fill number in isolation
certifies nothing: it is not comparable across corpora, and which way the margin
falls tracks corpus selectivity (see ``docs/search-quality.md``).

**These numbers are bounded by how the gold was labeled.** ``topic``/``query``/
``rerank`` pools are assembled by agents searching with the production stack, so
the relevant set is approximately what this ranker can reach — a thread it
systematically cannot surface never enters the gold and so never scores as missing.
A completeness metric is more exposed to that than an ordering one; read the
absolute numbers as upper bounds.

Read-only. Runs against whatever ``THREAD_ARCHIVE_HOME`` names::

    THREAD_ARCHIVE_HOME=~/.thread/archive-snap python search_lab/window_fill.py
    THREAD_ARCHIVE_HOME=~/.thread/archive-snap python search_lab/window_fill.py \\
        --cases ~/.thread/archive/topic-cases-2e292234e4cf.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# The lab dir too, so bare sibling imports (eval_core, snapshot, …) resolve
# however this file was loaded: as a script, by path, or as search_lab.X.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bm25_baseline import bm25_search  # noqa: E402
from eval_core import warm_for_scoring  # noqa: E402
from mine._framework import gold_dir  # noqa: E402

from thread_archive import _api as api  # noqa: E402


def _fill(retrieved: list[str], gold: set[str], k: int) -> float:
    """Share of the window's relevant capacity that relevant threads occupy."""
    return len(set(retrieved[:k]) & gold) / min(k, len(gold))


def score_file(path: Path, k: int) -> dict | None:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    acc = {"f2s": 0.0, "f2b": 0.0, "f1s": 0.0, "f1b": 0.0, "n": 0}
    u_gold: set[str] = set()
    u_st: set[str] = set()
    u_bm: set[str] = set()
    for row in rows:
        grades = {t: int(g) for t, g in (row.get("grades") or {}).items()}
        g2 = {t for t, g in grades.items() if g == 2}
        g1 = {t for t, g in grades.items() if g >= 1}
        if not g2:
            continue
        st = [h["thread_id"] for h in api.search(row["query"], limit=k)]
        bm = [h["thread_id"] for h in bm25_search(row["query"], limit=k)]
        acc["f2s"] += _fill(st, g2, k)
        acc["f2b"] += _fill(bm, g2, k)
        if g1:
            acc["f1s"] += _fill(st, g1, k)
            acc["f1b"] += _fill(bm, g1, k)
        acc["n"] += 1
        u_gold |= g2
        u_st |= set(st)
        u_bm |= set(bm)
    if not acc["n"]:
        return None
    acc["union_gold"] = len(u_gold)
    acc["union_st"] = len(u_st & u_gold)
    acc["union_bm"] = len(u_bm & u_gold)
    return acc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", type=Path, nargs="*", metavar="FILE",
                    help="case files to score (default: every topic file in the "
                         "gold dir — the protocol with enough answers per case for "
                         "a completeness metric to have resolution)")
    ap.add_argument("-k", type=int, default=10,
                    help="window size, the results an agent actually reads "
                         "(default 10, the MCP search tool's default limit)")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    files = args.cases or sorted(
        p for p in gold_dir().glob("topic-cases-*.jsonl") if "detail" not in p.name)
    if not files:
        print("no case files found", file=sys.stderr)
        return 1

    api.open_archive()
    # This scores through `api.search` rather than `_eval.evaluate`, so it warms
    # explicitly rather than inheriting the scorer's warm-up.
    warm_for_scoring()

    rows, tot = [], {"f2s": 0.0, "f2b": 0.0, "f1s": 0.0, "f1b": 0.0, "n": 0,
                     "union_gold": 0, "union_st": 0, "union_bm": 0}
    for path in files:
        acc = score_file(Path(path), args.k)
        if acc is None:
            continue
        n = acc["n"]
        rows.append({
            "file": Path(path).stem, "n": n,
            "fill2_stack": acc["f2s"] / n, "fill2_bm25": acc["f2b"] / n,
            "fill1_stack": acc["f1s"] / n, "fill1_bm25": acc["f1b"] / n,
            "union_stack": acc["union_st"] / acc["union_gold"],
            "union_bm25": acc["union_bm"] / acc["union_gold"],
        })
        for key in tot:
            tot[key] += acc[key]

    n = tot["n"]
    report = {
        "k": args.k, "files": len(rows), "n": n,
        "fill2_stack": tot["f2s"] / n, "fill2_bm25": tot["f2b"] / n,
        "fill1_stack": tot["f1s"] / n, "fill1_bm25": tot["f1b"] / n,
        "union_stack": tot["union_st"] / tot["union_gold"],
        "union_bm25": tot["union_bm"] / tot["union_gold"],
        "per_file": rows,
    }
    if args.json:
        print(json.dumps(report, indent=1))
        return 0

    print(f"{'case file':<40}{'n':>4}{'fill2 st':>10}{'fill2 bm':>10}"
          f"{'fill1 st':>10}{'fill1 bm':>10}{'union st':>10}{'union bm':>10}")
    for r in rows:
        print(f"{r['file']:<40}{r['n']:>4}{r['fill2_stack']:>10.3f}"
              f"{r['fill2_bm25']:>10.3f}{r['fill1_stack']:>10.3f}"
              f"{r['fill1_bm25']:>10.3f}{r['union_stack']:>10.3f}"
              f"{r['union_bm25']:>10.3f}")
    print(f"{'POOLED':<40}{n:>4}{report['fill2_stack']:>10.3f}"
          f"{report['fill2_bm25']:>10.3f}{report['fill1_stack']:>10.3f}"
          f"{report['fill1_bm25']:>10.3f}{report['union_stack']:>10.3f}"
          f"{report['union_bm25']:>10.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
