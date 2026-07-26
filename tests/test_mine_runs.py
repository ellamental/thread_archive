"""The mining run ledger (``search_lab/mine_runs.py``).

The denominator behind a mined benchmark — how many units a run attempted, and
the per-unit outcome breakdown (a judge's ``none-of-pool``, a generator's drop) —
is what a recall-blind protocol must not lose to a green console line. These pin
that it is recorded, read back newest-first, and stays fail-soft telemetry.
"""

from __future__ import annotations

import json

from search_lab import mine_runs


def test_record_and_read_round_trips_newest_first(tmp_path) -> None:
    mine_runs.record_run(
        miner="rerank", snapshot_id="snap-1", attempted=10, written=8, failed=2,
        outcomes={"ok": 8, "none-of-pool": 2}, home=tmp_path)
    mine_runs.record_run(
        miner="querygen", snapshot_id="snap-1", attempted=5, written=3, failed=2,
        outcomes={"ok": 3, "no-queries": 2}, home=tmp_path)

    runs = mine_runs.read_runs(tmp_path)
    assert [r["miner"] for r in runs] == ["querygen", "rerank"]  # newest first
    rerank = runs[1]
    assert rerank["attempted"] == 10 and rerank["written"] == 8
    assert rerank["outcomes"] == {"ok": 8, "none-of-pool": 2}
    assert rerank["kind"] == "mine-run" and "commit" in rerank


def test_read_runs_limit_and_missing_ledger(tmp_path) -> None:
    for i in range(3):
        mine_runs.record_run(
            miner=f"m{i}", snapshot_id="s", attempted=1, written=1, failed=0,
            outcomes={"ok": 1}, home=tmp_path)
    assert len(mine_runs.read_runs(tmp_path, limit=2)) == 2
    assert mine_runs.read_runs(tmp_path / "nope") == []  # no ledger yet → empty


def test_ledger_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_MINE_RUNS_LOG", "0")
    mine_runs.record_run(
        miner="rerank", snapshot_id="s", attempted=1, written=1, failed=0,
        outcomes={"ok": 1}, home=tmp_path)
    assert not (tmp_path / mine_runs.LEDGER_FILE).exists()


def test_record_run_is_fail_soft_on_a_bad_home(tmp_path) -> None:
    # A ledger write must never break the mining run it records: an unwritable
    # path is logged and swallowed, not raised.
    clash = tmp_path / "file"
    clash.write_text("x")
    mine_runs.record_run(
        miner="rerank", snapshot_id="s", attempted=1, written=1, failed=0,
        outcomes={"ok": 1}, home=clash)  # home is a file, not a dir
    assert clash.read_text() == "x"  # untouched, no exception


def test_written_records_are_valid_jsonl(tmp_path) -> None:
    mine_runs.record_run(
        miner="rerank", snapshot_id="s", attempted=2, written=1, failed=1,
        outcomes={"ok": 1, "agent-failed": 1}, home=tmp_path)
    line = (tmp_path / mine_runs.LEDGER_FILE).read_text().strip()
    assert json.loads(line)["outcomes"]["agent-failed"] == 1
