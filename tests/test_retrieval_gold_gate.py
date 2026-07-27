"""The retrieval gold gate: floors ratchet, and a stale or absent fixture skips
green rather than wedging the commit gate red.

Only the pure logic and the skip paths are exercised here — scoring a gold file
needs the 20 GB snapshot and the model arms, which live on the operator's box,
not in the unit suite. Those paths are covered by the CI row itself.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "retrieval_gold_gate", REPO / "scripts" / "retrieval_gold_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
sys.modules["retrieval_gold_gate"] = gate
assert SPEC.loader is not None
SPEC.loader.exec_module(gate)


def test_discover_keeps_gold_files_and_drops_mining_siblings(tmp_path) -> None:
    for name in (
        "judged-cases.jsonl",
        "topic-cases-alpha.jsonl",
        "topic-cases-beta.jsonl",
        "rerank-cases.jsonl",
        "findability-cases.jsonl",
        "judged-cases-detail.jsonl",
        "rerank-cases-detail.jsonl",
        "findability-cases-detail.jsonl",
        "topic-cases-alpha.detail.jsonl",
        "judged-seed.jsonl",
        "seed-candidates.jsonl",
        "judged-cases.jsonl.until-bak",
    ):
        (tmp_path / name).write_text("{}\n")

    found = {p.name for p in gate.discover_gold_files(tmp_path)}
    assert found == {
        "judged-cases.jsonl",
        "topic-cases-alpha.jsonl",
        "topic-cases-beta.jsonl",
        "rerank-cases.jsonl",
        "findability-cases.jsonl",
    }


def test_discover_missing_dir_is_empty(tmp_path) -> None:
    assert gate.discover_gold_files(tmp_path / "nope") == []


def _gold(dir_, name, *, snapshot="aaaaaaaaaaaaaaaa"):
    p = dir_ / name
    p.write_text(json.dumps({"query": "q", "gold": ["t1"], "snapshot_id": snapshot}) + "\n")
    return p


def test_read_floor_reads_the_sidecar_beside_the_gold_file(tmp_path) -> None:
    # A gold file carries its own calibration, the way a test file carries its own
    # assertions. Nothing lists gold files by name.
    g = _gold(tmp_path, "topic-cases-alpha.jsonl")
    gate.floor_path_for(g).write_text(json.dumps({
        "mrr": 0.78, "success10": 0.85, "recall10": 0.78, "ndcg10": 0.59,
        "by_difficulty": {"vague": {"recall10": 0.85}}}))
    floor = gate.read_floor(g)
    assert floor["mrr"] == 0.78
    assert floor["by_difficulty"] == {"vague": {"recall10": 0.85}}
    assert gate.floor_path_for(g).name == "topic-cases-alpha.floor.json"


def test_read_floor_missing_or_junk_is_uncalibrated(tmp_path) -> None:
    # Uncalibrated is scored-but-ungated, never an error: a fixture must not wedge
    # the gate. Absent, unparseable, and wrong-shaped all read the same.
    g = _gold(tmp_path, "topic-cases-alpha.jsonl")
    assert gate.read_floor(g) == {}
    gate.floor_path_for(g).write_text("not json")
    assert gate.read_floor(g) == {}
    gate.floor_path_for(g).write_text('["a list, not an object"]')
    assert gate.read_floor(g) == {}


def test_read_floor_drops_unknown_metrics_and_bad_values(tmp_path) -> None:
    g = _gold(tmp_path, "topic-cases-alpha.jsonl")
    gate.floor_path_for(g).write_text(json.dumps(
        {"mrr": 0.5, "bogus": 1.0, "ndcg10": "x"}))
    assert gate.read_floor(g) == {"mrr": 0.5}


def test_discover_floors_needs_no_manifest(tmp_path) -> None:
    # The calibrated set falls out of discovery: a file with a sidecar is gated, a
    # file without one is not, and there is no list to fall out of sync.
    a = _gold(tmp_path, "topic-cases-alpha.jsonl")
    _gold(tmp_path, "topic-cases-beta.jsonl")          # discovered, uncalibrated
    gate.floor_path_for(a).write_text(json.dumps(
        {"mrr": 0.4, "success10": 0.8, "recall10": 0.7, "ndcg10": 0.5}))
    floors = gate.discover_floors(gate.discover_gold_files(tmp_path))
    assert set(floors) == {"topic-cases-alpha.jsonl"}


def test_floor_sidecars_are_not_themselves_discovered_as_gold(tmp_path) -> None:
    # `.floor.json` must not match the `*cases*.jsonl` glob, or a sidecar would be
    # scored as a fixture.
    a = _gold(tmp_path, "topic-cases-alpha.jsonl")
    gate.floor_path_for(a).write_text(json.dumps({"mrr": 0.4}))
    assert [p.name for p in gate.discover_gold_files(tmp_path)] == [
        "topic-cases-alpha.jsonl"]


def test_every_discovered_floor_names_a_gold_shaped_file(tmp_path) -> None:
    # A floor can only reach a file discovery would keep, since it is found *from*
    # that file rather than declared against a name.
    from search_lab.gold_files import NON_GOLD_MARKERS as markers
    a = _gold(tmp_path, "topic-cases-alpha.jsonl")
    gate.floor_path_for(a).write_text(json.dumps(
        {"mrr": 0.4, "success10": 0.8, "recall10": 0.7, "ndcg10": 0.5}))
    for name, floor in gate.discover_floors(gate.discover_gold_files(tmp_path)).items():
        assert name.endswith(".jsonl") and "cases" in name
        assert not any(m in name for m in markers), name
        assert set(floor) <= {"mrr", "success10", "recall10", "ndcg10", "by_difficulty"}


def test_check_floors_flags_each_quality_signal() -> None:
    floor = {"mrr": 0.40, "success10": 0.80,
             "recall10": 0.70, "ndcg10": 0.50}
    ok = {"mrr": 0.45, "success": {10: 0.90},
          "recall": {10: 0.75}, "ndcg": {10: 0.55}}
    assert gate.check_floors("f", ok, floor) == []

    low_mrr = {**ok, "mrr": 0.30}
    assert gate.check_floors("f", low_mrr, floor) == [
        "f: MRR 0.300 < floor 0.4"
    ]

    low_all = {"mrr": 0.30, "success": {10: 0.60},
               "recall": {10: 0.40}, "ndcg": {10: 0.30}}
    breaches = gate.check_floors("f", low_all, floor)
    assert len(breaches) == 4
    assert any("success@10" in b for b in breaches)
    assert any("recall@10" in b for b in breaches)
    assert any("nDCG@10" in b for b in breaches)


def test_check_floors_other_metrics_optional() -> None:
    # A floor may set MRR only; the other report metrics are not required then.
    assert gate.check_floors("f", {"mrr": 0.5}, {"mrr": 0.4}) == []


def test_check_floors_flags_a_per_difficulty_stratum() -> None:
    # An aggregate that clears its floor can still hide a collapsed stratum: a
    # per-difficulty floor names the tier that product recall lives in.
    floor = {"mrr": 0.40, "by_difficulty": {"vague": {"mrr": 0.60, "success10": 0.80}}}
    report = {
        "mrr": 0.70, "success": {10: 0.9}, "recall": {10: 0.9}, "ndcg": {10: 0.9},
        "per_difficulty": {"vague": {"mrr": 0.50, "success10": 0.85},
                           "verbatim": {"mrr": 0.95, "success10": 1.0}},
    }
    breaches = gate.check_floors("f", report, floor)
    assert breaches == ["f: vague mrr 0.500 < floor 0.6"]  # success10 held, mrr didn't


def test_check_floors_flags_a_floored_tier_missing_from_the_report() -> None:
    floor = {"mrr": 0.40, "by_difficulty": {"vague": {"mrr": 0.60}}}
    report = {"mrr": 0.7, "success": {10: 1}, "recall": {10: 1}, "ndcg": {10: 1},
              "per_difficulty": {}}
    assert any("vague tier floored but absent" in b
               for b in gate.check_floors("f", report, floor))


# --- --require: fail closed -------------------------------------------------


def test_require_fails_closed_on_an_absent_snapshot(tmp_path, monkeypatch, capsys) -> None:
    # The default skips a missing snapshot green (a dev box without the fixture);
    # --require is the CI lane, where "no fixture" is a gate that stopped measuring.
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(tmp_path / "no-snap"))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(tmp_path))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE", raising=False)
    assert gate.main(["--require"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "no snapshot" in out


def test_require_maintenance_env_skips_explicitly_not_silently(tmp_path, monkeypatch, capsys) -> None:
    # A declared-maintenance window still passes CI, but only by announcing itself:
    # a loud MAINTENANCE SKIP, visibly not the same as a green pass.
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(tmp_path / "no-snap"))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(tmp_path))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE", "re-mining the golds")
    assert gate.main(["--require"]) == 0
    out = capsys.readouterr().out
    assert "MAINTENANCE SKIP" in out and "re-mining the golds" in out


def test_require_rejects_only_because_a_subset_cannot_prove_the_manifest() -> None:
    import pytest

    with pytest.raises(SystemExit, match="full calibrated manifest"):
        gate.main(["--require", "--only", "judged"])


def test_require_fails_on_missing_and_stale_calibrated_fixtures(tmp_path, monkeypatch, capsys) -> None:
    # Snapshot present; the gold dir holds one calibrated file and it is stale, so
    # it skips and every other calibrated file is simply missing. Without --require
    # that is a green skip; with it, each expected fixture is named and the gate
    # fails — no scoring runs (the one present file is stale), so no model load.
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "snapshot.json").write_text('{"snapshot_id": "aaaaaaaaaaaaaaaa"}')
    gold = tmp_path / "gold"
    gold.mkdir()
    (gold / "judged-cases.jsonl").write_text(
        '{"query": "q", "gold": ["t1"], "snapshot_id": "bbbbbbbbbbbbbbbb"}\n')
    (gold / "judged-cases.floor.json").write_text(json.dumps(
        {"mrr": 0.43, "success10": 0.90, "recall10": 0.85, "ndcg10": 0.52}))
    # findability is not on disk at all — the ledger is what remembers it was
    # measured last run, so --require can still name it as missing.
    (tmp_path / "gold-runs.jsonl").write_text(json.dumps({
        "kind": "gold-run", "files": {
            "judged-cases.jsonl": {"status": "scored"},
            "findability-cases.jsonl": {"status": "scored"}}}) + "\n")
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(snap))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(gold))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE", raising=False)

    assert gate.main(["--require"]) == 1
    out = capsys.readouterr().out
    assert "judged-cases.jsonl" in out and "stale" in out
    assert "findability-cases.jsonl" in out and "missing" in out


def test_require_fails_when_nothing_is_calibrated(tmp_path, monkeypatch, capsys) -> None:
    # No floors manifest means nothing is gated. Under --require that is the
    # failure itself: a gate measuring nothing would otherwise read green.
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "snapshot.json").write_text('{"snapshot_id": "aaaaaaaaaaaaaaaa"}')
    gold = tmp_path / "gold"
    gold.mkdir()
    (gold / "judged-cases.jsonl").write_text(
        '{"query": "q", "gold": ["t1"], "snapshot_id": "bbbbbbbbbbbbbbbb"}\n')
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(snap))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(gold))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("THREAD_ARCHIVE_GOLD_GATE_MAINTENANCE", raising=False)

    assert gate.main(["--require"]) == 1
    assert gate.FLOOR_SUFFIX in capsys.readouterr().out


def test_no_require_still_skips_missing_fixtures_green(tmp_path, monkeypatch) -> None:
    # The default contract is unchanged: without --require, a missing snapshot is
    # a green skip so a dev box without the 20 GB fixture isn't wedged red.
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(tmp_path / "no-snap"))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(tmp_path))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))
    assert gate.main([]) == 0


def test_absent_snapshot_skips_green(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(tmp_path / "no-snap"))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(tmp_path))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))
    assert gate.main() == 0
    assert "no snapshot" in capsys.readouterr().out


def test_stale_golds_skip_green_without_scoring(tmp_path, monkeypatch, capsys) -> None:
    # Snapshot present but the gold's id doesn't match it — the gate must skip
    # (returning 0) before it ever tries to score, so a mid-re-mine window is a
    # skip, not a red and not a model load.
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "snapshot.json").write_text('{"snapshot_id": "aaaaaaaaaaaaaaaa"}')
    gold = tmp_path / "gold"
    gold.mkdir()
    (gold / "judged-cases.jsonl").write_text(
        '{"query": "q", "gold": ["t1"], "snapshot_id": "bbbbbbbbbbbbbbbb"}\n'
    )
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(snap))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(gold))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))

    assert gate.main() == 0
    out = capsys.readouterr().out
    assert "SKIP" in out and "stale" in out


def test_no_gold_files_skips_green(tmp_path, monkeypatch, capsys) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "snapshot.json").write_text('{"snapshot_id": "aaaaaaaaaaaaaaaa"}')
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("THREAD_ARCHIVE_SNAP", str(snap))
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_DIR", str(empty))
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path))

    assert gate.main() == 0
    assert "no gold files" in capsys.readouterr().out


# --- fail-early -------------------------------------------------------------


def _progress(*, n, scored, mrr=0.0, success10=0.0, recall10=0.0, ndcg10=0.0,
              query="q", case_rr=1.0):
    from search_lab.eval_core import EvalProgress

    return EvalProgress(
        n=n, scored=scored,
        sums={"mrr": mrr, "success10": success10,
              "recall10": recall10, "ndcg10": ndcg10},
        query=query, case_rr=case_rr,
    )


def test_bound_never_aborts_a_run_that_could_still_pass() -> None:
    # The soundness property the whole --fail-early design rests on: a run is cut
    # short only when NO ordering of the remaining cases could reach the floor.
    # Here 4 of 10 cases scored 0, but 6 perfect ones would land exactly on 0.6.
    watch = gate.FloorWatch({"mrr": 0.6})
    assert watch(_progress(n=10, scored=4, mrr=0.0)) is None


def test_bound_aborts_once_the_floor_is_out_of_reach() -> None:
    # One more zero and the ceiling is 5/10 = 0.5 < 0.6.
    watch = gate.FloorWatch({"mrr": 0.6})
    reason = watch(_progress(n=10, scored=5, mrr=0.0))
    assert reason and "mrr" in reason and "0.6" in reason


def test_bound_watches_every_floored_metric_not_just_mrr() -> None:
    watch = gate.FloorWatch({"mrr": 0.1, "recall10": 0.9})
    reason = watch(_progress(n=10, scored=5, mrr=5.0, recall10=0.0))
    assert reason and "recall10" in reason


def test_a_passing_run_is_never_aborted() -> None:
    watch = gate.FloorWatch({"mrr": 0.6, "success10": 0.9, "ndcg10": 0.5})
    for k in range(1, 11):
        assert watch(_progress(n=10, scored=k, mrr=float(k),
                               success10=float(k), ndcg10=float(k))) is None


def test_regression_watch_counts_only_cases_that_used_to_rank() -> None:
    # A case that already scored 0 at the baseline is not a regression, however
    # many of them there are — otherwise ordering hardest-first would abort every
    # run, passing ones included.
    watch = gate.FloorWatch({"mrr": 0.0}, baseline={"a": 0.0, "b": 0.0},
                            max_regressions=1)
    assert watch(_progress(n=10, scored=1, query="a", case_rr=0.0)) is None
    assert watch(_progress(n=10, scored=2, query="b", case_rr=0.0)) is None
    assert watch.regressions == []


def test_regression_watch_fires_on_newly_unfindable_cases() -> None:
    watch = gate.FloorWatch({"mrr": 0.0}, baseline={"a": 1.0, "b": 0.5},
                            max_regressions=2)
    assert watch(_progress(n=10, scored=1, query="a", case_rr=0.0)) is None
    reason = watch(_progress(n=10, scored=2, query="b", case_rr=0.0))
    assert reason and "2 cases" in reason
    assert watch.regressions == ["a", "b"]


def test_regression_watch_is_inert_without_a_baseline() -> None:
    watch = gate.FloorWatch({"mrr": 0.0}, baseline={}, max_regressions=1)
    assert watch(_progress(n=10, scored=1, query="a", case_rr=0.0)) is None


def test_cases_run_best_baseline_first_unknown_cases_last() -> None:
    cases = [{"query": "weak"}, {"query": "new"}, {"query": "strong"}]
    ordered = gate._order_cases(cases, {"weak": 0.2, "strong": 1.0})
    assert [c["query"] for c in ordered] == ["strong", "weak", "new"]


def test_order_is_identity_without_a_baseline() -> None:
    cases = [{"query": "a"}, {"query": "b"}]
    assert gate._order_cases(cases, {}) == cases


# --- --set ------------------------------------------------------------------


def test_set_coerces_to_the_declared_field_type() -> None:
    params, overrides = gate._apply_overrides(
        ["fusion_weight=500", "pool_floor=300", "rerank_auto=true"])
    assert params.fusion_weight == 500.0 and isinstance(params.fusion_weight, float)
    assert params.pool_floor == 300 and isinstance(params.pool_floor, int)
    assert params.rerank_auto is True
    assert overrides == {"fusion_weight": 500.0, "pool_floor": 300, "rerank_auto": True}


def test_set_leaves_untouched_fields_at_the_shipped_values() -> None:
    from thread_archive._retrieval import SearchParams

    params, _ = gate._apply_overrides(["fusion_weight=500"])
    assert params.density_weight == SearchParams().density_weight


def test_set_rejects_an_unknown_field() -> None:
    # A typo'd knob that silently does nothing scores identically to the baseline
    # and reads as "this knob has no effect" — the exact false negative the
    # tuning loop exists to avoid.
    import pytest

    with pytest.raises(SystemExit, match="fusion_wieght"):
        gate._apply_overrides(["fusion_wieght=500"])
    with pytest.raises(SystemExit, match="field=value"):
        gate._apply_overrides(["fusion_weight"])
