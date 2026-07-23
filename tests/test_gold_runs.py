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
