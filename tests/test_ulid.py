"""ULID minting and validation (_store.ulid): canonical form, confusable
aliases, and the rejection arms."""

from __future__ import annotations

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
