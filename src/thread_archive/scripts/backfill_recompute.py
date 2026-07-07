"""Backfill NULL dedup_keys by recomputing each from the event's OWN persisted state.

Re-parsing the source (backfill_reconcile) only reaches threads whose source file
still exists — a small slice. Most NULL-key events are on threads whose transcript
was rotated away, or on live-captured threads that never had a provider source at
all. This backfill needs no source: ``dedup_key`` is a pure function of
``(provider_message_id, event_type, payload)`` — all recoverable from the row itself.

``provider_message_id`` is the one field not stored on every event: the builder
stamps every event of a turn with the turn's id but only writes it into the anchor
event's payload (``api_request_started`` / ``user_message_sent`` →
``provider_data.provider_message_id``). We regroup a thread's events by
``(stream_id, api_call_id)`` — exactly the events the builder built together — and
lift the id from the group's anchor, applying it to the group's NULL-key rows. A
group with no stored id falls back to the content-hash anchor, which is precisely
what the builder does for id-less providers, so the key still matches a re-import.

Safety: a recomputed key is deterministic and content-inclusive, so it can only ever
equal another row's key when the two rows are genuine duplicates — never a harmful
false collision. A recomputed key that would land on a *different* existing key is
skipped (left NULL), never written. Dry-run by default; ``--apply`` writes per-thread
with a row-level backup (every id set + its new key) so the pass is reversible.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select, update

from thread_import.event_builder import compute_dedup_key

from ..store import Event, ImportState, get_session
from .backfill_reconcile import _norm_key, _pmid

logger = logging.getLogger(__name__)

# Types whose provider_message_id is the turn's id — recoverable from the group's
# anchor event, so a recompute reproduces the builder's key (validated ~99.8% against
# already-keyed rows). Types NOT here (queue_operation, progress, file_snapshot,
# context_summary, model_change, ide_context, hook_context, and the live-capture
# *_delta stream) carry a *synthetic* id (``queue-remove-…``, ``file-snapshot-N``) that
# is not stored on the row, so a recompute would guess wrong — those are left NULL.
_RECOMPUTE_SAFE = frozenset({
    "api_request_started", "api_request_completed", "stream_completed",
    "text_complete", "tool_use_complete", "thinking_complete",
    "user_message_sent", "tool_execution_completed", "tool_execution_error",
    "tool_use_started",
})
# Anchor events store the turn's provider_message_id in their payload.
_ANCHOR_TYPES = frozenset({"api_request_started", "user_message_sent"})


def plan_thread(session, thread_id: int) -> tuple[list[tuple[int, str]], list[str], dict]:
    """Return (backfills, warnings, stats) for one thread — pure planning, no writes.

    Only whitelisted turn events in a group that contains its anchor are keyed, using
    the anchor's provider_message_id. This is the exact condition under which a
    recompute reproduces the builder's key; anything else is left NULL."""
    events = list(
        session.execute(
            select(Event).where(Event.thread_id == thread_id).order_by(Event.id)
        ).scalars().all()
    )
    stats: dict[str, int] = defaultdict(int)
    warnings: list[str] = []
    backfills: list[tuple[int, str]] = []

    # Keys already present in the thread, normalized to the current (un-prefixed)
    # format, so a recomputed key is recognized as colliding with an existing one.
    planned: set[str] = {_norm_key(e.dedup_key, thread_id) for e in events if e.dedup_key}

    groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in events:
        groups[(e.stream_id, e.api_call_id)].append(e)

    for evs in groups.values():
        nulls = [e for e in evs if e.dedup_key is None and e.event_type in _RECOMPUTE_SAFE]
        if not nulls:
            continue
        if not any(e.event_type in _ANCHOR_TYPES for e in evs):
            stats["no_anchor"] += len(nulls)   # can't source the turn id — leave NULL
            continue
        anchor_pmids = {
            p for p in (_pmid(e.payload or {}) for e in evs if e.event_type in _ANCHOR_TYPES)
            if p is not None
        }
        if len(anchor_pmids) > 1:
            warnings.append(f"group has {len(anchor_pmids)} anchor provider_message_ids")
            continue
        pmid = next(iter(anchor_pmids)) if anchor_pmids else ""
        for e in nulls:
            key = compute_dedup_key(pmid, e.event_type, e.payload or {})
            if key in planned:
                # would duplicate an existing/other row's key — genuine dup content;
                # leave NULL rather than write an ambiguous key.
                stats["collision"] += 1
                continue
            planned.add(key)
            backfills.append((e.id, key))
            stats["backfill"] += 1
    return backfills, warnings, stats


def _threads_with_null_keys(session, limit: Optional[int]) -> list[int]:
    """Import-derived threads (present in import_state) that have NULL-key events.

    Scoped to import-derived threads on purpose: dedup_key exists for *import*
    idempotence, and only these threads are ever re-imported. Live-captured threads
    (the original app's live event stream — no import_state, no source) are never
    re-imported, so their NULL keys are harmless; recompute skips them (their event
    structure also differs from the importer's and isn't validated here)."""
    imported = select(ImportState.thread_id).where(ImportState.thread_id.is_not(None))
    q = (
        select(Event.thread_id)
        .where(Event.dedup_key.is_(None), Event.thread_id.in_(imported))
        .group_by(Event.thread_id)
        .order_by(func.count().desc())
    )
    if limit is not None:
        q = q.limit(limit)
    return list(session.execute(q).scalars().all())


def run(*, apply: bool = False, limit: Optional[int] = None, backup_path: Optional[Path] = None) -> dict:
    totals: dict[str, Any] = defaultdict(int)
    backup = open(backup_path, "a", encoding="utf-8") if (apply and backup_path) else None
    try:
        with get_session() as s:
            thread_ids = _threads_with_null_keys(s, limit)
        totals["threads"] = len(thread_ids)
        for tid in thread_ids:
            with get_session() as s:
                try:
                    backfills, warnings, stats = plan_thread(s, tid)
                except Exception as e:  # noqa: BLE001
                    logger.warning("plan failed for thread %s: %s", tid, e)
                    totals["plan_errors"] += 1
                    continue
                totals["collisions"] += stats.get("collision", 0)
                if warnings:
                    totals["threads_with_warnings"] += 1
                    totals["warnings"] += len(warnings)
                if not backfills:
                    continue
                totals["threads_changed"] += 1
                totals["backfills"] += len(backfills)
                if apply:
                    if backup:
                        backup.write(json.dumps({"thread_id": tid, "backfills": backfills}) + "\n")
                        backup.flush()
                    for eid, key in backfills:
                        s.execute(update(Event).where(Event.id == eid).values(dedup_key=key))
                    s.commit()
    finally:
        if backup:
            backup.close()
    return dict(totals)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap threads (largest-NULL-count first)")
    ap.add_argument("--backup", type=Path, default=None, help="row-level backup JSONL (apply)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    totals = run(apply=args.apply, limit=args.limit, backup_path=args.backup)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] backfill-recompute (dedup_key from persisted payload)")
    print(f"  threads (w/ NULL keys): {totals.get('threads', 0):,}")
    print(f"  threads changed:        {totals.get('threads_changed', 0):,}")
    print(f"  dedup_key backfills:    {totals.get('backfills', 0):,}")
    print(f"  collisions (left NULL): {totals.get('collisions', 0):,}")
    print(f"  threads w/ warnings:    {totals.get('threads_with_warnings', 0):,}  (warnings={totals.get('warnings', 0):,})")
    print(f"  plan errors:            {totals.get('plan_errors', 0):,}")


if __name__ == "__main__":
    main()
