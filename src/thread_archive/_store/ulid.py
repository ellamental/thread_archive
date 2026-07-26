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
- **Monotonic within a process** — the millisecond only orders ids *between*
  milliseconds, so re-randomizing the low 80 bits leaves same-millisecond ids in
  random relative order. Two threads created in the same millisecond would then
  sort arbitrarily, and a clock stepped backwards would mint ids that sort before
  ones already handed out. Minting for *now* therefore carries the previous id's
  random field forward and increments it whenever the clock has not advanced past
  the last mint, so ``ORDER BY id`` is creation order at any resolution.

``mint_ulid`` accepts an explicit timestamp so ids minted for historical
threads (import backfill, migration) sort by the time the conversation actually
started rather than the time the id was assigned. Such a mint is outside the
monotonic sequence in both directions: it neither consults nor advances the
carried state, so a backfill full of historical — or bad, future-dated —
timestamps cannot drag the live sequence with it.

The integer ids threads carried before ULIDs live on as ``Thread.legacy_id``,
resolved by :func:`thread_archive._retrieval.read.resolve_thread_ref`.
"""

from __future__ import annotations

import os
import threading
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_DECODE: dict[str, int] = {c: i for i, c in enumerate(CROCKFORD)}
_DECODE.update({c.lower(): i for c, i in list(_DECODE.items())})
_DECODE.update({"I": 1, "i": 1, "L": 1, "l": 1, "O": 0, "o": 0})

ULID_LEN = 26
_MAX = 1 << 128

_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1
_TS_MASK = (1 << 48) - 1

# The last (timestamp_ms, random) minted for *now* — the monotonic carry (see the
# module docstring). Guarded by a lock because minting is a read-modify-write and
# the watcher imports on a threadpool. Owned by the process that set it: after a
# fork both sides inherit the same carry, so a pid change resets it rather than
# letting two processes increment from one value and collide.
_carry_lock = threading.Lock()
_carry: tuple[int, int] | None = None
_carry_pid: int | None = None


def _random() -> int:
    return int.from_bytes(os.urandom(_RANDOM_BITS // 8), "big")


def _compose(timestamp_ms: int, rand: int) -> int:
    return ((timestamp_ms & _TS_MASK) << _RANDOM_BITS) | rand


def _mint_monotonic(now_ms: int) -> int:
    """The next id value for ``now_ms``, never tying or preceding the last one.

    ``now_ms <= last`` covers both cases that break ordering with one rule: a
    second mint inside the same millisecond, and a clock stepped backwards (NTP,
    a suspend/resume). Both keep the last timestamp and increment the random
    field, so the sequence advances on the clock's behalf. Exhausting the 80-bit
    field within one millisecond would take 2**80 mints, but it is handled rather
    than left to wrap into a smaller id: borrow a millisecond from the future and
    start a fresh random there."""
    global _carry, _carry_pid
    with _carry_lock:
        pid = os.getpid()
        if pid != _carry_pid:
            _carry, _carry_pid = None, pid
        if _carry is not None and now_ms <= _carry[0]:
            timestamp_ms, rand = _carry[0], _carry[1] + 1
            if rand > _RANDOM_MAX:
                timestamp_ms, rand = timestamp_ms + 1, _random()
        else:
            timestamp_ms, rand = now_ms, _random()
        _carry = (timestamp_ms, rand)
    return _compose(timestamp_ms, rand)


def mint_ulid(timestamp_ms: int | None = None) -> str:
    """A fresh canonical ULID. ``timestamp_ms`` (unix milliseconds) defaults to
    now; pass it explicitly when minting for a historical thread.

    Minting for now is monotonic within the process; an explicit timestamp is a
    historical mint and sits outside that sequence. See the module docstring."""
    if timestamp_ms is not None:
        return _encode(_compose(timestamp_ms, _random()))
    return _encode(_mint_monotonic(time.time_ns() // 1_000_000))


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
