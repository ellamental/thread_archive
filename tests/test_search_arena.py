"""The arena stays runnable: blind side-mapping, tie short-circuit, duel scoring.

Guards the pairwise judge harness (``evals/search_arena.py``) in the fast
tier with a fake judge — no ``claude`` CLI, no tokens. The real thing it must
get right is attribution: verdicts on blinded, side-randomized lists must map
back to the correct configuration, identical rankings must never spend a judge
call, and a genuinely divergent configuration must reach the judge at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thread_archive._retrieval import SearchParams

from .quality_corpus import build_corpus

EVALS = Path(__file__).resolve().parent.parent / "evals"


def _arena():
    import importlib.util
    import sys

    mod = sys.modules.get("search_arena")
    if mod is None:
        spec = importlib.util.spec_from_file_location("search_arena", EVALS / "search_arena.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["search_arena"] = mod
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def corpus(archive_home):
    return build_corpus(archive_home)


def _experiment(name, params):
    lab = _arena()._lab()
    return lab.Experiment(name, f"test config {name}", lab._params_search(params))


# ── the statistics ───────────────────────────────────────────────────────────

def test_sign_test() -> None:
    arena = _arena()
    assert arena.sign_test_p(0, 0) is None
    assert arena.sign_test_p(5, 5) == pytest.approx(1.0)
    assert arena.sign_test_p(10, 0) == pytest.approx(2 / 2**10)
    assert arena.sign_test_p(3, 7) == pytest.approx(arena.sign_test_p(7, 3))


# ── blind attribution ────────────────────────────────────────────────────────

def test_duel_maps_blinded_verdicts_back_to_the_right_side() -> None:
    """Whatever slot the coin puts the challenger in, a vote for its list must
    come back as 'challenger' — attribution is the harness's whole job."""
    arena = _arena()
    base = [("b-1", "baseline top", "snippet"), ("b-2", "other", "snippet")]
    chal = [("c-1", "challenger top", "snippet"), ("b-2", "other", "snippet")]

    def prefer_challenger(prompt, model):
        first = prompt.split("List 1:")[1].split("List 2:")[0]
        return {"winner": 1 if "challenger top" in first else 2}

    def prefer_baseline(prompt, model):
        first = prompt.split("List 1:")[1].split("List 2:")[0]
        return {"winner": 1 if "baseline top" in first else 2}

    for flip in (False, True):
        assert arena.duel("q", base, chal, model="m", flip=flip,
                          call=prefer_challenger) == "challenger"
        assert arena.duel("q", base, chal, model="m", flip=flip,
                          call=prefer_baseline) == "baseline"
        assert arena.duel("q", base, chal, model="m", flip=flip,
                          call=lambda p, m: {"winner": 0}) == "tie"
        assert arena.duel("q", base, chal, model="m", flip=flip,
                          call=lambda p, m: None) is None
        assert arena.duel("q", base, chal, model="m", flip=flip,
                          call=lambda p, m: {"winner": "garbage"}) is None


def test_identical_rankings_tie_without_a_judge_call() -> None:
    arena = _arena()
    same = [("t-1", "title", "snippet")]

    def explode(prompt, model):
        raise AssertionError("identical rankings must not reach the judge")

    assert arena.duel("q", same, list(same), model="m", flip=True, call=explode) == "tie"


# ── duel runs over the live pipeline ─────────────────────────────────────────

def test_run_duels_spends_judge_calls_only_on_disagreements(corpus) -> None:
    """A challenger that IS the baseline (explicit default params) produces
    identical rankings on every query — all ties, zero judge calls."""
    arena = _arena()
    from thread_archive._retrieval import search as production

    calls = []

    def judge(prompt, model):
        calls.append(prompt)
        return {"winner": 0}

    cases = [{"query": "authentication"}, {"query": "token refresh"},
             {"query": "metrics review"}]
    rep = arena.run_duels(cases, _experiment("same", SearchParams()), production,
                          call=judge)
    assert calls == []
    assert rep["identical"] == len(cases) and rep["ties"] == len(cases)
    assert rep["wins"] == rep["losses"] == 0
    assert rep["win_rate"] is None and rep["sign_test_p"] is None


def test_run_duels_judges_a_divergent_challenger(corpus) -> None:
    """A config that flips 'authentication' to the css decoy diverges from the
    baseline, reaches the (fake) judge, and the verdict lands in the tallies
    with both rankings recorded for the audit trail."""
    arena = _arena()
    from thread_archive._retrieval import search as production

    skewed = _experiment("skewed", SearchParams(content_type_weights={"user": 0.5, "text": 5.0}))
    calls = []

    def judge(prompt, model):
        # Vote for whichever side ranks the css decoy FIRST — that's the skewed
        # config, so a correct harness must attribute the win to the challenger
        # whichever blind slot it landed in.
        calls.append(prompt)
        top_of_list1 = prompt.split("List 1:")[1].split("List 2:")[0].split("\n2.")[0]
        return {"winner": 1 if "sidebar" in top_of_list1 else 2}

    rep = arena.run_duels([{"query": "authentication"}], skewed, production, call=judge)
    assert len(calls) == 1
    assert rep["wins"] == 1 and rep["losses"] == 0 and rep["identical"] == 0
    assert rep["win_rate"] == 1.0
    judged = rep["detail"][0]
    assert judged["verdict"] == "challenger"
    assert judged["baseline"][0] == corpus["auth"]
    assert judged["challenger"][0] == corpus["css-decoy"]


def test_run_duels_counts_judge_failures(corpus) -> None:
    arena = _arena()
    from thread_archive._retrieval import search as production

    skewed = _experiment("skewed2", SearchParams(content_type_weights={"user": 0.5, "text": 5.0}))
    rep = arena.run_duels([{"query": "authentication"}], skewed, production,
                          call=lambda p, m: None)
    assert rep["judge_failures"] == 1
    assert rep["wins"] == rep["losses"] == rep["ties"] == 0
