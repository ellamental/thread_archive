"""Coverage for the mined-gold quality panel.

The panel exists to catch failures that are invisible in a score — a file of
single-gold cases, a graded pool of one document, an authoring template parroted
across a tier, a sample piled into one repo. So the tests are mostly *detection*
tests: build a file with the defect and assert the panel names it, then build the
healthy version and assert it stays quiet. A detector that fires on everything is
worth as little as one that never fires.
"""

from __future__ import annotations

import json
from collections import Counter

from search_lab import gold_stats as gs


def _write(tmp_path, rows, name="commit-cases.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _case(query, **kw):
    row = {"query": query, "gold": ["T1"], "grades": {"T1": 2},
           "protocol": "commit-linked", "snapshot_id": "snap-1"}
    row.update(kw)
    return row


# ── loading ─────────────────────────────────────────────────────────────────

def test_read_jsonl_skips_junk_rather_than_failing(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n\nnot json at all\n{"a": 2}\n{"a":')
    assert [r["a"] for r in gs.read_jsonl(p)] == [1, 2]


def test_read_jsonl_of_a_missing_file_is_empty(tmp_path):
    assert gs.read_jsonl(tmp_path / "nope.jsonl") == []


# ── statistics ──────────────────────────────────────────────────────────────

def test_spread_reports_quantiles_not_just_a_mean():
    s = gs.spread([1, 1, 1, 1, 100])
    assert s["median"] == 1 and s["max"] == 100 and s["n"] == 5
    # the outlier moves the mean and not the median — which is the point
    assert s["mean"] > s["median"]


def test_spread_of_nothing_is_empty():
    assert gs.spread([]) == {}


def test_concentration_is_one_when_spread_and_zero_when_piled_up():
    even = gs.concentration(Counter({"a": 1, "b": 1, "c": 1, "d": 1}))
    assert even["entropy"] == 1.0 and even["top_share"] == 0.25
    piled = gs.concentration(Counter({"a": 9, "b": 1}))
    assert piled["entropy"] < 0.5 and piled["top_share"] == 0.9
    single = gs.concentration(Counter({"a": 5}))
    assert single["entropy"] == 0.0 and single["top_share"] == 1.0


def test_concentration_of_nothing_is_empty():
    assert gs.concentration(Counter()) == {}


# ── pool ────────────────────────────────────────────────────────────────────

def test_pool_shape_flags_single_gold_and_a_pool_of_one():
    cases = [_case(f"q{i}") for i in range(4)]
    p = gs.pool_shape(cases)
    assert p["single_gold"] == 4 and p["single_gold_share"] == 1.0
    assert p["pool_of_one"] == 4          # grades holds only the gold itself
    assert p["resolution"] == 0.25        # one case moves any metric by 1/4
    assert p["grade_census"] == {"2": 4}


def test_pool_shape_sees_a_real_multi_answer_pool():
    cases = [_case("q", gold=["A", "B", "C"],
                   grades={"A": 2, "B": 2, "C": 2, "D": 1, "E": 0})]
    p = gs.pool_shape(cases)
    assert p["gold_set"]["median"] == 3 and p["graded_pool"]["median"] == 5
    assert p["single_gold"] == 0 and p["pool_of_one"] == 0
    assert p["cases_with_a_confound"] == 1


# ── query shape: the template detector ──────────────────────────────────────

def test_query_shape_flags_a_parroted_opening():
    """The failure this panel exists for: every query individually plausible, the
    set of them one sentence with the subject swapped."""
    cases = [_case(f"that time we changed the {w}", difficulty="intent")
             for w in "abcdefghij"]
    q = gs.query_shape(cases)
    assert q["openings"]["top_share"] == 1.0
    assert gs._template_flag(q["openings"]) == "  ⚠ TEMPLATE"


def test_query_shape_stays_quiet_on_a_varied_set():
    varied = ["where did the retry backoff get capped",
              "uploader stops after three attempts", "who touched the lockfile",
              "session about postgres connection pooling", "the ingest deadlock fix",
              "why did we drop the cross encoder", "rate limiting on the watcher",
              "changing how summaries are scored", "the day search got slow",
              "removing the pagerank term"]
    q = gs.query_shape([_case(v) for v in varied])
    assert gs._template_flag(q["openings"]) == ""


def test_template_flag_needs_enough_queries_to_mean_anything():
    """Three queries that happen to share an opening is a coincidence, not a
    template — flagging it would train the reader to ignore the flag."""
    tiny = gs.query_shape([_case("that time we broke it") for _ in range(3)])
    assert gs._template_flag(tiny["openings"]) == ""


def test_query_shape_validates_the_tier_contract_per_tier():
    """`literal` promises to name identifiers and the others promise not to, so the
    per-tier identifier share is what says the ladder is a ladder."""
    cases = ([_case(f"fix parse_header in reader_{i}.py", difficulty="literal")
              for i in range(4)]
             + [_case(f"the thing that reads incoming headers number {i}",
                      difficulty="intent") for i in range(4)])
    tiers = gs.query_shape(cases)["by_tier"]
    assert tiers["literal"]["carries_identifier"] == 1.0
    assert tiers["intent"]["carries_identifier"] == 0.0


def test_query_shape_of_an_empty_file_is_empty():
    assert gs.query_shape([]) == {}


# ── coverage ────────────────────────────────────────────────────────────────

def test_coverage_flags_mixed_templates_but_not_per_unit_prompt_hashes():
    """A rendered prompt embeds its own unit, so 25 cases carry 25 prompt hashes by
    construction — counting them as populations was a false alarm. The template
    hash is the one that means anything."""
    cases = [_case(f"q{i}", prompt_sha=f"p{i}", template_sha="tpl-1")
             for i in range(5)]
    cov = gs.coverage(cases)
    assert cov["template_shas"] == ["tpl-1"]
    assert cov["distinct_prompt_shas"] == 5
    assert "⚠" not in gs.text_report({"file": "x", "coverage": cov})

    mixed = cases + [_case("q9", prompt_sha="p9", template_sha="tpl-2")]
    assert len(gs.coverage(mixed)["template_shas"]) == 2
    assert "authoring templates" in gs.text_report(
        {"file": "x", "coverage": gs.coverage(mixed)})


def test_coverage_reports_repo_concentration():
    cases = ([_case("q", repo="o/big") for _ in range(8)]
             + [_case("q", repo="o/small") for _ in range(2)])
    cov = gs.coverage(cases)
    assert cov["repo"]["distinct"] == 2 and cov["repo"]["top_share"] == 0.8


def test_coverage_flags_cases_bound_to_different_corpora():
    cases = [_case("a", snapshot_id="s1"), _case("b", snapshot_id="s2")]
    text = gs.text_report({"file": "x", "coverage": gs.coverage(cases)})
    assert "not all bound to the same corpus" in text


# ── yield & cost ────────────────────────────────────────────────────────────

def test_yield_reads_cost_from_either_sidecar_shape():
    """The commit miner records its agent block as `agent`, the others as `stats`;
    the panel reads a file it did not write, so it accepts both."""
    details = [{"outcome": "ok", "agent": {"cost_usd": 0.20}},
               {"outcome": "no-queries", "stats": {"cost_usd": 0.10}}]
    y = gs.yield_and_cost([_case("q")], details, [])
    assert y["cost_usd"]["total"] == 0.3
    assert y["units_drawn"] == 2 and y["drop_rate"] == 0.5
    assert y["cases_per_unit"] == 0.5


def test_yield_surfaces_the_newest_runs_funnel():
    runs = [{"kind": "mine-run", "at": "2026-07-27T00:00:00Z",
             "funnel": [{"stage": "supply", "kind": "free", "in": 100, "out": 40,
                         "reasons": {"absent": 60}}]},
            {"kind": "mine-run", "at": "2026-07-01T00:00:00Z", "funnel": []}]
    y = gs.yield_and_cost([], [], runs)
    assert y["funnel"][0]["stage"] == "supply" and y["runs"] == 2
    assert "supply" in gs.text_report({"file": "x", "yield": y})


def test_a_run_with_no_funnel_says_so_rather_than_implying_none_were_dropped():
    text = gs.text_report({"file": "x", "yield": {"cases": 3}})
    assert "funnel: not recorded" in text


# ── the assembled report ────────────────────────────────────────────────────

def test_report_assembles_every_panel_from_a_file(tmp_path):
    cases = _write(tmp_path, [
        _case("where did we cap the retries", difficulty="intent", miner="commit",
              repo="o/r", template_sha="t1"),
        _case("retry_backoff in uploader.py", difficulty="literal", miner="commit",
              repo="o/r", template_sha="t1"),
    ])
    gs.detail_path_for(cases).write_text(
        json.dumps({"outcome": "ok", "agent": {"cost_usd": 0.5}}) + "\n")
    data = gs.report(cases)
    assert data["miners"] == ["commit"]
    assert data["pool"]["single_gold"] == 2
    assert data["yield"]["cost_usd"]["total"] == 0.5
    assert data["queries"]["by_tier"].keys() == {"intent", "literal"}
    assert "gold stats" in gs.text_report(data)


def test_report_of_an_empty_file_does_not_crash(tmp_path):
    data = gs.report(_write(tmp_path, []))
    assert data["yield"]["cases"] == 0
    assert isinstance(gs.text_report(data), str)


def test_main_rejects_a_missing_file(tmp_path, capsys):
    assert gs.main([str(tmp_path / "nope.jsonl")]) == 2


def test_main_emits_json(tmp_path, capsys):
    p = _write(tmp_path, [_case("q")])
    assert gs.main([str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pool"]["single_gold"] == 1


# ── refusals: the negative results ───────────────────────────────────────────

def test_refusals_panel_reads_the_record_beside_the_cases(tmp_path):
    """Cases say what a corpus could be asked; refusals say what it could not — and
    at scale several of the reasons are findings about the dataset rather than the
    run."""
    cases = _write(tmp_path, [_case("q")])
    (tmp_path / "commit-cases-rejects.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"unit": "T1", "stage": "provenance", "reason": "no-file-overlap", "kind": "free"},
        {"unit": "T2", "stage": "alignment", "reason": "misattributed", "kind": "agent"},
        {"unit": "T3", "stage": "alignment", "reason": "misattributed", "kind": "agent"},
        {"unit": "T4", "stage": "verify", "reason": "queries-rejected", "kind": "free",
         "detail": {"rejected": [{"why": "literal-names-nothing"},
                                 {"why": "repeats-an-opening"}]}},
    ]) + "\n")
    r = gs.refusals(cases)
    assert r["total"] == 4 and r["units"] == 4
    assert r["paid"] == 2                    # only the agent-stage refusals cost money
    assert r["by_reason"]["misattributed"] == 2
    assert r["by_stage"]["alignment"] == 2
    # A kept unit can still have queries thrown out; that is a different event.
    assert r["rejected_queries"] == {"literal-names-nothing": 1,
                                     "repeats-an-opening": 1}


def test_refusals_is_empty_when_nothing_was_refused(tmp_path):
    assert gs.refusals(_write(tmp_path, [_case("q")])) == {}


def test_report_carries_refusals_into_the_text_panel(tmp_path):
    cases = _write(tmp_path, [_case("q")])
    (tmp_path / "commit-cases-rejects.jsonl").write_text(json.dumps(
        {"unit": "T2", "stage": "alignment", "reason": "misattributed",
         "kind": "agent"}) + "\n")
    text = gs.text_report(gs.report(cases))
    assert "REFUSALS" in text and "misattributed 1" in text
    assert "came from a paid gate" in text
