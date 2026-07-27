"""Does the corpus-native embedding graph earn its spot in retrieval? Measure it.

The claim under test: communities built from the corpus itself
(:mod:`thread_archive._retrieval.embed_graph` — thread centroids → cosine kNN
→ Leiden, no topic-graph input) carry a retrieval signal. Two levers, evaluated
on the same log-mined cases as ``retrieval_eval.py`` (real queries, subsequent
reads as clicks):

* **coherence re-rank** — within the production pool, boost threads whose
  community carries more of the pool's top mass:
  ``score = 1/(60+rank) + gamma * community_mass``. The precision lever —
  this is the signal production search ships (``_apply_coherence``), so this
  harness is its regression check as well as its original gate.
* **expansion** — append community-mates of the pool's top seeds, ranked by
  query→centroid cosine, after the production results. The recall lever,
  aimed at the pool-miss rate (golds production search never surfaces). Not
  in production: as slotted here it costs more success@20 than its rescues buy.

Baseline is production search order with coherence *and* the cross-encoder
off (symmetric across all arms; both sit above candidate selection, which is
what these arms move). Reports MRR, success@k, and true recall@k per variant,
plus pool-miss diagnostics for the expansion arm. Read-only; run against the
live archive:

    .venv/bin/python search_lab/graph_eval.py
    .venv/bin/python search_lab/graph_eval.py --cases 150 --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

# The log-mined case harness lives next door.
_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval", Path(__file__).resolve().parent / "retrieval_eval.py")
retrieval_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("retrieval_eval", retrieval_eval)
_SPEC.loader.exec_module(retrieval_eval)

RECALL_KS = (1, 5, 10, 20)
POOL = 60          # production candidates fetched per case
LIMIT = 20         # ranking window scored (matches retrieval_eval)
GAMMAS = (0.002, 0.005, 0.01)
EXPAND_SEEDS = 5   # pool prefix whose communities are expansion sources
EXPAND_CAP = 40    # expansion candidates appended at most


def expansion_candidates(
    pool: list[str],
    community: dict[str, int],
    members: dict[int, list[str]],
    scores: dict[str, float],
    cap: int = EXPAND_CAP,
    seeds: int = EXPAND_SEEDS,
) -> list[str]:
    """Community-mates of the pool's top seeds, absent from the pool, best
    query-cosine first — pure given precomputed centroid ``scores``."""
    in_pool = set(pool)
    cids = {community[t] for t in pool[:seeds] if t in community}
    cands = {t for c in cids for t in members.get(c, []) if t not in in_pool}
    ranked = sorted(cands, key=lambda t: (-scores.get(t, -1.0), t))
    return ranked[:cap]


def score_case(order: list[str], gold: set, successes_at: dict,
               recall_at: dict, rr: list) -> int:
    positions = {}
    for r, thread_id in enumerate(order[:LIMIT], 1):
        if thread_id in gold and thread_id not in positions:
            positions[thread_id] = r
    rank = min(positions.values(), default=0)
    rr.append(1.0 / rank if rank else 0.0)
    for k in RECALL_KS:
        found = sum(position <= k for position in positions.values())
        if found:
            successes_at[k] += 1
        recall_at[k] += found / len(gold) if gold else 0.0
    return rank


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", type=int, default=2000,
                    help="max log-mined cases (the default is above the number "
                         "the trail yields, so it scores all of them)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    # The baseline pool must be coherence-free — the arms re-apply it themselves.
    os.environ["THREAD_ARCHIVE_COHERENCE"] = "off"

    from thread_archive import _api as api
    from thread_archive._retrieval import embed_graph
    from thread_archive._retrieval.embed import embed_query

    api.open_archive()
    t0 = time.time()
    graph = embed_graph.build()
    if graph is None:
        print("no embedded vectors — nothing to evaluate", file=sys.stderr)
        return 2
    print(f"corpus graph: {len(graph.thread_ids)} threads, {graph.edges} edges, "
          f"{len(graph.members)} communities ({time.time() - t0:.0f}s)", flush=True)

    cases = retrieval_eval.mine_log_cases(args.cases, seed=args.seed)
    print(f"{len(cases)} cases", flush=True)

    variants = (["baseline"]
                + [f"coherence:{g}" for g in GAMMAS]
                + ["expansion"])
    rr: dict[str, list] = {v: [] for v in variants}
    successes_at: dict[str, dict] = {v: {k: 0 for k in RECALL_KS} for v in variants}
    recall_at: dict[str, dict] = {v: {k: 0.0 for k in RECALL_KS} for v in variants}
    pool_misses = 0
    rescued = 0

    for i, case in enumerate(cases):
        gold = set(case["gold"])
        skip = set(case.get("sessions", []))
        hits = api.search(case["query"], limit=POOL + len(skip), rerank=False)
        pool, seen = [], set()
        for h in hits:
            t = h["thread_id"]
            if t not in skip and t not in seen:
                seen.add(t)
                pool.append(t)

        score_case(pool, gold, successes_at["baseline"], recall_at["baseline"],
                   rr["baseline"])
        for g in GAMMAS:
            order = embed_graph.coherence_order(pool, graph.community, g)
            score_case(order, gold, successes_at[f"coherence:{g}"],
                       recall_at[f"coherence:{g}"], rr[f"coherence:{g}"])

        missed = not (gold & set(pool))
        pool_misses += missed
        qvec = embed_query(case["query"])
        expansion: list[str] = []
        if qvec is not None:
            cids = {graph.community[t] for t in pool[:EXPAND_SEEDS] if t in graph.community}
            mates = [t for c in cids for t in graph.members.get(c, [])]
            scores = graph.similarity(qvec, mates)
            expansion = expansion_candidates(pool, graph.community, graph.members, scores)
        order = pool[:LIMIT - min(len(expansion), LIMIT // 2)] if expansion else pool
        # Expansion candidates ride behind the retained pool prefix — coverage
        # added at the tail, precision of the head preserved.
        order = order + [t for t in expansion if t not in set(order)]
        rank = score_case(order, gold, successes_at["expansion"],
                          recall_at["expansion"], rr["expansion"])
        if missed and rank:
            rescued += 1

        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(cases)} ({time.time() - t0:.0f}s)", flush=True)

    n = len(cases)
    report = {
        "n_cases": n,
        "graph": {"threads": len(graph.thread_ids), "edges": graph.edges,
                  "communities": len(graph.members)},
        "variants": {
            v: {"mrr": round(sum(rr[v]) / n, 4),
                **{f"success@{k}": round(successes_at[v][k] / n, 3)
                   for k in RECALL_KS},
                **{f"recall@{k}": round(recall_at[v][k] / n, 3)
                   for k in RECALL_KS}}
            for v in variants
        },
        "pool_misses": pool_misses,
        "rescued_by_expansion": rescued,
    }
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\n{'variant':<16} {'MRR':>7} "
              + " ".join(f"S@{k:<4}" for k in RECALL_KS)
              + f" {'R@10':>6} {'R@20':>6}")
        for v in variants:
            m = report["variants"][v]
            successes = " ".join(f"{m[f'success@{k}']:.3f}" for k in RECALL_KS)
            print(f"{v:<16} {m['mrr']:>7.4f} {successes} "
                  f"{m['recall@10']:>6.3f} {m['recall@20']:>6.3f}")
        print(f"\npool misses (gold absent from top-{POOL}): {pool_misses}/{n}"
              f" — expansion rescued {rescued}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
