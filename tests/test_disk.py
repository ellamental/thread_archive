"""Disk accounting: the split behind ``status``'s ``disk:`` line and the viewer's
storage section.

The interesting cases are all about *classification*, because the total is
arithmetic but the split is a judgment: get a kind wrong and the page tells
someone their conversations are rebuildable, or that a rebuildable projection is
irreplaceable.
"""

from __future__ import annotations

import os

import pytest

from thread_archive._ops.disk import KINDS, disk_usage, format_bytes


def write(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


@pytest.fixture
def populated(archive_home):
    """A home with one entry of every kind, at sizes that can't be confused."""
    write(archive_home / "truth" / "threads" / "a.jsonl", 4000)
    write(archive_home / "index.db", 8000)
    write(archive_home / "index.db-wal", 100)
    write(archive_home / "vector-pack" / "pack.npy", 900)
    write(archive_home / "source-mirror" / "claude-code" / "s.jsonl.gz", 2000)
    write(archive_home / "dumps" / "drift" / "raw.jsonl", 500)
    write(archive_home / "logs" / "watch.log", 300)
    write(archive_home / "pre-ulid-backup" / "old.jsonl", 7000)
    return archive_home


def test_kinds_sum_to_the_total(populated):
    """Every byte lands in exactly one kind — the split is an accounting, not a
    sample, so a reader can trust the parts against the whole."""
    d = disk_usage()
    assert sum(d["kinds"].values()) == d["total_bytes"] == 22800
    assert d["files"] == 8
    assert set(d["kinds"]) == set(KINDS)


def test_classification(populated):
    """Truth is irreplaceable, the projection over it is not, retained provider
    material is its own thing, and everything else is 'other'."""
    kinds = disk_usage()["kinds"]
    assert kinds["truth"] == 4000
    # index.db + its WAL sibling + the vector pack beside it.
    assert kinds["index"] == 8000 + 100 + 900
    assert kinds["sources"] == 2000 + 500
    # The migration leftover and the logs — unlabelled accumulation.
    assert kinds["other"] == 7000 + 300


def test_rebuildable_is_the_index(populated):
    d = disk_usage()
    assert d["rebuildable_bytes"] == d["kinds"]["index"] == 9000


def test_entries_are_largest_first_and_named(populated):
    """The named entries are what makes a total actionable: 'other 7 KB' is a
    shrug, 'pre-ulid-backup 7 KB' is a decision."""
    entries = disk_usage()["entries"]
    assert [e["bytes"] for e in entries] == sorted(
        (e["bytes"] for e in entries), reverse=True
    )
    by_name = {e["name"]: e for e in entries}
    assert by_name["index.db"]["kind"] == "index"
    assert by_name["pre-ulid-backup"]["kind"] == "other"
    assert by_name["source-mirror"]["kind"] == "sources"


def test_top_bounds_the_names_not_the_totals(populated):
    d = disk_usage(top=2)
    assert len(d["entries"]) == 2
    assert d["total_bytes"] == 22800


def test_the_default_measures_rather_than_reusing(populated):
    """An operator who just reclaimed space and asked what it saved must be told
    the new number. Reuse is a polling concession, so it is never the default."""
    assert disk_usage()["total_bytes"] == 22800
    write(populated / "truth" / "threads" / "b.jsonl", 1000)
    assert disk_usage()["total_bytes"] == 23800


def test_a_stated_budget_reuses_the_last_measurement(populated):
    """What the served endpoint asks for: a poll has no use for a number more
    precise than its own interval, and this is what stops a room full of viewers
    from walking the disk once each."""
    assert disk_usage(max_age_s=60)["total_bytes"] == 22800
    write(populated / "truth" / "threads" / "b.jsonl", 1000)
    assert disk_usage(max_age_s=60)["total_bytes"] == 22800, "walked again anyway"
    # The budget bounds staleness; it does not pin the number forever.
    assert disk_usage()["total_bytes"] == 23800


def test_top_is_sliced_off_a_shared_measurement(populated):
    """``top`` is presentation. Two callers wanting different amounts of detail
    are one walk, and neither may see the other's slice."""
    wide = disk_usage(max_age_s=60)
    narrow = disk_usage(top=2, max_age_s=60)
    assert len(narrow["entries"]) == 2
    assert len(wide["entries"]) > 2, "the shared measurement was truncated in place"
    assert narrow["total_bytes"] == wide["total_bytes"]


def test_concurrent_callers_share_one_walk(populated):
    """A walk that contends with another walk is many times slower than either
    alone, which is how a polled endpoint turns a 0.3s measurement into tens of
    seconds. Overlapping callers wait for the walk in flight instead.

    Timing-free: whether these four threads actually overlap is the scheduler's
    business, so this pins only what must hold either way — nobody sees a wrong
    number, and four callers never cost four walks. The sharing mechanism itself
    is driven deterministically in ``test_shared_work.py``."""
    import threading

    from thread_archive._ops.disk import measure_stats

    before = measure_stats()["computed"]
    out: list[dict] = []
    lock = threading.Lock()

    def ask() -> None:
        d = disk_usage()
        with lock:
            out.append(d)

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)

    assert len(out) == 4
    assert {d["total_bytes"] for d in out} == {22800}
    assert measure_stats()["computed"] - before <= 4


def test_absent_home_reports_zeros_without_creating_it(tmp_path, monkeypatch):
    """Asking an archive its size must never be what brings it into existence —
    a status read on a machine that has not been set up creates nothing."""
    from thread_archive import _config as config

    home = tmp_path / "never-made"
    monkeypatch.setenv(config.ENV_HOME, str(home))
    d = disk_usage()
    assert d["total_bytes"] == 0
    assert d["kinds"] == dict.fromkeys(KINDS, 0)
    assert not home.exists()


def test_truth_outside_the_home_is_still_counted(tmp_path, archive_home, monkeypatch):
    """Truth and index have location overrides. A total that ignored them would
    under-report the archive by however much of it lives elsewhere."""
    from thread_archive import _config as config

    elsewhere = tmp_path / "elsewhere-truth"
    write(elsewhere / "threads" / "a.jsonl", 5000)
    monkeypatch.setenv(config.ENV_TRUTH, str(elsewhere))
    write(archive_home / "index.db", 1000)

    d = disk_usage()
    assert d["kinds"]["truth"] == 5000
    assert d["total_bytes"] == 6000
    assert str(elsewhere) in d["external"]


def test_symlinks_are_not_followed(tmp_path, archive_home):
    """A symlink out of the home would otherwise report someone else's disk as
    this archive's — a backup mirror linked in would double the total."""
    outside = tmp_path / "backup-mirror"
    write(outside / "big.jsonl", 50_000)
    os.symlink(outside, archive_home / "mirror-link")
    write(archive_home / "truth" / "a.jsonl", 100)

    d = disk_usage()
    assert d["total_bytes"] < 1000


def test_unreadable_entry_is_skipped_not_fatal(archive_home):
    """Advisory reporting over a directory a live watcher writes to: a race or a
    permission wall rounds the number, it does not raise."""
    write(archive_home / "truth" / "a.jsonl", 100)
    blocked = archive_home / "blocked"
    blocked.mkdir()
    write(blocked / "inner.jsonl", 400)
    blocked.chmod(0o000)
    try:
        d = disk_usage()
    finally:
        blocked.chmod(0o755)
    assert d["kinds"]["truth"] == 100


@pytest.fixture
def reportable(archive_home):
    """A home the status commands can actually open: no hand-written ``index.db``
    (they open the engine, which needs a real one), but the same unlabelled pile
    around it that makes the report worth printing."""
    write(archive_home / "truth" / "threads" / "a.jsonl", 4000)
    write(archive_home / "source-mirror" / "claude-code" / "s.jsonl.gz", 2000)
    write(archive_home / "logs" / "watch.log", 300)
    write(archive_home / "pre-ulid-backup" / "old.jsonl", 7000)
    return archive_home


def test_status_prints_the_split_and_names_the_biggest_of_the_rest(reportable, capsys):
    """``status``'s whole job here is to make a large number answerable: the
    split says which part is which, and the follow-up names the entries that are
    neither truth nor index — the pile that grows without anything labelling it."""
    from thread_archive.cli import main

    assert main(["status"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(("disk:", "   "))]
    disk = next(ln for ln in lines if ln.startswith("disk:"))
    assert "truth" in disk and "(rebuildable)" in disk and "sources" in disk
    largest = next(ln for ln in lines if "largest:" in ln)
    # Named by entry, and never the truth or index already accounted for above.
    assert "pre-ulid-backup" in largest
    assert "index.db" not in largest and "truth " not in largest


def test_setup_status_names_what_is_rebuildable(reportable, capsys):
    """The friendly status answers 'what is this costing me' — and immediately
    says how much of it is the projection rather than the conversations."""
    import argparse

    from thread_archive._setup.wizard import print_status

    print_status(argparse.Namespace(home=None))
    disk = next(ln for ln in capsys.readouterr().out.splitlines() if "disk:" in ln)
    assert "conversations" in disk and "rebuildable index" in disk


def test_format_bytes_reads_as_an_operator_expects():
    assert format_bytes(0) == "0 B"
    assert format_bytes(999) == "999 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024**2) == "1.0 MB"
    assert format_bytes(3 * 1024**3) == "3.0 GB"
    # Past the largest unit it keeps growing rather than inventing one.
    assert format_bytes(4096 * 1024**3) == "4096.0 GB"
    assert format_bytes(None) == "?"
