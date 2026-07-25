"""Unit coverage for the SWE-chat benchmark export.

The export is the step that turns mined gold into something a stranger can score:
it rewrites cases off this archive's thread ids onto the dataset's own session
ids and writes the queries/qrels/corpus/manifest quartet. What needs coverage is
every way that translation can be silently wrong — a judgment pointing at a
document outside the corpus, a graded pool that lost its grade-0 rows (which
would change nDCG's denominator without changing any visible count), a query id
that collides two different questions into one, a skip-list the exported format
cannot express. Each of those produces a file that scores cleanly and means
something other than it claims.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from thread_archive import _api as ta
from thread_archive._ops.snapshot import stamp_snapshot
from thread_archive._store import use_session

from .helpers import cc_assistant, cc_user, write_jsonl

_SPEC = importlib.util.spec_from_file_location(
    "swechat_bench",
    Path(__file__).resolve().parent.parent / "evals" / "swechat_bench.py",
)
bench = importlib.util.module_from_spec(_SPEC)
sys.modules["swechat_bench"] = bench
_SPEC.loader.exec_module(bench)


# ── query_id ─────────────────────────────────────────────────────────────────

def test_query_id_is_stable_across_calls():
    case = {"protocol": "commit-linked", "repo": "o/r", "query": "retry backoff"}
    assert bench.query_id(case) == bench.query_id(dict(case))


def test_query_id_separates_the_same_question_asked_of_two_repos():
    a = {"protocol": "commit-linked", "repo": "o/one", "query": "retry backoff"}
    b = {"protocol": "commit-linked", "repo": "o/two", "query": "retry backoff"}
    assert bench.query_id(a) != bench.query_id(b)


def test_query_id_separates_protocols_and_carries_a_readable_prefix():
    q = "retry backoff"
    commit = bench.query_id({"protocol": "commit-linked", "query": q})
    topic = bench.query_id({"protocol": "topic-mined", "query": q})
    assert commit != topic
    assert commit.startswith("cm-") and topic.startswith("tp-")


def test_query_id_reads_topic_as_scope_when_there_is_no_repo():
    a = {"protocol": "topic-mined", "topic": "alpha", "query": "q"}
    b = {"protocol": "topic-mined", "topic": "beta", "query": "q"}
    assert bench.query_id(a) != bench.query_id(b)


# ── load_gold ────────────────────────────────────────────────────────────────

def _cases(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_load_gold_globs_case_files_and_skips_detail_sidecars(tmp_path):
    _cases(tmp_path / "commit-cases.jsonl", [{"query": "a"}])
    _cases(tmp_path / "topic-cases-o-r.jsonl", [{"query": "b"}])
    _cases(tmp_path / "commit-cases-detail.jsonl", [{"query": "audit trail"}])
    _cases(tmp_path / "repo-groups.json", [{"query": "not a case file"}])

    assert sorted(c["query"] for c in bench.load_gold(tmp_path)) == ["a", "b"]


def test_load_gold_tags_each_case_with_the_file_it_came_from(tmp_path):
    _cases(tmp_path / "commit-cases.jsonl", [{"query": "a"}])
    (case,) = bench.load_gold(tmp_path)
    assert case["_source_file"] == "commit-cases.jsonl"


# ── corpus_rows ──────────────────────────────────────────────────────────────

def test_corpus_rows_labels_sessions_from_the_groups_file(tmp_path):
    (tmp_path / "repo-groups.json").write_text(json.dumps({"o/r": ["s1"]}))
    rows = bench.corpus_rows(tmp_path, {"T1": "s1", "T2": "s2"})
    assert rows == [{"session_id": "s1", "repo": "o/r"},
                    {"session_id": "s2", "repo": None}]


def test_corpus_rows_survives_a_missing_groups_file(tmp_path):
    assert bench.corpus_rows(tmp_path, {"T1": "s1"}) == [
        {"session_id": "s1", "repo": None}]


# ── dataset_revision ─────────────────────────────────────────────────────────

def test_dataset_revision_reads_the_hub_cache(tmp_path):
    cache = tmp_path / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (cache / "sessions.parquet.metadata").write_text("abc123\nsha256\n1784851534.0\n")
    assert bench.dataset_revision(tmp_path) == "abc123"


def test_dataset_revision_is_none_when_the_download_is_absent(tmp_path):
    assert bench.dataset_revision(tmp_path) is None


# ── export ───────────────────────────────────────────────────────────────────

def _seed(tmp_path, names: list[str]) -> dict[str, str]:
    """Import one session per name with ``source_id`` set, and return
    ``{session_id: thread_id}`` — the map the export has to invert."""
    for name in names:
        f = tmp_path / f"{name}.jsonl"
        write_jsonl(f, [cc_user(name), cc_assistant(name)])
        ta.import_path(f, source_id=name)
    with use_session() as s:
        rows = s.execute(text(
            "SELECT source_id, id FROM threads WHERE thread_type = 'conversation'")).all()
    return {str(sid): str(tid) for sid, tid in rows}


@pytest.fixture
def seeded(tmp_path, archive_home):
    ids = _seed(tmp_path, ["sess-a", "sess-b", "sess-c"])
    snapshot = stamp_snapshot(str(archive_home))["snapshot_id"]
    gold = tmp_path / "gold"
    gold.mkdir()
    (gold / "repo-groups.json").write_text(json.dumps(
        {"o/r": ["sess-a", "sess-b", "sess-c"]}))
    return ids, snapshot, gold


def _read_qrels(out: Path) -> dict[str, dict[str, int]]:
    rows: dict[str, dict[str, int]] = {}
    for line in (out / "qrels.txt").read_text().splitlines():
        qid, _, doc, grade = line.split()
        rows.setdefault(qid, {})[doc] = int(grade)
    return rows


def test_export_rewrites_judgments_onto_session_ids(tmp_path, seeded):
    ids, snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [{
        "query": "retry backoff", "protocol": "commit-linked", "repo": "o/r",
        "snapshot_id": snapshot, "gold": [ids["sess-a"]],
        "grades": {ids["sess-a"]: 2, ids["sess-b"]: 1, ids["sess-c"]: 0},
    }])
    out = tmp_path / "bench"
    bench.export(gold, out, data=tmp_path / "absent")

    (judgments,) = _read_qrels(out).values()
    assert judgments == {"sess-a": 2, "sess-b": 1, "sess-c": 0}


def test_export_keeps_the_grade_zero_rows(tmp_path, seeded):
    """nDCG's ideal ranking comes from the graded pool, so dropping the judged
    non-relevant rows would change the measure while every visible count stayed
    the same."""
    ids, snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [{
        "query": "q", "protocol": "commit-linked", "snapshot_id": snapshot,
        "gold": [ids["sess-a"]],
        "grades": {ids["sess-a"]: 2, ids["sess-c"]: 0},
    }])
    out = tmp_path / "bench"
    bench.export(gold, out, data=tmp_path / "absent")
    assert 0 in next(iter(_read_qrels(out).values())).values()


def test_export_grades_an_ungraded_gold_as_relevant(tmp_path, seeded):
    """Single-gold protocols ship no pool; the gold still has to reach the qrels
    as grade 2 or the case would score as having no relevant document at all."""
    ids, snapshot, gold = seeded
    _cases(gold / "findability-cases.jsonl", [{
        "query": "q", "protocol": "query-gen", "snapshot_id": snapshot,
        "gold": [ids["sess-a"]], "grades": {},
    }])
    out = tmp_path / "bench"
    bench.export(gold, out, data=tmp_path / "absent")
    assert next(iter(_read_qrels(out).values())) == {"sess-a": 2}


def test_export_queries_carry_no_hint_of_the_answer(tmp_path, seeded):
    ids, snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [{
        "query": "retry backoff", "protocol": "commit-linked", "repo": "o/r",
        "difficulty": "intent", "snapshot_id": snapshot,
        "gold": [ids["sess-a"]], "grades": {ids["sess-a"]: 2},
    }])
    out = tmp_path / "bench"
    bench.export(gold, out, data=tmp_path / "absent")

    (query,) = [json.loads(ln) for ln in
                (out / "queries.jsonl").read_text().splitlines()]
    assert query["text"] == "retry backoff" and query["difficulty"] == "intent"
    # A repository is a repository and a subject is a subject; a cross-cutting
    # topic must not arrive labelled as a repo consumers can group by.
    assert query["repo"] == "o/r" and query["topic"] is None
    serialized = json.dumps(query)
    assert not any(t in serialized for t in ids.values())
    assert "sess-a" not in serialized


def test_export_drops_a_case_whose_gold_is_not_in_this_corpus(tmp_path, seeded):
    """A qrels row naming a document outside the corpus is unfindable for every
    system equally, which reads as a hard query rather than a broken export."""
    ids, snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [
        {"query": "keep me", "protocol": "commit-linked", "snapshot_id": snapshot,
         "gold": [ids["sess-a"]], "grades": {ids["sess-a"]: 2}},
        {"query": "drop me", "protocol": "commit-linked", "snapshot_id": snapshot,
         "gold": ["01NOTATHREADINTHISCORPUS0"], "grades": {}},
    ])
    out = tmp_path / "bench"
    manifest = bench.export(gold, out, data=tmp_path / "absent")

    assert manifest["queries"]["total"] == 1
    assert len(manifest["dropped_cases"]) == 1
    assert "drop me" in manifest["dropped_cases"][0]


def test_export_refuses_a_case_carrying_a_skip_list(tmp_path, seeded):
    ids, snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [{
        "query": "q", "protocol": "commit-linked", "snapshot_id": snapshot,
        "gold": [ids["sess-a"]], "grades": {ids["sess-a"]: 2},
        "sessions": [ids["sess-b"]],
    }])
    with pytest.raises(SystemExit, match="skip-list"):
        bench.export(gold, tmp_path / "bench", data=tmp_path / "absent")


def test_export_refuses_gold_mined_against_another_snapshot(tmp_path, seeded):
    ids, _snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [{
        "query": "q", "protocol": "commit-linked", "snapshot_id": "deadbeefdeadbeef",
        "gold": [ids["sess-a"]], "grades": {ids["sess-a"]: 2},
    }])
    with pytest.raises(SystemExit, match="mined against snapshot"):
        bench.export(gold, tmp_path / "bench", data=tmp_path / "absent")


def test_export_separates_repository_topics_from_cross_cutting_subjects(
        tmp_path, seeded):
    """A repository topic's confounds are separable by file and module vocabulary
    and a subject's are not, so the two are different difficulties wearing one
    protocol name. A consumer that cannot split them scores a mixture."""
    ids, snapshot, gold = seeded
    _cases(gold / "topic-cases-o-r.jsonl", [{
        "query": "q1", "protocol": "topic-mined", "topic": "o/r",
        "snapshot_id": snapshot, "gold": [ids["sess-a"]],
        "grades": {ids["sess-a"]: 2},
    }])
    _cases(gold / "topic-cases-styling.jsonl", [{
        "query": "q2", "protocol": "topic-mined", "topic": "Styling and CSS",
        "snapshot_id": snapshot, "gold": [ids["sess-b"]],
        "grades": {ids["sess-b"]: 2},
    }])
    out = tmp_path / "bench"
    bench.export(gold, out, data=tmp_path / "absent")

    kinds = {q["topic"]: q["topic_kind"] for q in
             (json.loads(ln) for ln in
              (out / "queries.jsonl").read_text().splitlines())}
    assert kinds == {"o/r": "repository", "Styling and CSS": "subject"}


def test_export_leaves_topic_kind_unset_for_single_gold_protocols(tmp_path, seeded):
    ids, snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [{
        "query": "q", "protocol": "commit-linked", "repo": "o/r",
        "snapshot_id": snapshot, "gold": [ids["sess-a"]],
        "grades": {ids["sess-a"]: 2},
    }])
    out = tmp_path / "bench"
    bench.export(gold, out, data=tmp_path / "absent")
    (query,) = [json.loads(ln) for ln in
                (out / "queries.jsonl").read_text().splitlines()]
    assert query["topic_kind"] is None


# ── run_lines ────────────────────────────────────────────────────────────────

def _rows(lines: list[str]) -> list[list[str]]:
    return [ln.split() for ln in lines]


def test_run_lines_write_trec_columns_with_dense_one_based_ranks():
    queries = [{"query_id": "cm-1", "text": "q"}]
    hits = [{"thread_id": "T1"}, {"thread_id": "T2"}]
    lines, missing = bench.run_lines(
        queries, lambda _t: hits, {"T1": "s1", "T2": "s2"},
        tag="mine", depth=20, sign=1.0)

    assert missing == 0
    assert [r[:4] + r[5:] for r in _rows(lines)] == [
        ["cm-1", "Q0", "s1", "1", "mine"],
        ["cm-1", "Q0", "s2", "2", "mine"],
    ]


def test_run_lines_negate_a_lower_is_better_ranker():
    """FTS5's ``bm25()`` is lower-is-better; a TREC run's score column is not, and
    a conforming scorer sorts on it. Without the flip the baseline's run is its
    own exact reverse — which scores cleanly and is wrong."""
    hits = [{"thread_id": "T1", "score": -128.8}, {"thread_id": "T2", "score": -53.9}]
    lines, _ = bench.run_lines(
        [{"query_id": "cm-1", "text": "q"}], lambda _t: hits,
        {"T1": "s1", "T2": "s2"}, tag="bm25", depth=20,
        sign=bench._SCORE_SIGN["bm25"])

    scores = [float(r[4]) for r in _rows(lines)]
    assert scores == [128.8, 53.9]
    assert scores == sorted(scores, reverse=True)


def test_run_lines_keep_ranks_dense_when_a_hit_is_outside_the_corpus():
    hits = [{"thread_id": "T1"}, {"thread_id": "TX"}, {"thread_id": "T2"}]
    lines, missing = bench.run_lines(
        [{"query_id": "cm-1", "text": "q"}], lambda _t: hits,
        {"T1": "s1", "T2": "s2"}, tag="mine", depth=20, sign=1.0)

    assert missing == 1
    assert [r[3] for r in _rows(lines)] == ["1", "2"]
    assert [r[2] for r in _rows(lines)] == ["s1", "s2"]


def test_run_lines_truncate_to_the_contract_depth():
    hits = [{"thread_id": f"T{i}"} for i in range(10)]
    to_session = {f"T{i}": f"s{i}" for i in range(10)}
    lines, _ = bench.run_lines(
        [{"query_id": "cm-1", "text": "q"}], lambda _t: hits[:3], to_session,
        tag="mine", depth=3, sign=1.0)
    assert len(lines) == 3


def test_run_lines_emit_nothing_for_a_query_with_no_hits():
    """TREC spells "returned nothing" as absence; the scorer counts the query as a
    miss rather than skipping it, so an empty row set is the correct output."""
    lines, _ = bench.run_lines(
        [{"query_id": "cm-1", "text": "q"}], lambda _t: [], {"T1": "s1"},
        tag="mine", depth=20, sign=1.0)
    assert lines == []


def test_export_manifest_pins_the_corpus_and_the_scoring_contract(tmp_path, seeded):
    ids, snapshot, gold = seeded
    _cases(gold / "commit-cases.jsonl", [{
        "query": "q", "protocol": "commit-linked", "snapshot_id": snapshot,
        "gold": [ids["sess-a"]], "grades": {ids["sess-a"]: 2},
    }])
    out = tmp_path / "bench"
    bench.export(gold, out, data=tmp_path / "absent")

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["snapshot_id"] == snapshot
    assert manifest["corpus"]["documents"] == 3
    assert manifest["scoring"]["relevant_grade"] == 2
    assert manifest["scoring"]["run_depth"] == bench.RUN_DEPTH
    assert "RR(rel=2)" in manifest["scoring"]["measures"]
    # The corpus file is the scope a system may return from, so it lists every
    # ingested session — not only the ones some case happens to judge.
    corpus = [json.loads(ln) for ln in
              (out / "corpus.jsonl").read_text().splitlines()]
    assert {r["session_id"] for r in corpus} == set(ids)
