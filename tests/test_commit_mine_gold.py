"""Unit coverage for the commit-linked gold miner's pure logic.

The agent stage is operator-run and costs real tokens; what needs coverage is the
logic around it — linkage loading (a machine-written file whose bad rows must drop,
not half-mine), repo-stratified sampling (the corpus is power-law skewed, so an
unstratified draw would spend the budget inside one codebase), the structural
grade pool (2 from provenance, 1/0 from file overlap, both capped), query parsing,
and the snapshot binding on each assembled case.
"""

from __future__ import annotations

import json
from pathlib import Path

from thread_archive._mine import commit_linked as cm

# ── load_linkage ─────────────────────────────────────────────────────────────

def _write(tmp_path, rows):
    p = tmp_path / "commit-linkage.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_load_linkage_keeps_complete_rows(tmp_path):
    p = _write(tmp_path, [
        {"session_id": "s1", "thread_id": "T1", "repo": "o/r", "files": ["a.py"],
         "commits": [{"sha": "abc", "message": "fix the thing", "patch": "@@"}]},
    ])
    (row,) = cm.load_linkage(p)
    assert row["thread_id"] == "T1" and row["repo"] == "o/r"
    assert row["files"] == ["a.py"] and row["commits"][0]["sha"] == "abc"


def test_load_linkage_drops_incomplete_and_malformed_rows(tmp_path):
    p = _write(tmp_path, [
        {"thread_id": "T1", "repo": "o/r", "commits": [{"message": "ok"}]},   # keep
        {"thread_id": "T2", "repo": "o/r", "commits": []},                    # no commits
        {"thread_id": "T3", "repo": "o/r", "commits": [{"message": "  "}]},   # blank message
        {"thread_id": "T4", "commits": [{"message": "ok"}]},                  # no repo
        {"repo": "o/r", "commits": [{"message": "ok"}]},                      # no thread
    ])
    p.write_text(p.read_text() + "not json\n")
    assert [r["thread_id"] for r in cm.load_linkage(p)] == ["T1"]


def test_load_linkage_missing_file_is_a_pointed_error(tmp_path):
    import pytest

    with pytest.raises(SystemExit, match="no linkage file"):
        cm.load_linkage(tmp_path / "absent.jsonl")


# ── sample_units ─────────────────────────────────────────────────────────────

def _unit(tid, repo, files=()):
    return {"thread_id": tid, "repo": repo, "session_id": tid,
            "files": list(files), "commits": [{"message": "m"}]}


def test_sample_units_caps_each_repo():
    rows = [_unit(f"T{i}", "big") for i in range(20)]
    rows += [_unit(f"S{i}", f"small{i}") for i in range(5)]
    got = cm.sample_units(rows, n=10, seed=7, skip=set(), per_repo=2)
    repos = [r["repo"] for r in got]
    assert repos.count("big") == 2          # the dominant repo is held to per_repo
    assert len(got) == 7                    # 2 from big + one each from 5 smalls


def test_sample_units_is_deterministic_and_honors_skip():
    rows = [_unit(f"T{i}", f"r{i}") for i in range(12)]
    a = cm.sample_units(rows, n=5, seed=3, skip=set(), per_repo=3)
    b = cm.sample_units(rows, n=5, seed=3, skip=set(), per_repo=3)
    assert [r["thread_id"] for r in a] == [r["thread_id"] for r in b]
    skipped = cm.sample_units(rows, n=5, seed=3, skip={a[0]["thread_id"]}, per_repo=3)
    assert a[0]["thread_id"] not in {r["thread_id"] for r in skipped}


# ── build_grades ─────────────────────────────────────────────────────────────

def test_build_grades_scores_gold_two_overlap_one_disjoint_zero():
    unit = _unit("T1", "o/r", ["src/a.py", "src/b.py"])
    siblings = [unit,
                _unit("T2", "o/r", ["src/b.py"]),        # shares a file -> partial
                _unit("T3", "o/r", ["docs/x.md"]),       # disjoint -> confound
                _unit("T4", "o/r", [])]                  # no files -> confound
    grades = cm.build_grades(unit, siblings, seed=7)
    assert grades["T1"] == 2
    assert grades["T2"] == 1
    assert grades["T3"] == 0 and grades["T4"] == 0


def test_build_grades_caps_the_pool():
    unit = _unit("T1", "o/r", ["shared.py"])
    siblings = [unit]
    siblings += [_unit(f"P{i}", "o/r", ["shared.py"]) for i in range(20)]
    siblings += [_unit(f"C{i}", "o/r", ["other.py"]) for i in range(40)]
    grades = cm.build_grades(unit, siblings, seed=7)
    assert sum(1 for g in grades.values() if g == 1) == cm.MAX_PARTIAL
    assert sum(1 for g in grades.values() if g == 0) == cm.MAX_CONFOUND
    assert grades["T1"] == 2


def test_build_grades_is_deterministic_per_gold():
    unit = _unit("T1", "o/r", ["a.py"])
    siblings = [unit] + [_unit(f"C{i}", "o/r", ["z.py"]) for i in range(30)]
    assert cm.build_grades(unit, siblings, 7) == cm.build_grades(unit, siblings, 7)


def test_build_grades_with_no_siblings_is_gold_only():
    unit = _unit("T1", "o/r", ["a.py"])
    assert cm.build_grades(unit, [unit], seed=7) == {"T1": 2}


# ── parse_queries ────────────────────────────────────────────────────────────

def test_parse_queries_keeps_one_per_tier_and_drops_malformed():
    v = cm.parse_queries(
        'sure: {"queries": ['
        '{"query": "retry backoff in uploader", "difficulty": "literal"}, '
        '{"query": "second literal", "difficulty": "literal"}, '     # tier taken
        '{"query": "", "difficulty": "intent"}, '                    # empty
        '{"query": "x", "difficulty": "nope"}, '                     # bad tier
        '{"query": "stop retrying forever", "difficulty": "intent"}]}')
    assert [q["difficulty"] for q in v["queries"]] == ["literal", "intent"]
    assert v["queries"][0]["query"] == "retry backoff in uploader"


def test_parse_queries_accepts_empty_and_rejects_junk():
    assert cm.parse_queries('{"queries": [], "note": "trivial bump"}')["queries"] == []
    assert cm.parse_queries("no json here") is None
    assert cm.parse_queries('{"note": "no queries key"}') is None


# ── case assembly ────────────────────────────────────────────────────────────

def test_cases_from_queries_binds_snapshot_gold_and_pool():
    unit = {"thread_id": "T1", "repo": "o/r", "files": [],
            "commits": [{"sha": "abc123", "message": "m"}]}
    grades = {"T1": 2, "T2": 1, "T3": 0}
    rows = cm.cases_from_queries(
        unit, [{"query": "q", "difficulty": "functional"}], grades, "snap-9")
    (row,) = rows
    assert row["gold"] == ["T1"] and row["grades"] == grades
    assert row["snapshot_id"] == "snap-9" and row["protocol"] == "commit-linked"
    assert row["difficulty"] == "functional" and row["repo"] == "o/r"
    assert row["commit_shas"] == ["abc123"] and row["sessions"] == []
    # the pool travels by value: mutating the case must not disturb the caller's
    row["grades"]["T2"] = 2
    assert grades["T2"] == 1


def test_build_prompt_carries_the_diff_and_never_the_thread():
    unit = {"thread_id": "T1", "repo": "o/r", "files": ["src/up.py"],
            "commits": [{"sha": "abc", "message": "cap the retries",
                         "patch": "@@ -1 +1 @@\n-while True\n+for _ in range(3)"}]}
    p = cm.build_prompt(unit)
    assert "cap the retries" in p and "for _ in range(3)" in p and "o/r" in p
    # the gold thread id must never reach the author — the query is written from
    # the commit alone, which is what keeps the label non-circular
    assert "T1" not in p


def test_build_prompt_truncates_a_huge_diff():
    unit = {"thread_id": "T1", "repo": "o/r", "files": [],
            "commits": [{"sha": "a", "message": "m", "patch": "x" * 50_000}]}
    assert len(cm.build_prompt(unit)) < cm.MAX_PATCH_CHARS + 4000


# ── the miner's run path ─────────────────────────────────────────────────────

def test_commit_miner_run_writes_cases(archive_home, tmp_path):
    import argparse

    from thread_archive._mine import _framework as fw
    from thread_archive._store import Thread, get_session, init_db

    init_db()
    with get_session() as s:
        gold = Thread(name="c:gold", title="the session that made the change",
                      thread_type="conversation", source="cc", source_id="s1")
        sib = Thread(name="c:sib", title="a sibling in the same repo",
                     thread_type="conversation", source="cc", source_id="s2")
        s.add_all([gold, sib])
        s.flush()
        gid, sid = gold.id, sib.id
        s.commit()

    linkage = _write(tmp_path, [
        {"session_id": "s1", "thread_id": gid, "repo": "o/r", "files": ["a.py"],
         "commits": [{"sha": "abc", "message": "cap retries", "patch": "@@"}]},
        {"session_id": "s2", "thread_id": sid, "repo": "o/r", "files": ["b.py"],
         "commits": [{"sha": "def", "message": "unrelated", "patch": "@@"}]},
        # a stale row: names a thread that is not in this corpus at all
        {"session_id": "s9", "thread_id": "GONE", "repo": "o/r", "files": [],
         "commits": [{"sha": "xyz", "message": "withdrawn", "patch": "@@"}]},
    ])
    out = tmp_path / "commit-cases.jsonl"
    reply = json.dumps({"queries": [
        {"query": "where did we cap the upload retries", "difficulty": "intent"}]})

    ns = argparse.Namespace(model="opus", jobs=1, seed=7, out=out, target=1,
                            linkage=linkage, per_repo=5)
    ctx = fw.MineContext(snapshot_id="snap-1", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=ns,
                         agent_run=lambda *a, **k: (reply, {"num_turns": 1}))
    result = cm.MINER.run(ctx)

    assert result.written == 1 and result.attempted == 1
    (row,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert row["miner"] == "commit" and row["snapshot_id"] == "snap-1"
    assert row["gold"][0] in (gid, sid)
    assert row["grades"][row["gold"][0]] == 2
    assert "GONE" not in row["grades"]                 # stale row never sampled
    assert len(row["prompt_sha"]) == 12 and "miner_commit" in row
    assert any("absent from this snapshot" in n for n in result.notes)


def test_commit_miner_is_excluded_from_mine_all():
    # `mine all` drives only miners a count alone can run; this one needs a linkage
    # file an ordinary archive has no reason to carry.
    assert cm.MINER.runnable_in_all is False


def test_commit_miner_is_registered():
    from thread_archive._mine import load_registry

    assert "commit" in {m.name for m in load_registry()}


# ── the SWE-chat corpus builder ──────────────────────────────────────────────

def _corpus_module():
    """``search_lab/swechat_corpus.py``, loaded by path (a script, not a package
    module) — the same way test_mine_framework loads the gold gate."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "search_lab" / "swechat_corpus.py"
    spec = importlib.util.spec_from_file_location("swechat_corpus", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_transcript_detector_accepts_jsonl_and_rejects_pretty_printed(tmp_path):
    """SWE-chat mixes providers under one .jsonl extension: OpenCode sessions are
    pretty-printed JSON objects, which the line-stream importer can only reject
    after logging a parse error per line (2.5M across a full build)."""
    sc = _corpus_module()

    cc = tmp_path / "cc.jsonl"
    cc.write_text(json.dumps({"type": "user", "sessionId": "s1",
                              "parentUuid": None}) + "\n")
    assert sc.is_claude_code_transcript(cc)

    pretty = tmp_path / "ses_x.jsonl"
    pretty.write_text('{\n  "info": {\n    "id": "ses_x"\n  }\n}\n')
    assert not sc.is_claude_code_transcript(pretty)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n")
    assert not sc.is_claude_code_transcript(empty)

    absent = tmp_path / "nope.jsonl"
    assert not sc.is_claude_code_transcript(absent)


def test_choose_sessions_caps_each_repo_and_prefers_linked():
    """A budget must not land inside one codebase. SWE-chat's largest repo holds 870
    of 5851 sessions, so taking repos whole would spend a modest budget on a single
    project and leave a benchmark that measures search over that project."""
    sc = _corpus_module()
    by_repo = {"o/big": [f"B{i}" for i in range(500)]}
    for r in range(5):
        by_repo[f"o/s{r}"] = [f"S{r}_{i}" for i in range(20)]
    # every small repo's first session is commit-linked; the big repo has none
    linked = {f"S{r}_0" for r in range(5)}

    keep = sc.choose_sessions(by_repo, linked, budget=100, per_repo=10)
    assert len(keep) == 60                       # 6 repos, each held to the cap
    for members in by_repo.values():
        assert len(keep & set(members)) <= 10    # no repo exceeds the cap
    assert linked <= keep                        # linked sessions kept first


def test_choose_sessions_spends_the_budget_on_the_richest_repos():
    """Ranking is by commit-linked yield: a budget too small for every repo must go
    where cases can actually be mined."""
    sc = _corpus_module()
    by_repo = {"rich": ["r1", "r2"], "poor": ["p1", "p2"]}
    keep = sc.choose_sessions(by_repo, {"r1", "r2"}, budget=2, per_repo=10)
    assert keep == {"r1", "r2"}


def test_select_corpus_without_a_budget_takes_everything():
    sc = _corpus_module()
    assert sc.select_corpus(Path("/nowhere"), budget=0) is None


def test_as_list_normalizes_json_string_columns():
    """SWE-chat stores list columns as JSON strings in some tables and real lists
    in others; both must read the same."""
    sc = _corpus_module()
    assert sc._as_list('["a", "b"]') == ["a", "b"]
    assert sc._as_list(["a"]) == ["a"]
    assert sc._as_list("") == [] and sc._as_list(None) == []
    assert sc._as_list("not json") == [] and sc._as_list('{"k": 1}') == []
