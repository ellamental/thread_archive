"""The gold-gate run ledger records each run's measured baseline as a timeseries."""

from __future__ import annotations

import json

from thread_archive._ops import gold_runs

FILES = {
    "judged-cases.jsonl": {
        "n": 21, "mrr": 0.46, "success10": 0.81, "recall10": 0.72,
        "ndcg10": 0.48, "p50_ms": 1100.0, "status": "ok",
    },
}


def _records(home):
    path = home / gold_runs.LEDGER_FILE
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_record_and_read_round_trip(archive_home) -> None:
    gold_runs.record_run(archive_home, snapshot_id="snap123", files=FILES,
                         passed=True, config={"params": {"rerank_pool": 12}}, commit="abc1234")
    (rec,) = _records(archive_home)
    assert rec["kind"] == "gold-run"
    assert rec["snapshot_id"] == "snap123" and rec["commit"] == "abc1234"
    assert rec["passed"] is True
    assert rec["files"]["judged-cases.jsonl"]["mrr"] == 0.46
    assert rec["config"]["params"]["rerank_pool"] == 12
    # read_runs returns newest-first
    runs = gold_runs.read_runs(archive_home)
    assert len(runs) == 1 and runs[0]["snapshot_id"] == "snap123"


def test_read_runs_newest_first_and_limit(archive_home) -> None:
    for i in range(3):
        gold_runs.record_run(archive_home, snapshot_id=f"s{i}", files=FILES,
                             passed=True, config={}, commit=f"c{i}")
    runs = gold_runs.read_runs(archive_home, limit=2)
    assert [r["snapshot_id"] for r in runs] == ["s2", "s1"]  # newest first, capped


def test_missing_ledger_reads_empty(archive_home) -> None:
    assert gold_runs.read_runs(archive_home) == []


def test_disabled_by_env(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_GOLD_RUNS_LOG", "0")
    gold_runs.record_run(archive_home, snapshot_id="s", files=FILES, passed=True,
                         config={}, commit="c")
    assert _records(archive_home) == []


def test_write_failure_is_fail_soft(archive_home) -> None:
    # The ledger path is a directory, so the append raises inside the product.
    blocked = archive_home / gold_runs.LEDGER_FILE
    blocked.mkdir()
    gold_runs.record_run(archive_home, snapshot_id="s", files=FILES, passed=True,
                         config={}, commit="c")  # must not raise
    assert blocked.is_dir() and not any(blocked.iterdir())


def test_active_config_captures_params_and_arm_state(archive_home, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK", "off")
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED", raising=False)
    cfg = gold_runs.active_config()
    # The shipped defaults are captured — the "which config produced these" record.
    assert cfg["params"]["rerank_pool"] == 12 and cfg["params"]["rerank_doc_chars"] == 768
    assert cfg["rerank"] == "off" and cfg["embed"] == "on"


# --- the per-case baseline ---------------------------------------------------


BASE_CASES = {"findability-cases.jsonl": {"where did we discuss X": 1.0,
                                          "the thing about Y": 0.0}}


def test_baseline_roundtrips(archive_home) -> None:
    gold_runs.write_baseline(archive_home, snapshot_id="snap1", files=BASE_CASES)
    assert gold_runs.read_baseline(archive_home, snapshot_id="snap1") == BASE_CASES


def test_baseline_from_another_snapshot_is_not_served(archive_home) -> None:
    # Per-case scores describe specific documents; under a different corpus they
    # would order the wrong cases first and invent regressions.
    gold_runs.write_baseline(archive_home, snapshot_id="snap1", files=BASE_CASES)
    assert gold_runs.read_baseline(archive_home, snapshot_id="snap2") == {}


def test_baseline_is_overwritten_not_appended(archive_home) -> None:
    gold_runs.write_baseline(archive_home, snapshot_id="s", files=BASE_CASES)
    gold_runs.write_baseline(archive_home, snapshot_id="s", files={"a.jsonl": {"q": 0.5}})
    assert gold_runs.read_baseline(archive_home, snapshot_id="s") == {"a.jsonl": {"q": 0.5}}


def test_missing_baseline_reads_empty(archive_home) -> None:
    assert gold_runs.read_baseline(archive_home, snapshot_id="s") == {}


def test_unreadable_baseline_reads_empty(archive_home) -> None:
    (archive_home / gold_runs.BASELINE_FILE).write_text("{not json")
    assert gold_runs.read_baseline(archive_home) == {}


def test_a_tuning_run_is_flagged_in_the_ledger(archive_home) -> None:
    # An experiment's numbers describe a candidate ranking; unflagged they read
    # as the baseline moving.
    gold_runs.record_run(archive_home, snapshot_id="s", files=FILES, passed=True,
                         overrides={"fusion_weight": 500.0})
    run = gold_runs.read_runs(archive_home)[0]
    assert run["overrides"] == {"fusion_weight": 500.0}


def test_a_plain_run_carries_no_overrides_key(archive_home) -> None:
    gold_runs.record_run(archive_home, snapshot_id="s", files=FILES, passed=True)
    assert "overrides" not in gold_runs.read_runs(archive_home)[0]


def test_active_config_records_the_params_actually_scored(archive_home) -> None:
    from thread_archive._retrieval import SearchParams

    cfg = gold_runs.active_config(SearchParams(fusion_weight=500.0))
    assert cfg["params"]["fusion_weight"] == 500.0
