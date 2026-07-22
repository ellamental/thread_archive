"""The retrieval gold gate: floors ratchet, and a stale or absent fixture skips
green rather than wedging the commit gate red.

Only the pure logic and the skip paths are exercised here — scoring a gold file
needs the 20 GB snapshot and the model arms, which live on the operator's box,
not in the unit suite. Those paths are covered by the CI row itself.
"""

from __future__ import annotations

import importlib.util
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
        "topic-cases-suicide.jsonl",
        "topic-cases-frustration.jsonl",
        "judged-cases-detail.jsonl",
        "topic-cases-suicide.detail.jsonl",
        "judged-seed.jsonl",
        "seed-candidates.jsonl",
        "judged-cases.jsonl.until-bak",
    ):
        (tmp_path / name).write_text("{}\n")

    found = {p.name for p in gate.discover_gold_files(tmp_path)}
    assert found == {
        "judged-cases.jsonl",
        "topic-cases-suicide.jsonl",
        "topic-cases-frustration.jsonl",
    }


def test_discover_missing_dir_is_empty(tmp_path) -> None:
    assert gate.discover_gold_files(tmp_path / "nope") == []


def test_every_floor_names_a_gold_shaped_file() -> None:
    # A floor keyed to a name the discovery filter would reject can never fire —
    # it would gate a file the gate never sees. Keep floors keyed to real gold
    # basenames.
    markers = gate._NON_GOLD_MARKERS
    for name, floor in gate.FLOORS.items():
        assert name.endswith(".jsonl") and "cases" in name
        assert not any(marker in name for marker in markers), name
        assert set(floor) == {"mrr", "success10", "recall10", "ndcg10"}


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
