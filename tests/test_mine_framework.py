"""The gold-mining framework: the contract, the shared helpers, the CLI surface.

The agents are operator-run and cost real tokens; covered here is everything
around them — the registry contract every miner must satisfy, the shared helpers
(gold validation, resume dedupe, output-path convention that keeps files
discoverable by the gold gate), and the CLI list/dispatch surface. A registered
miner's own parse / prompt / case-assembly logic is tested beside that miner
(``test_commit_mine_gold.py``).
"""

from __future__ import annotations

import pathlib
from pathlib import Path

from search_lab.gold_files import NON_GOLD_MARKERS
from search_lab.mine import (  # noqa: E402
    _cli,
    load_registry,
)
from search_lab.mine import (  # noqa: E402
    _framework as fw,
)

REPO = Path(__file__).resolve().parent.parent


# ── registry contract ────────────────────────────────────────────────────────

def test_registry_names_are_unique_and_populated():
    reg = load_registry()
    names = [m.name for m in reg]
    assert names == ["commit", "edited", "pooled"]
    assert len(set(names)) == len(names)


def test_every_miner_declares_the_descriptor():
    for m in load_registry():
        assert m.name and m.summary and m.measures and m.unit and m.cost
        assert m.target_kind in ("per-case", "batch")
        assert m.target_help
        assert isinstance(m.runnable_in_all, bool)


def test_every_miner_names_what_fixes_its_gold():
    """The registry's admission rule, stated at the point of definition: a miner
    declares the artifact its labels come from, and whether retrieval touched
    them. The fields cannot prove the claim — they force the author to make one,
    which is where a circular protocol gets caught."""
    for m in load_registry():
        assert m.gold_source, f"{m.name} declares no gold_source"
        assert isinstance(m.retrieval_free, bool)


def test_the_registry_holds_gold_no_retrieval_touched():
    """The strong rung has to stay populated. A bench whose every miner needs a
    retrieved pool has no reading that is independent of the incumbent, however
    carefully each pool is widened — so losing the last retrieval-free miner is a
    change to what the bench can claim, not a change to its coverage."""
    assert [m.name for m in load_registry() if m.retrieval_free]


def test_every_miner_output_is_gold_discoverable_and_detail_is_not():
    """A miner's default case file must pass the gold gate's discovery filter (so
    the CI floor and the baseline sweep see it), and its detail sidecar must not
    (it is not a scorable gold file)."""
    for m in load_registry():
        # a stem may embed a slug; substitute a concrete one for the check.
        stem = m.cases_stem.replace("<token>", "alpha")
        case_name = f"{stem}.jsonl"
        detail_name = fw.detail_path_for(Path(case_name)).name
        assert "cases" in case_name
        assert not any(mk in case_name for mk in NON_GOLD_MARKERS), case_name
        assert any(mk in detail_name for mk in NON_GOLD_MARKERS), detail_name


def test_miner_case_files_are_discovered_and_sidecars_are_not(tmp_path):
    """End to end against the shared discovery rule: every miner's real default
    basename is kept, every detail sidecar is dropped."""
    from search_lab.gold_files import discover

    for m in load_registry():
        stem = m.cases_stem.replace("<token>", "alpha")
        (tmp_path / f"{stem}.jsonl").write_text("{}\n")
        (tmp_path / fw.detail_path_for(Path(f"{stem}.jsonl")).name).write_text("{}\n")
    found = {p.name for p in discover(tmp_path)}
    assert found == {"commit-cases.jsonl", "edited-cases.jsonl",
                     "pooled-cases.jsonl"}


# ── shared helpers ───────────────────────────────────────────────────────────

def test_validate_gold_resolves_skips_sessions_and_dedupes():
    known = {"a", "b", "sess"}
    resolve = lambda ref: ref if ref in known else None  # noqa: E731
    out = fw.validate_gold(["a", "ghost", "sess", "b", "a"],
                           sessions={"sess"}, resolve=resolve)
    assert out == ["a", "b"]


def test_mined_queries_and_gold_ids_read_existing(tmp_path):
    f = tmp_path / "commit-cases.jsonl"
    f.write_text('{"query": "a", "gold": ["T1", "T2"]}\n'
                 'junk\n'
                 '{"query": "b", "gold": ["T2", "T3"]}\n')
    assert fw.mined_queries(f) == {"a", "b"}
    assert fw.mined_gold_ids(f) == {"T1", "T2", "T3"}
    assert fw.mined_queries(tmp_path / "absent.jsonl") == set()
    assert fw.mined_gold_ids(tmp_path / "absent.jsonl") == set()


def test_default_paths_stay_under_the_gold_dir_and_pair_a_detail(tmp_path):
    p = fw.default_cases_path("commit-cases")
    assert p.name == "commit-cases.jsonl"
    assert p.parent == fw.gold_dir()
    assert fw.detail_path_for(p).name == "commit-cases-detail.jsonl"


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
    cases = tmp_path / "commit-cases.jsonl"
    w = fw.CaseWriter("commit", cases, fw.detail_path_for(cases))
    w.write_case({"query": "q", "gold": ["A"]})
    w.write_detail({"query": "q", "outcome": "ok"})
    import json
    row = json.loads(cases.read_text().splitlines()[0])
    assert row["miner"] == "commit"
    assert fw.detail_path_for(cases).exists()


# ── CLI surface ──────────────────────────────────────────────────────────────

def test_list_view_names_every_miner_and_marks_mine_all():
    registry = load_registry()
    text = _cli.list_miners_text(registry)
    for m in registry:
        assert m.name in text
    # Every registered miner carries a run-mode marker on its row, and the legend
    # explains both markers whichever ones the current registry happens to use.
    for m in registry:
        expected = ("● mine all" if m.runnable_in_all and m.target_kind == "per-case"
                    else "○ direct")
        assert expected in text
    assert "● = " in text and "○ = " in text


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


# ── the pipeline spine ───────────────────────────────────────────────────────

def _ctx():
    import argparse

    return fw.MineContext(snapshot_id="snap-1", target=3, model="opus", jobs=1,
                          tool_cmd="py tool", args=argparse.Namespace())


def test_template_sha_is_stable_per_template_and_prompt_sha_is_not():
    """A rendered prompt embeds its unit, so it identifies one judgment; the
    template is the half that says whether two cases share instructions."""
    tpl = "author queries for {commit}"
    assert fw.template_sha(tpl) == fw.template_sha(tpl)
    assert fw.template_sha(tpl) != fw.template_sha(tpl + " but differently")
    assert fw.prompt_sha(tpl.format(commit="a")) != fw.prompt_sha(tpl.format(commit="b"))


def test_run_stage_splits_kept_from_dropped_and_counts_every_reason():
    stage = fw.Stage(name="gate", fn=lambda u, ctx: (
        fw.Verdict(keep=u, reason="ok") if u % 2 == 0
        else fw.Verdict(reason="odd")))
    kept, stat = fw.run_stage(stage, [1, 2, 3, 4], _ctx())
    assert kept == [2, 4]
    assert stat.n_in == 4 and stat.n_out == 2 and stat.dropped == 2
    assert stat.reasons == {"ok": 2, "odd": 2}


def test_run_stage_may_transform_the_unit_it_carries_forward():
    stage = fw.Stage(name="enrich", fn=lambda u, ctx: fw.Verdict(keep={"n": u}))
    kept, _ = fw.run_stage(stage, [1, 2], _ctx())
    assert kept == [{"n": 1}, {"n": 2}]


def test_a_stage_that_raises_drops_one_unit_rather_than_the_run():
    """One malformed row out of a thousand is a data fact. A miner that dies on it
    has spent everything before it for nothing."""
    def boom(unit, ctx):
        if unit == 2:
            raise ValueError("bad row")
        return fw.Verdict(keep=unit)

    kept, stat = fw.run_stage(fw.Stage(name="s", fn=boom), [1, 2, 3], _ctx())
    assert kept == [1, 3] and stat.reasons["stage-error"] == 1


def test_run_stage_sums_agent_cost_and_reports_it_on_the_row():
    stage = fw.Stage(name="author", kind="agent",
                     fn=lambda u, ctx: fw.Verdict(keep=u, cost_usd=0.25))
    _, stat = fw.run_stage(stage, [1, 2], _ctx())
    assert stat.cost_usd == 0.5 and stat.as_row()["kind"] == "agent"
    assert stat.as_row()["cost_usd"] == 0.5


def test_run_stage_hands_every_verdict_to_the_observer_kept_or_not():
    seen = []
    stage = fw.Stage(name="s", fn=lambda u, ctx: (
        fw.Verdict(keep=u) if u else fw.Verdict(reason="falsy")))
    fw.run_stage(stage, [1, 0, 2], _ctx(),
                 on_verdict=lambda st, unit, v: seen.append((unit, v.reason)))
    assert seen == [(1, "ok"), (0, "falsy"), (2, "ok")]


def test_run_stage_is_order_preserving_under_concurrency():
    stage = fw.Stage(name="s", kind="agent", fn=lambda u, ctx: fw.Verdict(keep=u))
    kept, stat = fw.run_stage(stage, list(range(20)), _ctx(), jobs=4)
    assert kept == list(range(20)) and stat.n_out == 20


def test_funnel_records_set_level_narrowing_beside_per_unit_stages():
    """A supply read and a stratified draw have no per-unit verdict to give, and
    they are still most of what a reader wants before any agent runs."""
    f = fw.Funnel()
    f.note("supply", n_in=1284, n_out=1149, reasons={"absent-from-snapshot": 135})
    _, stat = fw.run_stage(
        fw.Stage(name="author", kind="agent",
                 fn=lambda u, ctx: fw.Verdict(keep=u, cost_usd=0.1)),
        [1, 2], _ctx())
    f.record(stat)
    assert [r["stage"] for r in f.rows()] == ["supply", "author"]
    assert f.cost_usd == 0.2
    text = f.text()
    assert "1284" in text and "absent-from-snapshot 135" in text
    assert "$ author" in text          # agent stages are marked as spending


def test_an_empty_funnel_says_so():
    assert "no stages ran" in fw.Funnel().text()
