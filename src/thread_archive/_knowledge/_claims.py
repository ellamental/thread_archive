"""Lease-based work claims for parallel librarian backfill.

Static `id % N` sharding is rigid: you fix the worker count up front, a dead shard
stalls, and the modulo isn't index-sargable. Instead, workers **claim** a batch of
thread ids — stamped with a timestamp — into a shared state file, and a claim older than
the lease (default 1h) is treated as a dead worker's and reclaimed. So workers
self-balance (a fast one claims more), you can launch any number independently with no
coordinator, and a crash forfeits nothing but a one-hour wait on those few threads.

The claim file (`<home>/.librarian-claims.json`) is **ephemeral operator coordination**,
not truth — like a lock dir. Deleting it only drops in-flight leases; the archive (JSONL
truth + the topic citations/links that actually mark a thread done) is untouched. Concurrency
is an `flock`'d read-modify-write held only for the brief claim, never during the work.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DEFAULT_LEASE_SECONDS = 3600  # a claim older than this is a presumed-dead worker's


def claims_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / ".librarian-claims.json"


def _now() -> float:
    return time.time()


@contextmanager
def _locked(path: Path):
    """Yield the claims dict under an exclusive lock; persist it on clean exit.

    The lock is the claim file's own fd (``flock`` mutexes across processes *and*
    threads on separate descriptions). A corrupt file reads as empty. If the body
    raises, the file is left unchanged — a failed claim never half-writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
        raw = b"".join(chunks)
        try:
            claims = json.loads(raw) if raw.strip() else {}
        except ValueError:
            claims = {}
        yield claims
        os.lseek(fd, 0, 0)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(claims).encode("utf-8"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _prune_expired(claims: dict, lease: int, now: float) -> None:
    for tid in list(claims):
        if now - float(claims[tid].get("at", 0)) >= lease:
            del claims[tid]


def claim_review_batch(
    worker: str, batch: int, *, lease: int = DEFAULT_LEASE_SECONDS,
    exclude_source_id: Optional[str] = None,
) -> list[dict]:
    """Claim (and return) up to ``batch`` unreviewed conversations for ``worker``.

    Under the lock: expire stale claims, ask the queue for eligible threads not claimed
    by *other* live workers (resuming this worker's own still-unreviewed claims first),
    stamp them to now, and drop this worker's claims that have since left the queue
    (reviewed). Returns the queue rows the caller should process."""
    from .write import review_queue

    now = _now()
    with _locked(claims_path()) as claims:
        _prune_expired(claims, lease, now)
        others = [int(t) for t, c in claims.items() if c.get("worker") != worker]
        rows = review_queue(
            limit=batch, exclude_source_id=exclude_source_id, exclude_ids=others
        )
        returned = {r["id"] for r in rows}
        for r in rows:
            claims[str(r["id"])] = {"worker": worker, "at": now}
        # forget my claims for threads no longer eligible (reviewed, or bumped out of
        # the window) so stale leases don't accumulate under my name
        for tid in [t for t, c in claims.items() if c.get("worker") == worker and int(t) not in returned]:
            del claims[tid]
        return rows


def has_claimable_work(
    *, lease: int = DEFAULT_LEASE_SECONDS, exclude_source_id: Optional[str] = None
) -> bool:
    """Whether any unreviewed conversation is free to claim (not held by a live lease).
    The driver's respawn condition."""
    from .write import review_queue

    now = _now()
    with _locked(claims_path()) as claims:
        _prune_expired(claims, lease, now)
        active = [int(t) for t in claims]
        return bool(review_queue(limit=1, exclude_source_id=exclude_source_id, exclude_ids=active))
