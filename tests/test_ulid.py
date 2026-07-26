"""ULID minting and validation (_store.ulid): canonical form, confusable
aliases, and the rejection arms."""

from __future__ import annotations

import os
import threading
import time

import pytest

from thread_archive._store import ulid
from thread_archive._store.ulid import (
    ULID_LEN,
    mint_ulid,
    normalize_ulid,
    ulid_timestamp_ms,
)


def test_mint_default_now_and_explicit_timestamp_sort_chronologically() -> None:
    old = mint_ulid(1_000)  # explicit historical timestamp
    new = mint_ulid()  # defaults to now
    assert len(old) == len(new) == ULID_LEN
    # lexicographic order is chronological order — the property ORDER BY id leans on
    assert old < new
    # a minted id is already canonical
    assert normalize_ulid(old) == old
    assert normalize_ulid(new) == new


def test_normalize_rejects_wrong_length() -> None:
    assert normalize_ulid("") is None
    assert normalize_ulid("0" * (ULID_LEN - 1)) is None
    assert normalize_ulid("0" * (ULID_LEN + 1)) is None


def test_normalize_rejects_excluded_alphabet_characters() -> None:
    # U is excluded from Crockford base32 and has no confusable alias
    assert normalize_ulid("U" * ULID_LEN) is None
    assert normalize_ulid("0" * (ULID_LEN - 1) + "!") is None


def test_normalize_rejects_out_of_range_value() -> None:
    # The first char encodes more than 3 bits: a leading 'Z' overflows 128 bits.
    assert normalize_ulid("Z" * ULID_LEN) is None
    # '7' is the largest legal leading character (7 << 125 < 2**128).
    assert normalize_ulid("7" + "Z" * (ULID_LEN - 1)) is not None


def test_normalize_maps_confusables_and_lowercase_to_canonical() -> None:
    # o → 0, i/l → 1, lowercase accepted — all normalize to the canonical form
    assert normalize_ulid("o" + "i" * (ULID_LEN - 1)) == "0" + "1" * (ULID_LEN - 1)
    assert normalize_ulid("0" + "l" * (ULID_LEN - 1)) == "0" + "1" * (ULID_LEN - 1)
    canonical = mint_ulid(123_456)
    assert normalize_ulid(canonical.lower()) == canonical


def test_ulid_timestamp_ms_round_trips_and_rejects_invalid() -> None:
    ts = 1_234_567_890_123
    assert ulid_timestamp_ms(mint_ulid(ts)) == ts
    # accepts non-canonical input via normalization
    assert ulid_timestamp_ms(mint_ulid(ts).lower()) == ts
    assert ulid_timestamp_ms("not-a-ulid") is None


# ── monotonic minting ────────────────────────────────────────────────────────


class _FrozenClock:
    """Stand-in for the module's ``time``, so a test can pin the millisecond
    without touching the real clock for everything else running in-process."""

    def __init__(self, ms: int) -> None:
        self.ms = ms

    def time_ns(self) -> int:
        return self.ms * 1_000_000


@pytest.fixture(autouse=True)
def _fresh_carry():
    """Each test starts with no carried state and gets it cleared afterwards —
    the carry is process-global, so a pinned clock in one test would otherwise
    decide what the next test's first mint looks like."""
    ulid._carry, ulid._carry_pid = None, None
    yield
    ulid._carry, ulid._carry_pid = None, None


def test_same_millisecond_mints_are_strictly_increasing() -> None:
    """The property ORDER BY id leans on, at sub-millisecond resolution: the
    timestamp cannot separate these ids, so the random field has to."""
    ulid.time = _FrozenClock(1_700_000_000_000)
    try:
        ids = [mint_ulid() for _ in range(500)]
    finally:
        ulid.time = time
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)
    # every id still reports the millisecond it was minted in
    assert {ulid_timestamp_ms(i) for i in ids} == {1_700_000_000_000}


def test_clock_stepped_backwards_still_mints_forwards() -> None:
    """NTP correction or suspend/resume must not produce an id that sorts before
    one already handed out."""
    ulid.time = _FrozenClock(1_700_000_000_000)
    try:
        first = mint_ulid()
        ulid.time = _FrozenClock(1_600_000_000_000)  # clock jumps back 100s
        second = mint_ulid()
    finally:
        ulid.time = time
    assert second > first
    # the carried timestamp wins over the regressed clock
    assert ulid_timestamp_ms(second) == 1_700_000_000_000


def test_explicit_timestamp_is_outside_the_monotonic_sequence() -> None:
    """A historical mint neither reads nor advances the carry, so a backfill
    cannot drag the live sequence to a wrong millisecond."""
    ulid.time = _FrozenClock(1_700_000_000_000)
    try:
        live = mint_ulid()
        carried = ulid._carry
        # a future-dated historical mint must not pin the sequence forward
        assert ulid_timestamp_ms(mint_ulid(2_000_000_000_000)) == 2_000_000_000_000
        assert ulid._carry == carried
        assert mint_ulid() > live
        assert ulid_timestamp_ms(mint_ulid()) == 1_700_000_000_000
    finally:
        ulid.time = time


def test_exhausted_random_field_borrows_a_millisecond() -> None:
    """2**80 mints in one millisecond is unreachable, but the wrap is handled
    rather than left to produce a *smaller* id than the one before it."""
    ulid.time = _FrozenClock(1_700_000_000_000)
    try:
        ulid._carry = (1_700_000_000_000, ulid._RANDOM_MAX)
        ulid._carry_pid = os.getpid()
        nxt = mint_ulid()
    finally:
        ulid.time = time
    assert ulid_timestamp_ms(nxt) == 1_700_000_000_001


def test_carry_resets_when_the_pid_changes() -> None:
    """After a fork both sides inherit the carry; incrementing from one value in
    two processes hands out the same id twice. A pid change drops it instead."""
    ulid.time = _FrozenClock(1_700_000_000_000)
    try:
        mint_ulid()
        ulid._carry = (1_700_000_000_500, 7)  # as if inherited from a parent
        ulid._carry_pid = os.getpid() + 1  # ...in a different process
        fresh = mint_ulid()
    finally:
        ulid.time = time
    assert ulid_timestamp_ms(fresh) == 1_700_000_000_000  # own clock, not the carry
    assert ulid._carry_pid == os.getpid()


def test_concurrent_mints_are_unique_and_ordered() -> None:
    """The carry is a read-modify-write and the watcher imports on a threadpool."""
    ulid.time = _FrozenClock(1_700_000_000_000)
    out: list[list[str]] = []
    lock = threading.Lock()

    def mint_many() -> None:
        batch = [mint_ulid() for _ in range(200)]
        with lock:
            out.append(batch)

    try:
        threads = [threading.Thread(target=mint_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        ulid.time = time
    flat = [i for batch in out for i in batch]
    assert len(set(flat)) == 8 * 200  # no ties under contention
    for batch in out:
        assert batch == sorted(batch)  # each thread sees its own mints in order
