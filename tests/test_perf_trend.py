"""The perf trend blob: calibrators, ledger reading, and normalization.

The calibrators are the part with a correctness claim worth holding: they must
be deterministic workloads that produce a positive timing anywhere the bench
runs, because a calibrator that errors out on some machine silently turns the
CI artifact into raw wall clock. Everything else here is shape — the blob a
trend line will be drawn through has to keep naming the same things.
"""

from __future__ import annotations

import json
from pathlib import Path

from search_lab import bench_runs, benchmark, perf_trend


def test_every_manifest_row_names_an_arm_a_calibrator_covers() -> None:
    # Normalization is keyed off the arm tag in the row name. A manifest row
    # whose name carries no recognized tag would travel raw-only, which is a
    # silent hole in the comparable series — so a new row has to either carry a
    # tag or be a deliberate exception added here.
    for row in benchmark.manifest():
        for name in (row.name, row.quick().name):
            assert perf_trend.arm_of(name) in perf_trend.ARM_CALIBRATORS, name


def test_arm_of_reads_the_tag_not_the_dataset() -> None:
    assert perf_trend.arm_of("beam:100K[vectors]") == "vectors"
    assert perf_trend.arm_of("perltqa[lexical]~1200") == "lexical"
    assert perf_trend.arm_of("something-untagged") is None


def test_matvec_calibrator_times_the_pinned_workload() -> None:
    result = perf_trend.calibrate_matvec(rounds=2, reps=4)
    assert result["seconds"] > 0
    assert len(result["rounds"]) == 2
    assert result["seconds"] == min(result["rounds"])


def test_fts_calibrator_times_the_pinned_workload() -> None:
    result = perf_trend.calibrate_fts(rounds=2, reps=2)
    assert result["seconds"] > 0
    assert len(result["rounds"]) == 2
    assert result["seconds"] == min(result["rounds"])


def test_emit_reports_the_latest_ok_run_per_row(tmp_path: Path) -> None:
    home = tmp_path / "state"
    # An older run, a newer one that supersedes it, a failed one that must not,
    # and an untagged row that gets no normalized value.
    bench_runs.record_run(row="beir:scifact[vectors]", argv=[], corpus_id="c1",
                          measures={"ndcg10": 0.5}, elapsed_s=100.0, status="ok",
                          home=home)
    bench_runs.record_run(row="beir:scifact[vectors]", argv=[], corpus_id="c1",
                          measures={"ndcg10": 0.6}, elapsed_s=120.0, status="ok",
                          home=home)
    bench_runs.record_run(row="locomo[lexical]", argv=[], corpus_id="c2",
                          measures={"recall10": 0.4}, elapsed_s=33.0, status="failed",
                          home=home)
    bench_runs.record_run(row="untagged-row", argv=[], corpus_id="c3",
                          measures={"x": 1.0}, elapsed_s=9.0, status="ok",
                          home=home)

    blob = perf_trend.emit(home=home, rounds=1)

    assert blob["rows"]["beir:scifact[vectors]"]["elapsed_s"] == 120.0
    assert "locomo[lexical]" not in blob["rows"]
    assert blob["rows"]["untagged-row"]["arm"] is None
    assert "untagged-row" not in blob["normalized"]
    expected = round(120.0 / blob["calibration"]["matvec"]["seconds"], 1)
    assert blob["normalized"]["beir:scifact[vectors]"] == expected
    assert blob["env"]["python"]
    assert blob["kind"] == "perf-trend"


def test_cli_writes_the_blob_it_prints(tmp_path: Path, capsys) -> None:
    out = tmp_path / "perf-trend.json"
    assert perf_trend.main(["--json", str(out), "--rounds", "1"]) == 0
    printed = capsys.readouterr().out
    blob = json.loads(out.read_text(encoding="utf-8"))
    assert json.loads(printed) == blob
    assert set(blob) == {"kind", "at", "commit", "env", "calibration",
                        "rows", "normalized"}
    assert {"matvec", "fts"} <= set(blob["calibration"])
