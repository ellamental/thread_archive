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

import pytest

from search_lab.mine import commit_linked as cm  # noqa: E402

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


def test_split_budget_serves_short_diffs_whole_and_hands_back_the_surplus():
    # 100 to share: the two short ones fit entire, the long one takes what is left
    assert cm.split_budget([10, 20, 500], 100) == [10, 20, 70]
    # nothing to trim — every diff fits
    assert cm.split_budget([10, 20], 100) == [10, 20]
    # nothing fits — the cut is even
    assert cm.split_budget([500, 500], 100) == [50, 50]


def test_build_prompt_shows_every_commits_diff_not_just_the_first():
    # A first diff at the cap used to spend the whole budget, so a multi-commit
    # session's later work reached the author as a message with no change under it.
    unit = {"thread_id": "T1", "repo": "o/r", "files": [],
            "commits": [{"sha": "a", "message": "first", "patch": "A" * 50_000},
                        {"sha": "b", "message": "second", "patch": "B" * 40}]}
    prompt = cm.build_prompt(unit)
    assert "B" * 40 in prompt                       # the short second diff is whole
    assert "diff truncated" in prompt               # and the clip is declared
    body, clipped = cm.render_patches(unit["commits"])
    assert clipped == 1 and len(body) < cm.MAX_PATCH_CHARS + 200


def test_render_patches_tolerates_commits_with_no_diff():
    commits = [{"sha": "a", "message": "m", "patch": ""},
               {"sha": "b", "message": "m", "patch": "@@ -1 +1 @@"}]
    body, clipped = cm.render_patches(commits)
    assert body == "@@ -1 +1 @@" and clipped == 0
    assert cm.render_patches([])[0] == ""


# ── the miner's run path ─────────────────────────────────────────────────────

def test_commit_miner_run_writes_cases(archive_home, tmp_path):
    import argparse

    from search_lab.mine import _framework as fw
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
    calls: list[dict] = []

    def fake_agent(prompt, model, tool_cmd, **kwargs):
        """Two agent stages, two different asks — dispatch on the prompt the way
        the real ``claude`` would, so the run exercises the audit *and* the author
        rather than feeding one reply to both."""
        calls.append({"prompt": prompt, **kwargs})
        if "auditing a benchmark" in prompt:
            return json.dumps({"aligned": True, "targetable": True,
                               "reason": "the session edits a.py and caps the retry loop"
                               }), {"num_turns": 6, "cost_usd": 0.4}
        return json.dumps({"queries": [
            {"query": "where did we cap the upload retries",
             "difficulty": "intent"}]}), {"num_turns": 1, "cost_usd": 0.1}

    ns = argparse.Namespace(model="opus", jobs=1, seed=7, out=out, target=1,
                            linkage=linkage, per_repo=5,
                            provenance_gate=True, alignment=True)
    ctx = fw.MineContext(snapshot_id="snap-1", target=1, model="opus", jobs=1,
                         tool_cmd="py tool", args=ns, agent_run=fake_agent)
    result = cm.MINER.run(ctx)

    assert result.written == 1 and result.attempted == 1
    (row,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert row["miner"] == "commit" and row["snapshot_id"] == "snap-1"
    assert row["gold"][0] in (gid, sid)
    assert row["grades"][row["gold"][0]] == 2
    assert "GONE" not in row["grades"]                 # stale row never sampled
    assert len(row["prompt_sha"]) == 12 and "miner_commit" in row
    assert len(row["template_sha"]) == 12

    audit, author = calls
    # The auditor reads the session — that is its whole job — and the author is
    # denied the corpus seam structurally rather than asked not to look.
    assert audit["corpus_access"] is not False and gid in audit["prompt"]
    assert author["corpus_access"] is False
    assert gid not in author["prompt"] and sid not in author["prompt"]
    # And nothing the auditor *found* is carried forward. This is the property the
    # whole two-stage design rests on: a validator that handed its reasoning to the
    # author would be a vocabulary leak wearing a QA badge.
    assert "caps the retry loop" not in author["prompt"]

    # The funnel is the run's record of where units went, stage by stage.
    stages = [r["stage"] for r in result.funnel.rows()]
    assert stages == ["linkage", "sample", "provenance", "alignment", "author",
                      "verify"]
    linkage_row = result.funnel.rows()[0]
    assert linkage_row["in"] == 3 and linkage_row["out"] == 2
    assert linkage_row["reasons"]["absent-from-snapshot"] == 1
    assert result.funnel.cost_usd == 0.5           # audit + author, per unit
    assert "funnel" in result.notes[0]


def test_commit_miner_is_excluded_from_mine_all():
    # `mine all` drives only miners a count alone can run; this one needs a linkage
    # file an ordinary archive has no reason to carry.
    assert cm.MINER.runnable_in_all is False


def test_commit_miner_is_registered():
    from search_lab.mine import load_registry

    assert "commit" in {m.name for m in load_registry()}


# ── the SWE-chat corpus builder ──────────────────────────────────────────────

def _corpus_module():
    """``search_lab/swechat_corpus.py``, loaded by path (a script, not a package
    module)."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "search_lab" / "swechat_corpus.py"
    spec = importlib.util.spec_from_file_location("swechat_corpus", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corpus_build_folds_the_code_axis(archive_home):
    """A corpus home has no watcher, so nothing else ever builds ``event_paths``.
    Left unfolded it is empty rather than merely stale, and ``mine edited`` finds
    no path with two editing conversations on a corpus full of them."""
    from sqlalchemy import text as sa_text

    from thread_archive._store import Thread, get_session, init_db, use_session

    sc = _corpus_module()
    init_db()
    with get_session() as s:
        t = Thread(name="c:t", title="t", thread_type="conversation",
                   source="claude-code", source_id="s1")
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()
    with use_session() as s:
        s.execute(sa_text(
            "INSERT INTO events (thread_id, stream_id, event_type, payload, "
            "occurred_at) VALUES (:t, 's', 'tool_use_complete', :p, :at)"),
            {"t": tid, "at": "2026-01-05T10:00:00Z",
             "p": json.dumps({"tool_name": "Edit",
                              "input": {"file_path": "/repo/src/up.py",
                                        "new_string": "for _ in range(3)"}})})
        s.commit()

    result = sc.fold_code_index(archive_home)

    assert result["paths"] >= 1
    with use_session() as s:
        rows = s.execute(sa_text("SELECT path FROM event_paths")).all()
    assert "/repo/src/up.py" in {r[0] for r in rows}
    # Idempotent: the second fold is a cursor read, not a rebuild.
    assert sc.fold_code_index(archive_home)["paths"] == result["paths"]


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


def test_transcript_detector_rejects_other_harnesses_line_delimited_json(tmp_path):
    """Line-delimited JSON with a ``type`` key is not enough to call a file Claude
    Code's. Codex writes JSONL too, under a ``{timestamp, type, payload}`` envelope
    the Claude Code parser does not read — accepting it imports a thread of about
    four events where a real session holds several hundred, which is a corpus of
    empty documents rather than a loud failure."""
    sc = _corpus_module()

    codex = tmp_path / "codex.jsonl"
    codex.write_text(json.dumps({
        "timestamp": "2026-03-30T05:38:34.431Z", "type": "session_meta",
        "payload": {"id": "019d3d40", "cwd": "/repo", "originator": "codex-tui"},
    }) + "\n")
    assert not sc.is_claude_code_transcript(codex)

    copilot = tmp_path / "copilot.jsonl"
    copilot.write_text(json.dumps({"type": "session.start"}) + "\n")
    assert not sc.is_claude_code_transcript(copilot)


def test_transcript_detector_accepts_preamble_opening_lines(tmp_path):
    """Most Claude Code transcripts do not open on a turn. A file-history snapshot
    or a title record carries none of the session keys, so a rule that keyed only
    on ``sessionId``/``parentUuid`` would reject the majority of the corpus."""
    sc = _corpus_module()

    for opener in ("file-history-snapshot", "progress", "queue-operation",
                   "permission-mode", "summary", "custom-title"):
        path = tmp_path / f"{opener}.jsonl"
        path.write_text(json.dumps({"type": opener}) + "\n")
        assert sc.is_claude_code_transcript(path), opener


def test_as_list_normalizes_json_string_columns():
    """SWE-chat stores list columns as JSON strings in some tables and real lists
    in others; both must read the same."""
    sc = _corpus_module()
    assert sc._as_list('["a", "b"]') == ["a", "b"]
    assert sc._as_list(["a"]) == ["a"]
    assert sc._as_list("") == [] and sc._as_list(None) == []
    assert sc._as_list("not json") == [] and sc._as_list('{"k": 1}') == []


# ── the gates ────────────────────────────────────────────────────────────────

def _ctx(**kw):
    import argparse

    from search_lab.mine import _framework as fw

    ns = argparse.Namespace(provenance_gate=True, alignment=True, **kw)
    return fw.MineContext(snapshot_id="s", target=1, model="opus", jobs=1,
                          tool_cmd="py tool", args=ns)


def test_commit_files_strips_the_name_status_prefix():
    """SWE-chat stores files_changed as `git diff --name-status`, so every path
    arrives behind a status letter and a tab."""
    unit = {"commits": [{"files": ["M\tsrc/a.py", "D\tsrc/b.py", "  "]},
                        {"files": ["A\tsrc/a.py"]}]}          # dupe collapses
    assert cm.commit_files(unit) == ["src/a.py", "src/b.py"]


def test_provenance_gate_refuses_a_session_that_never_touched_the_files(archive_home):
    from sqlalchemy import text as sa_text

    from search_lab.mine import _framework as fw
    from thread_archive._store import Thread, get_session, init_db, use_session

    init_db()
    with get_session() as s:
        t = Thread(name="c:t", title="t", thread_type="conversation",
                   source="cc", source_id="s1")
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()
    with use_session() as s:
        s.execute(sa_text(
            "INSERT INTO event_paths (event_id, thread_id, path, basename, op, "
            "tool_name, occurred_at) VALUES (1, :t, '/repo/src/a.py', 'a.py', "
            "'edit', 'Edit', '2026-01-05T10:00:00Z')"), {"t": tid})
        s.commit()

    aligned = {"thread_id": tid, "commits": [{"files": ["M\tsrc/a.py"]}]}
    assert fw.Verdict is not None
    assert cm.stage_provenance(aligned, _ctx()).kept          # the trail agrees

    elsewhere = {"thread_id": tid, "commits": [{"files": ["M\tsrc/z.py"]}]}
    verdict = cm.stage_provenance(elsewhere, _ctx())
    assert not verdict.kept and verdict.reason == "no-file-overlap"


def test_provenance_gate_stands_down_when_the_corpus_has_no_projection(archive_home):
    """An empty event_paths means unmeasured, not disproven. Refusing every unit
    would report a corpus of pure misattribution, which is a spectacular way to be
    wrong."""
    from thread_archive._store import init_db

    init_db()
    unit = {"thread_id": "T1", "commits": [{"files": ["M\tsrc/a.py"]}]}
    assert cm.stage_provenance(unit, _ctx()).kept


def test_provenance_gate_can_be_turned_off(archive_home):
    from thread_archive._store import init_db

    init_db()
    unit = {"thread_id": "T1", "commits": [{"files": ["M\tsrc/a.py"]}]}
    ctx = _ctx()
    ctx.args.provenance_gate = False
    assert cm.stage_provenance(unit, ctx).kept


def test_parse_alignment_requires_both_verdicts():
    """A reply answering one question is not a verdict, and defaulting the other
    would silently turn the audit into a rubber stamp."""
    v = cm.parse_alignment('ok: {"aligned": true, "targetable": false, "reason": "wip"}')
    assert v == {"aligned": True, "targetable": False, "reason": "wip"}
    assert cm.parse_alignment('{"aligned": true}') is None
    assert cm.parse_alignment('{"targetable": true}') is None
    assert cm.parse_alignment('{"aligned": "yes", "targetable": true}') is None
    assert cm.parse_alignment("no json") is None


def test_alignment_gate_separates_a_broken_label_from_a_hard_one():
    """The two drops move a benchmark in opposite directions — one removes a wrong
    label, the other removes a hard case — so they are never one counter."""
    unit = {"thread_id": "T1", "repo": "o/r",
            "commits": [{"sha": "a", "message": "m", "patch": "@@", "files": []}]}

    def replying(payload):
        return lambda *a, **k: (json.dumps(payload), {"cost_usd": 0.3})

    ctx = _ctx()
    ctx.agent_run = replying({"aligned": False, "targetable": True, "reason": "r"})
    assert cm.stage_alignment(unit, ctx).reason == "misattributed"

    ctx.agent_run = replying({"aligned": True, "targetable": False, "reason": "r"})
    assert cm.stage_alignment(unit, ctx).reason == "untargetable-commit"

    ctx.agent_run = replying({"aligned": True, "targetable": True, "reason": "r"})
    verdict = cm.stage_alignment(unit, ctx)
    assert verdict.kept and verdict.reason == "ok" and verdict.cost_usd == 0.3


def test_alignment_gate_records_a_failed_or_unparseable_audit_apart():
    unit = {"thread_id": "T1", "repo": "o/r",
            "commits": [{"sha": "a", "message": "m", "patch": "@@", "files": []}]}
    ctx = _ctx()
    ctx.agent_run = lambda *a, **k: (None, {"error": "timeout"})
    assert cm.stage_alignment(unit, ctx).reason == "audit-failed"
    ctx.agent_run = lambda *a, **k: ("hello", {})
    assert cm.stage_alignment(unit, ctx).reason == "audit-unparseable"


def test_no_alignment_drops_the_paid_gate_from_the_declared_funnel():
    import argparse

    full = cm.MINER.stages(argparse.Namespace(alignment=True))
    assert [s.name for s in full] == ["provenance", "alignment", "author", "verify"]
    # Free gates bracket the spend: cheap admission first so the paid stages are
    # asked about fewer units, cheap QA last so nothing pays to check its own work.
    assert [s.kind for s in full] == ["free", "agent", "agent", "free"]
    cheap = cm.MINER.stages(argparse.Namespace(alignment=False))
    assert [s.name for s in cheap] == ["provenance", "author", "verify"]
