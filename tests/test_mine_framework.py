"""The gold-mining framework and the two pool-free miners' pure logic.

The agents are operator-run and cost real tokens; covered here is everything
around them — the registry contract every miner must satisfy, the shared helpers
(gold validation, resume dedupe, output-path convention that keeps files
discoverable by the gold gate), the CLI list/dispatch surface, and the parse /
prompt / case-assembly logic of the rerank judge and the query generator.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from pathlib import Path

from search_lab.mine import (  # noqa: E402
    _cli,
    load_registry,
    querygen,
    rerank_judged,
)
from search_lab.mine import (  # noqa: E402
    _framework as fw,
)

REPO = Path(__file__).resolve().parent.parent

# The gold gate's discovery filter — the miners must produce files it keeps and
# sidecars it drops. Loaded by path (a script, not a package module).
_SPEC = importlib.util.spec_from_file_location(
    "retrieval_gold_gate", REPO / "scripts" / "retrieval_gold_gate.py")
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["retrieval_gold_gate"] = gate
_SPEC.loader.exec_module(gate)


# ── registry contract ────────────────────────────────────────────────────────

def test_registry_names_are_unique_and_populated():
    reg = load_registry()
    names = [m.name for m in reg]
    assert names == ["query", "topic", "rerank", "querygen", "commit"]
    assert len(set(names)) == len(names)


def test_every_miner_declares_the_descriptor():
    for m in load_registry():
        assert m.name and m.summary and m.measures and m.unit and m.cost
        assert m.target_kind in ("per-case", "batch")
        assert m.target_help
        assert isinstance(m.runnable_in_all, bool)


def test_every_miner_output_is_gold_discoverable_and_detail_is_not():
    """A miner's default case file must pass the gold gate's discovery filter (so
    the CI floor and the baseline sweep see it), and its detail sidecar must not
    (it is not a scorable gold file)."""
    for m in load_registry():
        # topic embeds a slug; substitute a concrete one for the check.
        stem = m.cases_stem.replace("<slug>", "alpha")
        case_name = f"{stem}.jsonl"
        detail_name = fw.detail_path_for(Path(case_name)).name
        assert "cases" in case_name
        assert not any(mk in case_name for mk in gate._NON_GOLD_MARKERS), case_name
        assert any(mk in detail_name for mk in gate._NON_GOLD_MARKERS), detail_name


def test_miner_case_files_are_discovered_by_the_gate(tmp_path):
    """End to end against the gate's own discovery: every miner's real default
    basename is kept, every detail sidecar is dropped."""
    for m in load_registry():
        stem = m.cases_stem.replace("<slug>", "alpha")
        (tmp_path / f"{stem}.jsonl").write_text("{}\n")
        (tmp_path / fw.detail_path_for(Path(f"{stem}.jsonl")).name).write_text("{}\n")
    found = {p.name for p in gate.discover_gold_files(tmp_path)}
    assert found == {"judged-cases.jsonl", "topic-cases-alpha.jsonl",
                     "rerank-cases.jsonl", "findability-cases.jsonl",
                     "commit-cases.jsonl"}


def test_only_topic_is_excluded_from_mine_all():
    by_name = {m.name: m for m in load_registry()}
    runnable = {n for n, m in by_name.items()
                if m.runnable_in_all and m.target_kind == "per-case"}
    assert runnable == {"query", "rerank", "querygen"}
    assert by_name["topic"].runnable_in_all is False


# ── shared helpers ───────────────────────────────────────────────────────────

def test_validate_gold_resolves_skips_sessions_and_dedupes():
    known = {"a", "b", "sess"}
    resolve = lambda ref: ref if ref in known else None  # noqa: E731
    out = fw.validate_gold(["a", "ghost", "sess", "b", "a"],
                           sessions={"sess"}, resolve=resolve)
    assert out == ["a", "b"]


def test_mined_queries_and_gold_ids_read_existing(tmp_path):
    f = tmp_path / "rerank-cases.jsonl"
    f.write_text('{"query": "a", "gold": ["T1", "T2"]}\n'
                 'junk\n'
                 '{"query": "b", "gold": ["T2", "T3"]}\n')
    assert fw.mined_queries(f) == {"a", "b"}
    assert fw.mined_gold_ids(f) == {"T1", "T2", "T3"}
    assert fw.mined_queries(tmp_path / "absent.jsonl") == set()
    assert fw.mined_gold_ids(tmp_path / "absent.jsonl") == set()


def test_default_paths_stay_under_the_gold_dir_and_pair_a_detail(tmp_path):
    p = fw.default_cases_path("rerank-cases")
    assert p.name == "rerank-cases.jsonl"
    assert p.parent == fw.gold_dir()
    assert fw.detail_path_for(p).name == "rerank-cases-detail.jsonl"


def test_open_output_honors_an_override(tmp_path):
    override = tmp_path / "nested" / "custom.jsonl"
    cases, detail = fw.open_output(override, fw.default_cases_path("x-cases"))
    assert cases == override and detail.name == "custom-detail.jsonl"
    assert cases.parent.is_dir()  # created


def test_tool_cmd_is_an_absolute_cwd_independent_seam():
    """The agents run this through Bash from whatever directory their session
    started in, and it is also their allowlist prefix — so it has to be the running
    interpreter plus an absolute path to the entry point, and nothing else."""
    import sys

    cmd = fw.tool_cmd()
    assert cmd.startswith(sys.executable + " ")
    entry = pathlib.Path(cmd[len(sys.executable) + 1:-len(" tool")])
    assert entry.is_absolute() and entry.is_file()
    assert entry.parts[-3:] == ("search_lab", "mine", "__main__.py")
    assert cmd.endswith(" tool")


def test_clamp_jobs_caps_at_the_concurrency_ceiling():
    from search_lab.mine._agent import MAX_CONCURRENT_SESSIONS

    assert fw.clamp_jobs(1) == 1
    assert fw.clamp_jobs(MAX_CONCURRENT_SESSIONS) == MAX_CONCURRENT_SESSIONS
    assert fw.clamp_jobs(100) == MAX_CONCURRENT_SESSIONS
    assert fw.clamp_jobs(0) == 1  # never spawn zero workers


def test_clamp_sessions_caps_at_the_per_run_ceiling():
    assert fw.clamp_sessions(3) == 3
    assert fw.clamp_sessions(fw.MAX_SESSIONS_PER_RUN) == fw.MAX_SESSIONS_PER_RUN
    assert fw.clamp_sessions(1000) == fw.MAX_SESSIONS_PER_RUN
    assert fw.clamp_sessions(0) == 0  # a batch miner carries no target to bound


def test_casewriter_stamps_miner_provenance(tmp_path):
    cases = tmp_path / "rerank-cases.jsonl"
    w = fw.CaseWriter("rerank", cases, fw.detail_path_for(cases))
    w.write_case({"query": "q", "gold": ["A"]})
    w.write_detail({"query": "q", "outcome": "ok"})
    import json
    row = json.loads(cases.read_text().splitlines()[0])
    assert row["miner"] == "rerank"
    assert fw.detail_path_for(cases).exists()


# ── CLI surface ──────────────────────────────────────────────────────────────

def test_list_view_names_every_miner_and_marks_mine_all():
    text = _cli.list_miners_text(load_registry())
    for name in ("query", "topic", "rerank", "querygen"):
        assert name in text
    assert "● mine all" in text and "○ direct" in text


def test_dispatch_bare_lists(capsys):
    assert _cli.dispatch([]) == 0
    assert "Gold miners" in capsys.readouterr().out


def test_dispatch_unknown_miner_is_a_usage_error(capsys):
    assert _cli.dispatch(["nope"]) == 2
    assert "unknown miner" in capsys.readouterr().err


def test_allocate_splits_the_sweep_budget_across_miners():
    from search_lab.mine._cli import _allocate

    # Fits under budget: every miner gets its full target.
    assert _allocate(5, 3, 25) == [5, 5, 5]
    # Over budget: even split, the remainder favoring the earlier miners.
    assert _allocate(20, 3, 25) == [9, 8, 8]
    assert _allocate(100, 2, 25) == [13, 12]
    # However wide the sweep, the total never passes the budget.
    assert sum(_allocate(1000, 4, 25)) == 25
    assert _allocate(5, 0, 25) == []  # nothing runnable


# ── rerank judge: parse + prompt ─────────────────────────────────────────────

def test_parse_rerank_keeps_only_pool_ids_and_valid_grades():
    pool_ids = {"A", "B", "C"}
    v = rerank_judged.parse_rerank(
        'verdict: {"grades": {"A": 2, "B": 0, "C": 7, "D": 2}, '
        '"none_answer": false, "rationale": "A answers"}', pool_ids)
    # C's grade is out of range, D is not in the pool — both dropped.
    assert v["grades"] == {"A": 2, "B": 0}
    assert v["none"] is False


def test_parse_rerank_flags_none_of_pool():
    v = rerank_judged.parse_rerank(
        '{"grades": {"A": 0, "B": 1}, "none_answer": true}', {"A", "B"})
    assert v["none"] is True
    assert 2 not in v["grades"].values()  # nothing answered


def test_parse_rerank_rejects_bad_shapes():
    assert rerank_judged.parse_rerank("no json", {"A"}) is None
    assert rerank_judged.parse_rerank('{"none_answer": true}', {"A"}) is None  # no grades


def test_rerank_prompt_lists_the_pool_inline():
    pool = [{"thread_id": "T1", "title": "the answer", "snippet": "blue whale facts"}]
    p = rerank_judged.build_prompt("largest animal", pool, "py tool")
    assert "largest animal" in p
    assert "T1" in p and "the answer" in p and "blue whale facts" in p


# ── query generator: parse + prompt + case assembly ──────────────────────────

def test_parse_queries_keeps_valid_tiers_and_dedupes():
    v = querygen.parse_queries(
        '{"queries": ['
        '{"query": "exact phrase", "difficulty": "verbatim"}, '
        '{"query": "in other words", "difficulty": "paraphrase"}, '
        '{"query": "exact phrase", "difficulty": "vague"}, '   # dup query dropped
        '{"query": "bad tier", "difficulty": "medium"}, '      # unknown tier dropped
        '{"query": "", "difficulty": "vague"}], '              # empty query dropped
        '"note": "one tier skipped"}')
    assert [q["query"] for q in v["queries"]] == ["exact phrase", "in other words"]
    assert v["queries"][0]["difficulty"] == "verbatim"
    assert v["note"] == "one tier skipped"


def test_parse_queries_keeps_at_most_one_query_per_tier():
    # The ladder is one query per difficulty tier: a target that returns several
    # at the same tier keeps only the first, so it can't outweigh single-case
    # targets when the file is scored.
    v = querygen.parse_queries(
        '{"queries": ['
        '{"query": "first vague", "difficulty": "vague"}, '
        '{"query": "second vague", "difficulty": "vague"}, '
        '{"query": "third vague", "difficulty": "vague"}, '
        '{"query": "the verbatim one", "difficulty": "verbatim"}]}')
    assert [(q["difficulty"], q["query"]) for q in v["queries"]] == [
        ("vague", "first vague"), ("verbatim", "the verbatim one")]


def test_parse_queries_empty_list_is_valid():
    v = querygen.parse_queries('{"queries": [], "note": "too generic"}')
    assert v["queries"] == []


def test_parse_queries_rejects_bad_shapes():
    assert querygen.parse_queries("junk") is None
    assert querygen.parse_queries('{"note": "x"}') is None  # no queries key


# ── mining provenance (finding 4) ────────────────────────────────────────────

def test_prompt_sha_is_stable_and_prompt_sensitive():
    # A stamp so a re-mint under a changed prompt is detectable rather than
    # silently redefining a benchmark beneath an old, filename-keyed floor.
    a = fw.prompt_sha("grade this pool")
    assert a == fw.prompt_sha("grade this pool")  # deterministic
    assert a != fw.prompt_sha("grade this pool differently")  # prompt-sensitive
    assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)


def test_miner_commit_is_a_short_sha_or_none():
    commit = fw.miner_commit()
    assert commit is None or (isinstance(commit, str) and commit)


def test_resolved_model_prefers_the_concrete_id_over_the_alias():
    from search_lab.mine._agent import _resolved_model

    # The CLI reports the resolved id directly on newer versions …
    assert _resolved_model({"model": "claude-opus-4-8-20260101"}) == \
        "claude-opus-4-8-20260101"
    # … and only in the modelUsage map on older ones.
    assert _resolved_model({"modelUsage": {"claude-opus-4-8": {"cost": 1}}}) == \
        "claude-opus-4-8"
    # Neither present → None, and the caller falls back to the requested alias.
    assert _resolved_model({"num_turns": 3}) is None


def test_gen_prompt_hands_over_the_thread_to_read():
    p = querygen.build_prompt("01THREADID", "a real conversation", "py tool")
    assert "01THREADID" in p and "a real conversation" in p
    assert "read 01THREADID" in p
    for tier in querygen.DIFFICULTIES:
        assert tier in p


def test_cases_from_queries_binds_gold_and_carries_difficulty():
    rows = querygen.cases_from_queries(
        "T1", [{"query": "q1", "difficulty": "verbatim"},
               {"query": "q2", "difficulty": "vague"}], "snap-1")
    assert [r["query"] for r in rows] == ["q1", "q2"]
    for r in rows:
        assert r["gold"] == ["T1"] and r["grades"] == {"T1": 2}
        assert r["sessions"] == [] and r["snapshot_id"] == "snap-1"
        assert r["protocol"] == "query-gen"
    assert rows[0]["difficulty"] == "verbatim" and rows[1]["difficulty"] == "vague"


def test_sample_threads_excludes_already_mined(archive_home):
    """Random sampling with a skip set: a thread already represented in the file is
    not re-mined, and only threads with enough content are eligible."""
    from sqlalchemy import text as sa_text

    from thread_archive._store import Thread, get_session, init_db

    init_db()
    ids = []
    eid = iter(range(1000))
    with get_session() as s:
        for n in range(3):
            t = Thread(name=f"conv:{n}", title=f"a real conversation number {n}",
                       thread_type="conversation", source="cc", source_id=f"c{n}")
            s.add(t)
            s.flush()
            for i in range(3):  # enough user content to be eligible
                s.execute(sa_text(
                    "INSERT INTO events_fts (event_id, thread_id, event_type, "
                    "content, content_type) VALUES (:e, :t, 'message', :c, 'user')"),
                    {"e": next(eid), "t": t.id, "c": f"user turn {i} in {n}"})
            ids.append(t.id)
        s.commit()

    sampled = querygen.sample_threads(10, seed=1, skip=set())
    assert {t["thread_id"] for t in sampled} == set(ids)

    skip_one = querygen.sample_threads(10, seed=1, skip={ids[0]})
    assert ids[0] not in {t["thread_id"] for t in skip_one}
    assert len(skip_one) == 2
