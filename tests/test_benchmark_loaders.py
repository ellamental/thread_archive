"""The loaders that turn a downloaded benchmark into (corpus, queries, gold).

A loader is the one place a benchmark can be wrong without anything looking
wrong: a mis-resolved gold id scores as a ranking failure, a dropped question
shrinks the denominator, and both produce a plausible number with no error. Every
check here is on real dataset structure — the shapes taken from the shipped
files — because these are the assertions that would have caught the mistakes the
datasets actually invite:

- BEAM's ``source_chat_ids`` arrives as a list, as a dict of named lists, and
  occasionally absent, and a loader that reads only the first shape silently
  drops a third of its gold.
- PerLTQA's profile questions reference a *field name* rather than a unit id, so
  they need the protagonist mapping; the rest reference the block's own id.
- MTRAG ships role markers inside the query text and qrels ids that look like
  they need offset-stripping but do not.

No network and no model: each test builds the structure it needs on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from search_lab import haystack_eval, mtrag_eval, perltqa_eval

# ── MTRAG ────────────────────────────────────────────────────────────────────


def test_role_markers_are_stripped_from_a_query() -> None:
    # `|user|:` is a serialization artifact of the conversation export, not part
    # of the information need — a dense arm would embed it as content.
    assert mtrag_eval.strip_roles("|user|: does IBM offer document databases?") == \
        "does IBM offer document databases?"


def test_a_multi_turn_query_collapses_to_one_line() -> None:
    # The `questions` variant separates turns with newlines; left in, one query
    # would span four lines of the per-query report.
    text = "|user|: first?\n|user|: second?\n|assistant|: an answer"
    assert mtrag_eval.strip_roles(text) == "first? second? an answer"


def test_queries_load_with_ids_intact(tmp_path: Path) -> None:
    # The id carries a `<::>` conversation/turn separator, which must survive to
    # match the qrels.
    path = tmp_path / "q.jsonl"
    path.write_text(
        json.dumps({"_id": "abc<::>2", "text": "|user|: is there a limit?"}) + "\n"
        + json.dumps({"_id": "abc<::>3", "text": "|user|:   "}) + "\n")
    queries = mtrag_eval.load_queries(path)

    assert queries == {"abc<::>2": "is there a limit?"}, "an empty query is dropped"


def test_qrels_map_straight_onto_passage_ids(tmp_path: Path) -> None:
    # The benchmark's README describes stripping two trailing offsets from a
    # qrels corpus-id. That applies to the document-level corpus; against the
    # passage-level corpus this harness uses, the ids match directly — verified
    # 578/578, 494/494, 535/535 and 521/521 across the four domains. Stripping
    # would map every judgment to nothing and score a flat zero.
    path = tmp_path / "dev.tsv"
    path.write_text("query-id\tcorpus-id\tscore\n"
                    "q1\tibmcld_00474-7885-8455\t1\n"
                    "q1\tibmcld_00513-7-2197\t1\n"
                    "q2\tibmcld_00526-7-1750\t0\n")
    qrels = mtrag_eval.load_qrels(path)

    assert qrels == {"q1": {"ibmcld_00474-7885-8455": 1, "ibmcld_00513-7-2197": 1}}


def test_only_the_two_published_query_forms_carry_a_reference() -> None:
    # The paper reports last-turn and query-rewrite. A `questions` run lands with
    # no reference rather than borrowing one from a different query form.
    assert set(mtrag_eval.REFERENCE) == {"lastturn", "rewrite"}
    assert mtrag_eval.REFERENCE["rewrite"]["bm25"] > \
        mtrag_eval.REFERENCE["lastturn"]["bm25"], "rewriting is supposed to help"


# ── BEAM ─────────────────────────────────────────────────────────────────────


def _beam_parquet(path: Path, probing: dict, chat: list) -> Path:
    """One BEAM conversation as a real parquet, in the shipped schema —
    ``probing_questions`` is a Python-repr string, not JSON."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({
        "conversation_id": ["c1"],
        "chat": [chat],
        "probing_questions": [repr(probing)],
    })
    pq.write_table(table, path)
    return path


def _chat(n: int) -> list:
    """One session of ``n`` alternating messages, ids 0..n-1."""
    return [[{"id": i, "index": None, "role": "user" if i % 2 == 0 else "assistant",
              "content": f"message {i}", "question_type": None, "time_anchor": None}
             for i in range(n)]]


def test_beam_reads_gold_from_a_flat_id_list(tmp_path: Path) -> None:
    path = _beam_parquet(tmp_path / "t.parquet",
                         {"information_extraction": [
                             {"question": "when?", "source_chat_ids": [2, 4]}]},
                         _chat(6))
    (_gid, corpus, queries), = haystack_eval.beam_groups(path, "100K")

    assert len(corpus) == 6
    assert queries[0]["gold"] == {"2", "4"}
    assert queries[0]["category"] == "information_extraction"


def test_beam_flattens_dict_shaped_gold(tmp_path: Path) -> None:
    # `temporal_reasoning` splits its ids into named events. A loader that reads
    # only the list shape drops every question in this category.
    path = _beam_parquet(tmp_path / "t.parquet",
                         {"temporal_reasoning": [
                             {"question": "how long?",
                              "source_chat_ids": {"first_event": [2],
                                                  "second_event": [0, 3]}}]},
                         _chat(6))
    (_gid, _corpus, queries), = haystack_eval.beam_groups(path, "100K")

    assert queries[0]["gold"] == {"0", "2", "3"}


def test_beam_indexes_both_roles(tmp_path: Path) -> None:
    # A quarter of the shipped gold ids are assistant messages — the answer to
    # "what did I decide about X" usually lives in the reply. A user-only corpus
    # would put them out of reach and score the miss as a ranking failure.
    path = _beam_parquet(tmp_path / "t.parquet",
                         {"knowledge_update": [
                             {"question": "q", "source_chat_ids": [1]}]},
                         _chat(4))
    (_gid, corpus, queries), = haystack_eval.beam_groups(path, "100K")

    assert corpus["1"].startswith("assistant:")
    assert queries[0]["gold"] == {"1"}


def test_beam_drops_abstention_and_goldless_questions(tmp_path: Path) -> None:
    # Abstention questions are deliberately unanswerable from the conversation
    # and carry no source ids, so there is nothing to retrieve. Five questions
    # across the 500K and 1M tiers are missing the field entirely.
    path = _beam_parquet(tmp_path / "t.parquet", {
        "abstention": [{"question": "unanswerable?", "ideal_response": "no info"}],
        "event_ordering": [{"question": "no ids?", "source_chat_ids": None},
                           {"question": "real?", "source_chat_ids": [1]}],
    }, _chat(4))
    (_gid, _corpus, queries), = haystack_eval.beam_groups(path, "100K")

    assert [q["text"] for q in queries] == ["real?"]


def test_beam_clips_gold_to_ids_that_resolve(tmp_path: Path) -> None:
    # An id naming no message would otherwise sit in the gold set as a case
    # nothing can satisfy, scoring a permanent miss against the ranker.
    path = _beam_parquet(tmp_path / "t.parquet",
                         {"multi_session_reasoning": [
                             {"question": "q", "source_chat_ids": [1, 999]}]},
                         _chat(4))
    (_gid, _corpus, queries), = haystack_eval.beam_groups(path, "100K")

    assert queries[0]["gold"] == {"1"}


def test_beam_group_ids_carry_the_tier(tmp_path: Path) -> None:
    # The per-corpus home is keyed on the group id, so two tiers sharing a
    # conversation id would otherwise collide on one cached build.
    path = _beam_parquet(tmp_path / "t.parquet",
                         {"summarization": [{"question": "q",
                                             "source_chat_ids": [1]}]},
                         _chat(4))
    ids = {gid for gid, _c, _q in haystack_eval.beam_groups(path, "1M")}

    assert ids == {"1M:c1"}


# ── PerLTQA ──────────────────────────────────────────────────────────────────


def _perltqa_files(tmp_path: Path) -> tuple[Path, Path]:
    """A two-bank memory file and a QA file over it, in the shipped shapes."""
    mem = [
        {"profile": {"Protagonist": "Wang Xiaoming", "Gender": "male"},
         "profile_description": "An engineer.",
         "social_relationship": {"1_0": {"Supporting Characters": "Wang Xiaohong",
                                         "Relationship": "elder sister"}},
         "events": {"1_0_0": {"content": "They visited the Grand Canyon."}},
         "dialogues": {"1_0_0#0": {"events": "1_0_0",
                                   "contents": {"2022-05-12 08:00": ["A: hello",
                                                                     "B: hi"]}}}},
        {"profile": {"Protagonist": "Zhang Xiaohong", "Gender": "female"},
         "profile_description": "A chemical engineer.",
         "social_relationship": {}, "events": {}, "dialogues": {}},
    ]
    qa = [{"Zhang Xiaohong": {"profile": [
               {"Question": "What is Zhang Xiaohong's gender?",
                "Answer": "female", "Reference Memory": "Gender"}]}},
          {"Wang Xiaoming": {
              "events": [{"1_0_0": [{"Question": "Where did they go?",
                                     "Answer": "The Grand Canyon.",
                                     "Reference Memory": "['1_0_0']"}]}],
              "dialogues": [{"1_0_0#0": [{"Question": "How did they greet?",
                                          "Answer": "hello",
                                          "Reference Memory": "['1_0_0#0']"}]}]}}]
    mem_path, qa_path = tmp_path / "mem.json", tmp_path / "qa.json"
    mem_path.write_text(json.dumps(mem))
    qa_path.write_text(json.dumps(qa))
    return mem_path, qa_path


def test_perltqa_corpus_spans_every_memory_type(tmp_path: Path) -> None:
    mem_path, _qa = _perltqa_files(tmp_path)
    corpus, type_of, bank_of = perltqa_eval.load_corpus(mem_path)

    assert set(corpus) == {"profile:0", "profile:1", "1_0", "1_0_0", "1_0_0#0"}
    assert type_of["1_0_0#0"] == "dialogues"
    assert bank_of == {"Wang Xiaoming": 0, "Zhang Xiaohong": 1}


def test_perltqa_profile_text_carries_fields_and_prose(tmp_path: Path) -> None:
    # The questions ask across both — "what is X's gender" is answered by a field
    # and "what did X study" by the description.
    mem_path, _qa = _perltqa_files(tmp_path)
    corpus, _t, _b = perltqa_eval.load_corpus(mem_path)

    assert "Gender: male" in corpus["profile:0"]
    assert "An engineer." in corpus["profile:0"]


def test_perltqa_dialogue_text_keeps_its_timestamps(tmp_path: Path) -> None:
    mem_path, _qa = _perltqa_files(tmp_path)
    corpus, _t, _b = perltqa_eval.load_corpus(mem_path)

    assert "2022-05-12 08:00 A: hello" in corpus["1_0_0#0"]


def test_perltqa_gold_ids_parse_from_a_stringified_list() -> None:
    # `Reference Memory` ships as a repr, not as JSON.
    assert perltqa_eval._gold_ids("['4_0_0']") == ["4_0_0"]
    assert perltqa_eval._gold_ids("['a', 'b']") == ["a", "b"]
    assert perltqa_eval._gold_ids("Gender") == ["Gender"]


def test_perltqa_profile_question_resolves_to_its_protagonists_record(
        tmp_path: Path) -> None:
    # A profile question names a *field*, not a unit id, so its gold can only be
    # found through the protagonist mapping. Getting this wrong scores every
    # profile question as a miss and looks exactly like a weak ranker.
    mem_path, qa_path = _perltqa_files(tmp_path)
    corpus, _t, bank_of = perltqa_eval.load_corpus(mem_path)
    queries = perltqa_eval.load_queries(qa_path, corpus, bank_of)

    profile = [q for q in queries if q["category"] == "profile"]
    assert len(profile) == 1
    assert profile[0]["gold"] == {"profile:1"}
    assert "Zhang Xiaohong" in corpus["profile:1"]


def test_perltqa_keyed_questions_resolve_to_their_block_id(tmp_path: Path) -> None:
    mem_path, qa_path = _perltqa_files(tmp_path)
    corpus, _t, bank_of = perltqa_eval.load_corpus(mem_path)
    queries = perltqa_eval.load_queries(qa_path, corpus, bank_of)

    gold = {q["category"]: q["gold"] for q in queries}
    assert gold["events"] == {"1_0_0"}
    assert gold["dialogues"] == {"1_0_0#0"}


def test_perltqa_drops_a_question_whose_gold_is_absent(tmp_path: Path) -> None:
    # A case nothing can satisfy is a miss recorded against the ranker for the
    # dataset's bookkeeping, not for anything the ranker did.
    mem_path, qa_path = _perltqa_files(tmp_path)
    qa_path.write_text(json.dumps([{"Wang Xiaoming": {"events": [
        {"9_9_9": [{"Question": "unknown unit?", "Reference Memory": "['9_9_9']"}]}]}}]))
    corpus, _t, bank_of = perltqa_eval.load_corpus(mem_path)

    assert perltqa_eval.load_queries(qa_path, corpus, bank_of) == []


@pytest.mark.parametrize("memory_type,unit,expected", [
    ("events", {"content": "  a trip  "}, "a trip"),
    ("social_relationship", {"Relationship": "sister", "Description": ""},
     "Relationship: sister"),
])
def test_perltqa_unit_text_keeps_the_field_each_type_is_asked_about(
        memory_type: str, unit: dict, expected: str) -> None:
    assert perltqa_eval.unit_text(memory_type, unit) == expected
