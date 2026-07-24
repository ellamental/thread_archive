"""The consolidated haystack corpus: what flattening 500 per-question haystacks
into one archive has to get right.

``haystack_eval`` keeps each question's history in its own home, where ids only
have to be unique *within* a question. Merging them into a single archive removes
that shelter, and two properties decide whether the merged corpus is the dataset
or a corrupted version of it — a collision silently merges unrelated documents,
and a missed dedup ingests the shared session pool hundreds of times over.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _corpus_module():
    """``evals/haystack_corpus.py``, loaded by path (a script, not a package module)."""
    path = Path(__file__).resolve().parents[1] / "evals" / "haystack_corpus.py"
    spec = importlib.util.spec_from_file_location("haystack_corpus", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _locomo_file(tmp_path: Path, conversations: list[dict]) -> Path:
    repo = tmp_path / "locomo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "locomo10.json").write_text(json.dumps(conversations),
                                                 encoding="utf-8")
    return repo


def _conversation(sample_id: str, turns: list[tuple[str, str]]) -> dict:
    return {
        "sample_id": sample_id,
        "conversation": {"session_1": [
            {"dia_id": did, "speaker": "A", "text": text} for did, text in turns]},
        "qa": [{"question": "q?", "evidence": [turns[0][0]], "category": 1}],
    }


def test_locomo_turn_ids_are_namespaced_by_conversation(tmp_path):
    """``dia_id`` restarts at ``D1:1`` in every conversation, so the raw id is
    unique only inside one. Flattened without the conversation id, ten
    conversations would collapse onto ~588 shared ids and each doc would hold
    whichever text happened to land last."""
    hc = _corpus_module()
    repo = _locomo_file(tmp_path, [
        _conversation("conv-a", [("D1:1", "alpha one"), ("D1:2", "alpha two")]),
        _conversation("conv-b", [("D1:1", "bravo one"), ("D1:2", "bravo two")]),
    ])

    corpus = hc.collect("locomo", _Args(locomo_repo=str(repo), longmemeval_file=""))

    assert len(corpus) == 4, corpus
    assert corpus["conv-a:D1:1"].endswith("alpha one")
    assert corpus["conv-b:D1:1"].endswith("bravo one")


def test_longmemeval_shares_its_session_pool_across_questions(tmp_path):
    """The questions draw haystacks from one pool, so the same session id recurs
    across them — 22,342 haystack slots over 18,191 distinct sessions. Ingesting
    per question would put thousands of duplicate threads in the corpus."""
    hc = _corpus_module()
    shared = {"role": "user", "content": "shared session text"}
    path = tmp_path / "lme.json"
    path.write_text(json.dumps([
        {"question_id": "q1", "question": "one?",
         "haystack_session_ids": ["s-shared", "s-one"],
         "haystack_sessions": [[shared], [{"role": "user", "content": "only in q1"}]],
         "answer_session_ids": ["s-one"]},
        {"question_id": "q2", "question": "two?",
         "haystack_session_ids": ["s-shared", "s-two"],
         "haystack_sessions": [[shared], [{"role": "user", "content": "only in q2"}]],
         "answer_session_ids": ["s-two"]},
    ]), encoding="utf-8")

    corpus = hc.collect("longmemeval", _Args(locomo_repo="", longmemeval_file=str(path)))

    assert sorted(corpus) == ["s-one", "s-shared", "s-two"]
    assert corpus["s-shared"] == "shared session text"


def test_abstention_questions_contribute_no_documents(tmp_path):
    """``_abs`` questions are skipped by the scored harness, so their haystacks are
    not part of the benchmark corpus either."""
    hc = _corpus_module()
    path = tmp_path / "lme.json"
    path.write_text(json.dumps([
        {"question_id": "q1_abs", "question": "abstain?",
         "haystack_session_ids": ["s-abs"],
         "haystack_sessions": [[{"role": "user", "content": "abstention only"}]],
         "answer_session_ids": ["s-abs"]},
    ]), encoding="utf-8")

    assert hc.collect("longmemeval",
                      _Args(locomo_repo="", longmemeval_file=str(path))) == {}


def test_missing_dataset_fails_with_a_pointed_message(tmp_path):
    import pytest

    hc = _corpus_module()
    with pytest.raises(SystemExit, match="locomo data not found"):
        hc.collect("locomo", _Args(locomo_repo=str(tmp_path / "nope"),
                                   longmemeval_file=""))
    with pytest.raises(SystemExit, match="longmemeval file not found"):
        hc.collect("longmemeval", _Args(locomo_repo="",
                                        longmemeval_file=str(tmp_path / "nope.json")))
