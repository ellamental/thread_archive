"""Does the corpus-native embedding graph earn a spot in retrieval? Measure it.

The claim under test: communities built from the corpus itself
(:mod:`thread_archive._retrieval.embed_graph` — thread centroids → cosine kNN
→ Leiden, zero curation input) carry a retrieval signal. Two levers, evaluated
on the same log-mined cases as ``retrieval_eval.py`` (real queries, subsequent
reads as clicks):

* **coherence re-rank** — within the production pool, boost threads whose
  community carries more of the pool's top mass:
  ``score = 1/(60+rank) + gamma * community_mass``. The precision lever.
* **expansion** — append community-mates of the pool's top seeds, ranked by
  query→centroid cosine, after the production results. The recall lever,
  aimed at the pool-miss rate (golds production search never surfaces).

Baseline is production search order (no cross-encoder — symmetric across all
arms and ~10x faster; the reranker sits above candidate selection, which is
what these arms move). Reports MRR + recall@k per variant, plus pool-miss
diagnostics for the expansion arm. Read-only; run against the live archive:

    .venv/bin/python scripts/graph_eval.py
    .venv/bin/python scripts/graph_eval.py --cases 150 --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_eval", Path(__file__).resolve().parent / "retrieval_eval.py")
retrieval_eval = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("retrieval_eval", retrieval_eval)
_SPEC.loader.exec_module(retrieval_eval)

RECALL_KS = (1, 5, 10, 20)
POOL = 60          # production candidates fetched per case
LIMIT = 20         # ranking window scored (matches retrieval_eval)
TOP_MASS = 10      # pool prefix that votes on community mass
RRF_K = 60
GAMMAS = (0.002, 0.005, 0.01)
EXPAND_SEEDS = 5   # pool prefix whose communities are expansion sources
EXPAND_CAP = 40    # expansion candidates appended at most


def mass_for(pool: list[str], community: dict[str, int]) -> dict[int, float]:
    """Rank-weighted share of the pool's top slots each community holds — pure."""
    mass: dict[int, float] = {}
    total = 0.0
    for i, t in enumerate(pool[:TOP_MASS]):
        w = 1.0 / (i + 1)
        total += w
        cid = community.get(t)
        if cid is not None:
            mass[cid] = mass.get(cid, 0.0) + w
    return {c: m / total for c, m in mass.items()} if total else {}


def coherence_order(pool: list[str], community: dict[str, int], gamma: float) -> list[str]:
    """Re-rank the pool by RRF base + gamma * its community's mass — pure."""
    mass = mass_for(pool, community)
    base = {t: 1.0 / (RRF_K + r) for r, t in enumerate(pool, start=1)}
    return sorted(pool, key=lambda t: (-(base[t] + gamma * mass.get(community.get(t), 0.0)), t))


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


def score_case(order: list[str], gold: set, hits_at: dict, rr: list) -> int:
    rank = next((r for r, t in enumerate(order[:LIMIT], 1) if t in gold), 0)
    rr.append(1.0 / rank if rank else 0.0)
    for k in RECALL_KS:
        if rank and rank <= k:
            hits_at[k] += 1
    return rank


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", type=int, default=2000,
                    help="max log-mined cases (default: all ~563)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

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
    hits_at: dict[str, dict] = {v: {k: 0 for k in RECALL_KS} for v in variants}
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

        score_case(pool, gold, hits_at["baseline"], rr["baseline"])
        for g in GAMMAS:
            order = coherence_order(pool, graph.community, g)
            score_case(order, gold, hits_at[f"coherence:{g}"], rr[f"coherence:{g}"])

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
        # Expansion candidates ride behind the retained pool prefix — recall
        # added at the tail, precision of the head preserved.
        order = order + [t for t in expansion if t not in set(order)]
        rank = score_case(order, gold, hits_at["expansion"], rr["expansion"])
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
                **{f"recall@{k}": round(hits_at[v][k] / n, 3) for k in RECALL_KS}}
            for v in variants
        },
        "pool_misses": pool_misses,
        "rescued_by_expansion": rescued,
    }
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\n{'variant':<16} {'MRR':>7} " + " ".join(f"R@{k:<4}" for k in RECALL_KS))
        for v in variants:
            m = report["variants"][v]
            recs = " ".join(f"{m[f'recall@{k}']:.3f}" for k in RECALL_KS)
            print(f"{v:<16} {m['mrr']:>7.4f} {recs}")
        print(f"\npool misses (gold absent from top-{POOL}): {pool_misses}/{n}"
              f" — expansion rescued {rescued}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
