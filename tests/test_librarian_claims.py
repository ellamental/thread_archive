"""Lease-claim coordination for parallel librarian backfill.

Replaces static sharding: workers claim batches under a timestamped lease in a shared
state file, so concurrent instances self-balance with no overlap and a dead worker's
claims expire and get reclaimed. These tests prove disjointness, resume, lease expiry,
the driver's "is there work left" check, and the review_queue self-claim integration.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

import thread_archive as ta
from thread_archive.knowledge import add_topic_evidence, create_topic, review_queue
from thread_archive.knowledge._claims import claim_review_batch, claims_path, has_claimable_work
from thread_archive.store import Event, Thread, get_session

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed(n: int) -> list[int]:
    ids: list[int] = []
    with get_session() as s:
        for i in range(n):
            t = Thread(
                name=f"claude-code:c{i}", title=f"c{i}", thread_type="conversation",
                source="claude-code", source_id=f"c{i}",
            )
            s.add(t)
            s.flush()
            s.add(Event(thread_id=t.id, stream_id=f"c{i}", event_type="user_message_sent",
                        payload={"content": "x"}, occurred_at=NOW))
            ids.append(t.id)
        s.commit()
    return ids


def test_two_workers_claim_disjoint_batches(archive_home):
    ta.open_archive()
    _seed(6)
    a = {r["id"] for r in claim_review_batch("A", 3)}
    b = {r["id"] for r in claim_review_batch("B", 3)}
    assert len(a) == 3 and len(b) == 3
    assert a.isdisjoint(b)


def test_worker_resumes_its_own_claim(archive_home):
    ta.open_archive()
    _seed(4)
    first = {r["id"] for r in claim_review_batch("A", 2)}
    again = {r["id"] for r in claim_review_batch("A", 2)}
    assert first == again  # same worker, same still-unreviewed batch — resume, not advance


def test_fresh_lease_blocks_others_stale_is_reclaimed(archive_home):
    ta.open_archive()
    seeded = set(_seed(2))
    a = {r["id"] for r in claim_review_batch("A", 2, lease=3600)}
    assert a == seeded
    # B can't take A's fresh claims
    assert claim_review_batch("B", 2, lease=3600) == []
    # with the lease elapsed, A's claims are reclaimable
    b = {r["id"] for r in claim_review_batch("B", 2, lease=0)}
    assert b == seeded


def test_has_claimable_work_reflects_live_leases(archive_home):
    ta.open_archive()
    _seed(2)
    assert has_claimable_work() is True
    claim_review_batch("A", 5)            # claims both, fresh
    assert has_claimable_work() is False  # nothing free to claim
    assert has_claimable_work(lease=0) is True  # leases elapsed → free again


def test_reviewed_threads_leave_the_claimable_set(archive_home):
    ta.open_archive()
    ids = _seed(2)
    claim_review_batch("A", 5)
    # 'Reviewed' is derived from curation now: cite a message from each thread.
    topic = create_topic("T")["topic_id"]
    with get_session() as s:
        eids = {
            tid: s.execute(select(Event.id).where(Event.thread_id == tid)).scalars().first()
            for tid in ids
        }
    for tid in ids:
        add_topic_evidence(topic, eids[tid], tid, "q")
    # both cited → reviewed → no claimable work even though the claims linger
    assert has_claimable_work(lease=0) is False


def test_review_queue_self_claims_when_worker_set(archive_home, monkeypatch):
    monkeypatch.setenv("THREAD_ARCHIVE_LIBRARIAN_WORKER", "wX")
    ta.open_archive()
    _seed(3)
    rows = review_queue(limit=2)
    assert len(rows) == 2

    claims = json.loads(claims_path().read_text())
    assert len(claims) == 2
    assert all(c["worker"] == "wX" for c in claims.values())


def test_review_queue_plain_read_without_worker(archive_home):
    ta.open_archive()
    _seed(2)
    rows = review_queue(limit=5)
    assert len(rows) == 2
    assert not claims_path().exists()  # no claim file written on a plain read
