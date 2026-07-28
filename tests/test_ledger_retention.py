"""Rotation retains: a capped ledger keeps its history in stamped segments.

The cap exists to bound what one read walks. It must never bound what the archive
remembers — an analysis that silently lost its older half is worse than no
analysis, because it still produces a plausible number.
"""

from __future__ import annotations

import json

from thread_archive._ops import ledger


def test_rotation_keeps_the_old_file_instead_of_replacing_it(tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    for i in range(6):
        ledger.append(path, {"i": i}, max_bytes=20)  # tiny cap: rotates most rows
    kept = [r["i"] for r in ledger.iter_rows(path)]
    assert kept == [0, 1, 2, 3, 4, 5], "every appended row must survive rotation"
    assert len(ledger.segments(path)) > 1, "the cap must actually have rotated"


def test_two_rotations_in_one_second_do_not_collide(tmp_path) -> None:
    # The stamp is per-second, so a burst is exactly where a naive scheme loses a
    # file to a same-name replace.
    path = tmp_path / "burst.jsonl"
    for i in range(12):
        ledger.append(path, {"i": i}, max_bytes=1)
    assert [r["i"] for r in ledger.iter_rows(path)] == list(range(12))


def test_segments_are_ordered_oldest_first_with_the_live_file_last(tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    for i in range(4):
        ledger.append(path, {"i": i}, max_bytes=15)
    segs = ledger.segments(path)
    assert segs[-1] == path, "the file still being appended to sorts last"
    assert [r["i"] for r in ledger.iter_rows(path)] == [0, 1, 2, 3]


def test_iter_rows_newest_first_reverses_segments_and_lines(tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    for i in range(5):
        ledger.append(path, {"i": i}, max_bytes=15)
    assert [r["i"] for r in ledger.iter_rows(path, newest_first=True)] == [4, 3, 2, 1, 0]


def test_a_legacy_numeric_rotation_is_read_and_sorts_before_stamped_ones(tmp_path) -> None:
    # An install that rotated under the old scheme has a `.jsonl.1` on disk; its
    # rows are history too.
    path = tmp_path / "l.jsonl"
    (tmp_path / "l.jsonl.1").write_text(json.dumps({"i": -1}) + "\n", encoding="utf-8")
    ledger.append(path, {"i": 0}, max_bytes=10_000)
    assert [r["i"] for r in ledger.iter_rows(path)] == [-1, 0]


def test_unrelated_siblings_are_not_read_as_telemetry(tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    (tmp_path / "l.jsonl.superseded").write_text(json.dumps({"i": 99}) + "\n", encoding="utf-8")
    (tmp_path / "l.jsonl.bak").write_text("not json at all\n", encoding="utf-8")
    ledger.append(path, {"i": 0}, max_bytes=10_000)
    assert [r["i"] for r in ledger.iter_rows(path)] == [0]


def test_a_torn_final_line_does_not_cost_the_rest_of_the_file(tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    ledger.append(path, {"i": 0}, max_bytes=10_000)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"i": 1, "half')  # a concurrent append caught mid-write
    assert [r["i"] for r in ledger.iter_rows(path)] == [0]


def test_reading_a_ledger_nobody_has_written_is_empty_not_an_error(tmp_path) -> None:
    assert list(ledger.iter_rows(tmp_path / "absent.jsonl")) == []
    assert ledger.segments(tmp_path / "absent.jsonl") == []
    assert ledger.total_bytes(tmp_path / "absent.jsonl") == 0


def test_total_bytes_counts_every_retained_segment(tmp_path) -> None:
    path = tmp_path / "l.jsonl"
    for i in range(6):
        ledger.append(path, {"i": i}, max_bytes=20)
    assert ledger.total_bytes(path) > path.stat().st_size, (
        "retained history costs more than the live file — that is the tradeoff, "
        "and it has to be visible"
    )


def test_an_unwritable_ledger_does_not_raise(tmp_path) -> None:
    # A telemetry write must never break the work it describes.
    ledger.append(tmp_path / "nope.jsonl" / "deeper.jsonl", {"i": 0}, max_bytes=10)


def test_env_max_bytes_falls_back_on_junk(monkeypatch) -> None:
    monkeypatch.setenv("X_LEDGER_CAP", "not-a-number")
    assert ledger.env_max_bytes("X_LEDGER_CAP", 123) == 123
    monkeypatch.setenv("X_LEDGER_CAP", "456")
    assert ledger.env_max_bytes("X_LEDGER_CAP", 123) == 456
