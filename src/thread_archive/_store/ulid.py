"""ULID minting and validation — the thread-id format.

A thread id is a canonical ULID: 26 characters of Crockford base32 encoding a
48-bit millisecond timestamp followed by 80 bits of randomness. Properties the
archive leans on:

- **Lexicographic order is chronological order** — ``ORDER BY id`` sorts threads
  by start time, and the id itself carries when the thread began.
- **No separators, no ambiguous characters** — double-click selects the whole
  token; I/L/O/U are excluded from the alphabet, and decoding accepts their
  confusable aliases (i/l → 1, o → 0) plus lowercase.
- **Globally unique** — 80 random bits means ids from independently-created
  archives can be merged without collision or rewriting.

``mint_ulid`` accepts an explicit timestamp so ids minted for historical
threads (import backfill, migration) sort by the time the conversation actually
started rather than the time the id was assigned.

The integer ids threads carried before ULIDs live on as ``Thread.legacy_id``,
resolved by :func:`thread_archive._retrieval.read.resolve_thread_ref`.
"""

from __future__ import annotations

import os
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_DECODE: dict[str, int] = {c: i for i, c in enumerate(CROCKFORD)}
_DECODE.update({c.lower(): i for c, i in list(_DECODE.items())})
_DECODE.update({"I": 1, "i": 1, "L": 1, "l": 1, "O": 0, "o": 0})

ULID_LEN = 26
_MAX = 1 << 128


def mint_ulid(timestamp_ms: int | None = None) -> str:
    """A fresh canonical ULID. ``timestamp_ms`` (unix milliseconds) defaults to
    now; pass it explicitly when minting for a historical thread."""
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    value = ((timestamp_ms & ((1 << 48) - 1)) << 80) | int.from_bytes(os.urandom(10), "big")
    return _encode(value)


def _encode(value: int) -> str:
    return "".join(CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def normalize_ulid(ref: str) -> str | None:
    """The canonical (uppercase, de-confused) form of ``ref`` if it is a valid
    ULID, else None. Accepts lowercase and the Crockford confusables (i/l/o)."""
    if len(ref) != ULID_LEN:
        return None
    value = 0
    for ch in ref:
        v = _DECODE.get(ch)
        if v is None:
            return None
        value = (value << 5) | v
    if value >= _MAX:  # first char encodes >3 bits — out of the 128-bit range
        return None
    return _encode(value)


def ulid_timestamp_ms(ulid: str) -> int | None:
    """The embedded unix-millisecond timestamp, or None for an invalid ULID."""
    canonical = normalize_ulid(ulid)
    if canonical is None:
        return None
    value = 0
    for ch in canonical:
        value = (value << 5) | _DECODE[ch]
    return value >> 80
