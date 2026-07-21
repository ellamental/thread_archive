"""Unit coverage for graph_eval's pure ranking logic (the DB-touching paths run
against the live archive, like the other eval harnesses). The coherence
formula itself lives in production (``_retrieval.embed_graph``) and is covered
by test_embed_graph.py; this pins the eval-only levers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "graph_eval", Path(__file__).resolve().parent.parent / "scripts" / "graph_eval.py")
graph_eval = importlib.util.module_from_spec(_SPEC)
sys.modules["graph_eval"] = graph_eval
_SPEC.loader.exec_module(graph_eval)


def test_expansion_excludes_pool_and_ranks_by_score() -> None:
    pool = ["a", "b"]
    community = {"a": 1, "b": 2}
    members = {1: ["a", "x", "y"], 2: ["b", "z"]}
    scores = {"x": 0.2, "y": 0.9, "z": 0.5}
    out = graph_eval.expansion_candidates(pool, community, members, scores)
    assert out == ["y", "z", "x"]  # best cosine first, pool members never repeat
    assert graph_eval.expansion_candidates(pool, community, members, scores, cap=2) == ["y", "z"]


def test_score_case_counts_recall_window() -> None:
    hits = {k: 0 for k in graph_eval.RECALL_KS}
    rr: list = []
    rank = graph_eval.score_case(["x", "gold", "y"], {"gold"}, hits, rr)
    assert rank == 2 and rr == [0.5]
    assert hits[1] == 0 and hits[5] == 1 and hits[20] == 1
    rank = graph_eval.score_case(["x"], {"gold"}, hits, rr)
    assert rank == 0 and rr[-1] == 0.0
