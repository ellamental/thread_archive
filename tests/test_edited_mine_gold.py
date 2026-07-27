"""Unit coverage for the edit-linked gold miner's pure logic.

The agent stage is operator-run and costs real tokens; what needs coverage is the
logic around it — the sampling rules that decide which paths can carry a
multi-answer case (a floor so the case is not single-gold, a ceiling so recall@k
stops measuring gold-set size, a per-directory cap so one module doesn't own the
file), the grade pool built entirely from ``event_paths`` (editors 2, other
touchers 1, sibling-directory editors 0), excerpt extraction from tool payloads,
query parsing, and the assembled case's binding to its snapshot.

The load-bearing property here is that gold is *enumerated, not retrieved*: the
tests seed the path projection directly and assert the whole editing set lands in
the case, including a thread nothing would rank.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import text as sa_text

from search_lab.mine import _framework as fw
from search_lab.mine import edited_paths as ep

# ── seeding ──────────────────────────────────────────────────────────────────

def _thread(title: str, *, ttype: str = "conversation", excluded: bool = False) -> str:
    from thread_archive._store import Thread, get_session

    with get_session() as s:
        t = Thread(name=f"c:{title}", title=title, thread_type=ttype,
                   source="cc", source_id=title)
        t.exclude_from_search = excluded
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()
    return tid


_EVENT_ID = iter(range(1, 100_000))

# Edit bodies with enough substance to clear the `substance` gate, which exists so
# an author is never handed a filename and asked to write about work.
_BODY = ("def retry_backoff(attempt: int) -> float:\n"
         "    # cap the wait so an uploader cannot retry forever\n"
         "    return min(BASE_DELAY * 2 ** attempt, MAX_DELAY)\n") * 2
_BODY2 = ("MAX_DELAY = 30.0  # was unbounded; a stuck upload held the queue\n"
          "BASE_DELAY = 0.5\n") * 4


def _touch(thread_id: str, path: str, op: str, *, payload: dict | None = None,
           at: str = "2026-01-05T10:00:00Z") -> int:
    """One row in the path projection, with the tool event behind it when a
    payload is given (the excerpt reader joins through to it)."""
    from thread_archive._store import use_session

    eid = next(_EVENT_ID)
    with use_session() as s:
        if payload is not None:
            s.execute(sa_text(
                "INSERT INTO events (id, thread_id, stream_id, event_type, payload, "
                "occurred_at) VALUES (:id, :tid, 's', 'tool_use_complete', :p, :at)"),
                {"id": eid, "tid": thread_id, "p": json.dumps(payload), "at": at})
        s.execute(sa_text(
            "INSERT INTO event_paths (event_id, thread_id, path, basename, op, "
            "tool_name, occurred_at) VALUES (:e, :t, :p, :b, :o, 'Edit', :at)"),
            {"e": eid, "t": thread_id, "p": path, "b": path.rsplit("/", 1)[-1],
             "o": op, "at": at})
        s.commit()
    return eid


# ── candidate selection ──────────────────────────────────────────────────────

def test_candidates_need_at_least_the_floor_of_editors(archive_home):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    _touch(a, "src/lonely.py", "edit")            # one editor — single-gold
    _touch(a, "src/shared.py", "edit")
    _touch(b, "src/shared.py", "edit")            # two editors — a real case

    paths = {p["path"] for p in ep.candidate_paths(min_sessions=2, max_sessions=12)}
    assert paths == {"src/shared.py"}


def test_candidates_drop_paths_edited_by_too_many(archive_home):
    from thread_archive._store import init_db

    init_db()
    for i in range(5):
        _touch(_thread(f"t{i}"), "src/hot.py", "edit")

    assert ep.candidate_paths(min_sessions=2, max_sessions=12)
    # Past the ceiling the gold set cannot fit a window, so the path is dropped.
    assert ep.candidate_paths(min_sessions=2, max_sessions=4) == []


def test_candidates_ignore_subagent_and_excluded_threads(archive_home):
    """A path only two *conversations* edited must not qualify on a subagent run
    or a search-excluded thread — those never belong in a gold set, so counting
    them would qualify a path its real editors cannot support."""
    from thread_archive._store import init_db

    init_db()
    real = _thread("real")
    agent = _thread("subagent", ttype="system")
    hidden = _thread("hidden", excluded=True)
    for tid in (real, agent, hidden):
        _touch(tid, "src/one.py", "edit")

    assert ep.candidate_paths(min_sessions=2, max_sessions=12) == []


def test_sampling_caps_each_directory_and_is_deterministic(archive_home):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    for i in range(4):
        _touch(a, f"src/mod/f{i}.py", "edit")
        _touch(b, f"src/mod/f{i}.py", "edit")
    _touch(a, "other/z.py", "edit")
    _touch(b, "other/z.py", "edit")

    picked = ep.sample_paths(10, seed=1, skip=set(), min_sessions=2,
                             max_sessions=12, per_dir=2)
    dirs = [p["path"].rsplit("/", 1)[0] for p in picked]
    assert dirs.count("src/mod") == 2          # capped, though four qualify
    assert ep.sample_paths(10, seed=1, skip=set(), min_sessions=2, max_sessions=12,
                           per_dir=2) == picked          # deterministic


def test_sampling_skips_already_mined_paths(archive_home):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    for path in ("src/one.py", "src/two.py"):
        _touch(a, path, "edit")
        _touch(b, path, "edit")

    picked = ep.sample_paths(10, seed=1, skip={"src/one.py"}, min_sessions=2,
                             max_sessions=12, per_dir=5)
    assert [p["path"] for p in picked] == ["src/two.py"]


# ── the graded pool ──────────────────────────────────────────────────────────

def test_grades_enumerate_every_editor_as_gold(archive_home):
    """The property the whole protocol rests on: gold is the complete editing set
    read off the trail, so a thread search could never surface is in it anyway."""
    from thread_archive._store import init_db

    init_db()
    e1, e2 = _thread("editor one"), _thread("editor two")
    reader = _thread("reader")
    sibling = _thread("sibling")
    stranger = _thread("stranger")

    _touch(e1, "src/pkg/target.py", "edit")
    _touch(e2, "src/pkg/target.py", "write")
    _touch(reader, "src/pkg/target.py", "read")
    _touch(sibling, "src/pkg/neighbour.py", "edit")
    _touch(stranger, "elsewhere/far.py", "edit")

    grades = ep.build_grades("src/pkg/target.py", seed=1)
    assert grades[e1] == 2 and grades[e2] == 2
    assert grades[reader] == 1
    assert grades[sibling] == 0
    assert stranger not in grades


def test_grades_are_empty_when_nothing_edited_the_path(archive_home):
    from thread_archive._store import init_db

    init_db()
    _touch(_thread("only-read"), "src/x.py", "read")
    assert ep.build_grades("src/x.py", seed=1) == {}


def test_grades_cap_the_confound_pool(archive_home):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    _touch(a, "src/pkg/target.py", "edit")
    _touch(b, "src/pkg/target.py", "edit")
    for i in range(ep.MAX_CONFOUND + 6):
        _touch(_thread(f"sib{i}"), f"src/pkg/n{i}.py", "edit")

    grades = ep.build_grades("src/pkg/target.py", seed=1)
    assert sum(1 for g in grades.values() if g == 0) == ep.MAX_CONFOUND


def test_grades_are_deterministic_per_path(archive_home):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    _touch(a, "src/pkg/t.py", "edit")
    _touch(b, "src/pkg/t.py", "edit")
    for i in range(20):
        _touch(_thread(f"sib{i}"), f"src/pkg/n{i}.py", "edit")

    assert ep.build_grades("src/pkg/t.py", 3) == ep.build_grades("src/pkg/t.py", 3)


# ── excerpts ─────────────────────────────────────────────────────────────────

def test_excerpts_read_the_change_out_of_the_tool_payload(archive_home):
    from thread_archive._store import init_db

    init_db()
    a = _thread("a")
    _touch(a, "src/x.py", "edit",
           payload={"tool_name": "Edit",
                    "input": {"file_path": "src/x.py",
                              "new_string": "def backoff(): return 2"}})
    assert ep.edit_excerpts("src/x.py") == ["def backoff(): return 2"]


def test_excerpts_skip_payloads_with_no_edit_body(archive_home):
    from thread_archive._store import init_db

    init_db()
    a = _thread("a")
    _touch(a, "src/x.py", "delete", payload={"tool_name": "Bash", "input": {}})
    assert ep.edit_excerpts("src/x.py") == []


def test_excerpts_are_capped_in_length():
    assert ep._excerpt_from_payload({"input": {"content": "x" * 5000}}) == \
        "x" * ep.MAX_EXCERPT_CHARS


# ── parsing and case assembly ────────────────────────────────────────────────

def test_parse_queries_keeps_one_per_tier_and_drops_malformed():
    v = ep.parse_queries(
        'here you go {"queries": ['
        '{"query": "src/x.py retry", "difficulty": "literal"}, '
        '{"query": "second literal", "difficulty": "literal"}, '   # tier taken
        '{"query": "how the uploader backs off", "difficulty": "functional"}, '
        '{"query": "bad tier", "difficulty": "medium"}, '          # unknown tier
        '{"query": "", "difficulty": "intent"}], "note": "n/a"}')
    assert [q["difficulty"] for q in v["queries"]] == ["literal", "functional"]
    assert v["note"] == "n/a"


def test_parse_queries_accepts_empty_and_rejects_junk():
    assert ep.parse_queries('{"queries": [], "note": "lockfile"}')["queries"] == []
    assert ep.parse_queries("no json here") is None
    assert ep.parse_queries('{"note": "no queries key"}') is None


def test_cases_carry_the_whole_gold_set_and_the_snapshot():
    grades = {"T1": 2, "T2": 2, "T3": 1, "T4": 0}
    rows = ep.cases_from_queries(
        {"path": "src/x.py", "n_sessions": 2, "first_at": "", "last_at": ""},
        grades, [{"query": "q", "difficulty": "intent"}], "snap-9")
    (row,) = rows
    assert row["gold"] == ["T1", "T2"]            # every editor, not a sample
    assert row["n_gold"] == 2 and row["grades"] == grades
    assert row["snapshot_id"] == "snap-9" and row["protocol"] == "edit-linked"
    assert row["path"] == "src/x.py" and row["difficulty"] == "intent"


def test_prompt_carries_the_edits_and_never_a_conversation():
    prompt = ep.build_prompt(
        {"path": "src/pkg/x.py", "n_sessions": 3, "first_at": "2026-01-01",
         "last_at": "2026-02-01"},
        ["def backoff(): return 2"])
    assert "src/pkg/x.py" in prompt and "def backoff(): return 2" in prompt
    assert "You have NO tools" in prompt
    for tier in ep.DIFFICULTIES:
        assert tier in prompt


def test_prompt_survives_a_path_with_no_recorded_edit_text():
    prompt = ep.build_prompt(
        {"path": "src/x.py", "n_sessions": 2, "first_at": "", "last_at": ""}, [])
    assert "(no edit text recorded)" in prompt


# ── resume ───────────────────────────────────────────────────────────────────

def test_mined_paths_reads_back_the_resume_key(tmp_path):
    f = tmp_path / "edited-cases.jsonl"
    f.write_text(json.dumps({"query": "a", "path": "src/one.py"}) + "\n"
                 + "junk\n"
                 + json.dumps({"query": "b", "path": "src/one.py"}) + "\n"
                 + json.dumps({"query": "c", "path": "src/two.py"}) + "\n")
    assert ep.mined_paths(f) == {"src/one.py", "src/two.py"}
    assert ep.mined_paths(tmp_path / "absent.jsonl") == set()


# ── the run path ─────────────────────────────────────────────────────────────

def test_edited_miner_run_writes_multi_answer_cases(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    e1, e2 = _thread("editor one"), _thread("editor two")
    reader, sibling = _thread("reader"), _thread("sibling")
    _touch(e1, "src/pkg/target.py", "edit",
           payload={"tool_name": "Edit", "input": {"new_string": _BODY}})
    _touch(e2, "src/pkg/target.py", "edit",
           payload={"tool_name": "Edit", "input": {"new_string": _BODY2}})
    _touch(reader, "src/pkg/target.py", "read")
    _touch(sibling, "src/pkg/other.py", "edit")

    out = tmp_path / "edited-cases.jsonl"
    calls: list[dict] = []

    def fake_agent(prompt, model, tool_cmd, **kw):
        calls.append({"prompt": prompt, **kw})
        if "auditing a benchmark" in prompt:
            return json.dumps({"coherent": True, "targetable": True,
                               "reason": "both sessions rework the retry loop"}), \
                {"cost_usd": 0.5}
        return json.dumps({"queries": [
            {"query": "how this file decides when to give up retrying",
             "difficulty": "intent"},
            {"query": "retry_backoff in src/pkg/target.py",
             "difficulty": "literal"}]}), {"num_turns": 1, "cost_usd": 0.1}

    ns = argparse.Namespace(model="opus", jobs=1, seed=7, out=out, target=1,
                            min_sessions=2, max_sessions=12, per_dir=2,
                            coherence=True)
    ctx = fw.MineContext(snapshot_id="snap-1", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=ns, agent_run=fake_agent)
    result = ep.MINER.run(ctx)

    assert result.written == 2 and result.attempted == 1
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert {r["miner"] for r in rows} == {"edited"}
    for row in rows:
        assert sorted(row["gold"]) == sorted([e1, e2])   # both editors, always
        assert row["grades"][reader] == 1 and row["grades"][sibling] == 0
        assert row["snapshot_id"] == "snap-1"
    audit, author = calls
    # The auditor reads the editing conversations; the author is denied the seam
    # structurally, and nothing the auditor concluded travels forward.
    assert audit["corpus_access"] is True and e1 in audit["prompt"]
    assert author["corpus_access"] is False
    assert "retry_backoff" in author["prompt"]
    assert "rework the retry loop" not in author["prompt"]

    stages = [r["stage"] for r in result.funnel.rows()]
    assert stages == ["supply", "sample", "substance", "coherence", "author",
                      "verify"]


def test_edited_miner_run_records_an_agent_failure(archive_home, tmp_path):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    _touch(a, "src/x.py", "edit",
           payload={"tool_name": "Edit", "input": {"new_string": _BODY}})
    _touch(b, "src/x.py", "edit",
           payload={"tool_name": "Edit", "input": {"new_string": _BODY2}})

    out = tmp_path / "edited-cases.jsonl"
    ns = argparse.Namespace(model="opus", jobs=1, seed=7, out=out, target=1,
                            min_sessions=2, max_sessions=12, per_dir=2,
                            coherence=False)
    ctx = fw.MineContext(snapshot_id="s", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=ns,
                         agent_run=lambda *a, **k: (None, {"error": "boom"}))
    result = ep.MINER.run(ctx)

    assert result.written == 0 and result.failed == 1
    assert result.outcomes == {"agent-failed": 1}
    assert not out.exists() or out.read_text() == ""


def test_edited_miner_run_refuses_an_archive_with_no_path_projection(
    archive_home, tmp_path,
):
    """An archive that never folded its code projection has an empty
    ``event_paths``, which is a setup problem with a fix — so it gets named
    rather than read as "no paths qualify"."""
    import pytest

    from thread_archive._store import init_db

    init_db()
    ns = argparse.Namespace(model="opus", jobs=1, seed=7,
                            out=tmp_path / "edited-cases.jsonl", target=1,
                            min_sessions=2, max_sessions=12, per_dir=2)
    ctx = fw.MineContext(snapshot_id="s", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=ns,
                         agent_run=lambda *a, **k: ("{}", {}))
    with pytest.raises(SystemExit, match="event_paths"):
        ep.MINER.run(ctx)


def test_edited_miner_is_registered_and_declares_retrieval_free_gold():
    from search_lab.mine import load_registry

    (miner,) = [m for m in load_registry() if m.name == "edited"]
    assert miner.retrieval_free is True
    assert miner.gold_source and miner.runnable_in_all is False


# ── the gates ────────────────────────────────────────────────────────────────

def _ectx(**kw):
    ns = argparse.Namespace(seed=7, min_sessions=2, coherence=True, **kw)
    return fw.MineContext(snapshot_id="s", target=1, model="opus", jobs=1,
                          tool_cmd="py tool", args=ns)


def test_substance_gate_refuses_a_path_with_no_readable_change(archive_home):
    """The author sees the path and its edit excerpts and nothing else, so a path
    whose payloads carry no change body leaves it writing about a filename."""
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    _touch(a, "src/bare.py", "edit")        # no payload at all
    _touch(b, "src/bare.py", "edit")
    row = {"path": "src/bare.py", "n_sessions": 2}
    assert ep.stage_substance(row, _ectx()).reason == "no-edit-text"


def test_substance_gate_refuses_a_trivial_edit(archive_home):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    for t in (a, b):
        _touch(t, "src/tiny.py", "edit",
               payload={"tool_name": "Edit", "input": {"new_string": "x = 1"}})
    row = {"path": "src/tiny.py", "n_sessions": 2}
    assert ep.stage_substance(row, _ectx()).reason == "trivial-edits"


def test_substance_gate_admits_real_work_and_attaches_the_pool(archive_home):
    from thread_archive._store import init_db

    init_db()
    a, b = _thread("a"), _thread("b")
    _touch(a, "src/real.py", "edit",
           payload={"tool_name": "Edit", "input": {"new_string": _BODY}})
    _touch(b, "src/real.py", "edit",
           payload={"tool_name": "Edit", "input": {"new_string": _BODY2}})
    row = {"path": "src/real.py", "n_sessions": 2}
    verdict = ep.stage_substance(row, _ectx())
    assert verdict.kept and sorted(row["_grades"]) == sorted([a, b])
    assert row["_excerpts"]


def test_parse_audit_requires_both_verdicts():
    assert ep.parse_audit('{"coherent": true, "targetable": true, "reason": "r"}') == \
        {"coherent": True, "targetable": True, "reason": "r"}
    assert ep.parse_audit('{"coherent": true}') is None
    assert ep.parse_audit("nope") is None


def test_coherence_gate_separates_an_incoherent_set_from_an_untargetable_file():
    """Membership here is enumerated, so a *wrong* label is not the risk — an
    incoherent one is. A path six sessions touched for six unrelated reasons has a
    gold set nothing ties together, which is unanswerable by construction."""
    row = {"path": "src/x.py", "_excerpts": ["body"], "_grades": {"T1": 2, "T2": 2}}
    ctx = _ectx()

    def replying(payload):
        return lambda *a, **k: (json.dumps(payload), {"cost_usd": 0.2})

    ctx.agent_run = replying({"coherent": False, "targetable": True, "reason": "r"})
    assert ep.stage_coherence(row, ctx).reason == "incoherent-gold-set"
    ctx.agent_run = replying({"coherent": True, "targetable": False, "reason": "r"})
    assert ep.stage_coherence(row, ctx).reason == "untargetable-path"
    ctx.agent_run = replying({"coherent": True, "targetable": True, "reason": "r"})
    assert ep.stage_coherence(row, ctx).kept


def test_verify_drops_the_offending_query_not_the_whole_path():
    """A path yielding one good query and one templated one should contribute the
    good one."""
    row = {"path": "src/retry.py", "_excerpts": ["def retry_backoff(): pass"],
           "_rows": [
               {"query": "retry_backoff in src/retry.py", "difficulty": "literal"},
               {"query": "that time we changed the waiting", "difficulty": "intent"}]}
    ctx = _ectx()
    ctx.seen_openings = {"that time we"}
    verdict = ep.stage_verify(row, ctx)
    assert verdict.kept and len(row["_rows"]) == 1
    assert row["_rows"][0]["difficulty"] == "literal"
    assert verdict.detail["rejected"][0]["why"] == "repeats-an-opening"


def test_verify_drops_the_unit_when_every_query_fails():
    row = {"path": "src/retry.py", "_excerpts": ["def retry_backoff(): pass"],
           "_rows": [{"query": "the waiting stuff", "difficulty": "literal"}]}
    assert ep.stage_verify(row, _ectx()).reason == "all-queries-rejected"


def test_edited_declares_the_full_funnel_and_drops_the_paid_gate_on_request():
    full = ep.MINER.stages(argparse.Namespace(coherence=True))
    assert [s.name for s in full] == ["substance", "coherence", "author", "verify"]
    assert [s.kind for s in full] == ["free", "agent", "agent", "free"]
    cheap = ep.MINER.stages(argparse.Namespace(coherence=False))
    assert [s.name for s in cheap] == ["substance", "author", "verify"]
