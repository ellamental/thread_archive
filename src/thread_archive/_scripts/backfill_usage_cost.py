"""Backfill cost + full usage onto stored ``api_request_completed`` events.

The importer historically lifted only the input/output/thinking token trio from
a turn's usage; ``cost`` and the remaining usage fields (cache read/write
counts, provider extras) were dropped at import. The current parser + builder
preserve them, so a fresh re-parse of each thread's source carries the values
its stored events lack — and because none of those fields are content-hash
material, the stored event and its fresh twin share the same ``dedup_key``.

This script re-parses each source (the Claude-Code-shaped transcripts), matches
every fresh ``api_request_completed`` event to its stored row — by ``dedup_key``
first, content anchor as the NULL-key fallback — and merges the *missing*
non-content fields onto the stored payload via the amendment mechanism
(:mod:`thread_archive._ops.amend`): a superseding truth line per event, the
original preserved as history, provenance in ``truth/amendments.jsonl``.

Missing-only merge: a key already present on the stored payload is never
overwritten, even when the fresh value differs — such value drift is counted
and reported, not resolved. Idempotent: a re-run finds nothing missing.
Dry-run by default; ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select

from .._importers._read import read_session_lines
from .._ops.amend import amend_event_payloads, check_patch
from .._store import Event, ImportState, get_session
from .backfill_reconcile import (
    _content_anchor,
    _fresh_events,
    _iter_pairs,
    _norm_key,
)

logger = logging.getLogger(__name__)

TARGET_TYPE = "api_request_completed"


def plan_thread(session, thread_id: str, source: str, lines: list[dict]) -> dict:
    """Plan the usage/cost patches for one thread. Pure planning — no writes.

    Returns ``{"patches": [(event_id, patch)], "stats": {...}}``. A fresh event
    that matches no stored row, an ambiguous anchor match, or a stored key
    already-present-with-a-different-value are all counted, never guessed at."""
    stats: dict[str, int] = defaultdict(int)
    fresh = [e for e in _fresh_events(source, lines) if e.event_type == TARGET_TYPE]
    stored = list(
        session.execute(
            select(Event).where(
                Event.thread_id == thread_id, Event.event_type == TARGET_TYPE
            ).order_by(Event.id)
        ).scalars().all()
    )
    by_key = {_norm_key(e.dedup_key, thread_id): e for e in stored if e.dedup_key}
    by_anchor: dict[tuple, list] = defaultdict(list)
    for e in stored:
        by_anchor[_content_anchor(e.event_type, e.payload or {}, e.occurred_at)].append(e)

    patches: list[tuple[int, dict]] = []
    used: set[int] = set()
    for f in fresh:
        match = by_key.get(f.dedup_key)
        if match is None:
            candidates = [
                e for e in by_anchor.get(
                    _content_anchor(f.event_type, f.payload, f.occurred_at), []
                )
                if e.id not in used
            ]
            if len(candidates) > 1:
                stats["ambiguous"] += 1
                continue
            match = candidates[0] if candidates else None
        if match is None or match.id in used:
            stats["unmatched"] += 1
            continue
        used.add(match.id)
        old = match.payload if isinstance(match.payload, dict) else {}
        patch = {k: v for k, v in f.payload.items() if k not in old and v is not None}
        stats["value_drift"] += sum(
            1 for k, v in f.payload.items()
            if k in old and old.get(k) != v and k not in ("content_blocks",)
        )
        if not patch or check_patch(old, patch) is not None:
            stats["already_complete"] += 1
            continue
        for k in patch:
            stats[f"field:{k}"] += 1
        patches.append((match.id, patch))
    stats["patches"] = len(patches)
    return {"patches": patches, "stats": dict(stats)}


def run(
    *,
    apply: bool = False,
    limit: Optional[int] = None,
    sources: Optional[set[str]] = None,
    pairs: Optional[Iterable[tuple[str, Path, str]]] = None,
) -> dict:
    """Sweep every source transcript, plan per thread, amend on ``apply``.

    ``pairs`` overrides on-disk discovery (tests feed scripted stores);
    ``sources`` filters by source name (default: all discovered)."""
    totals: dict[str, Any] = defaultdict(int)
    examined = 0
    for name, path, source_id in (pairs if pairs is not None else _iter_pairs()):
        if sources and name not in sources:
            continue
        if limit is not None and examined >= limit:
            break
        with get_session() as s:
            state = s.execute(
                select(ImportState).where(
                    ImportState.source == name, ImportState.source_id == source_id
                )
            ).scalar_one_or_none()
            if state is None or not state.thread_id:
                continue
            examined += 1
            totals["threads"] += 1
            thread_id = state.thread_id
            try:
                lines = read_session_lines(path)
                plan = plan_thread(s, thread_id, name, lines)
            except Exception as e:  # noqa: BLE001
                logger.warning("plan failed for %s (%s): %s", source_id, path, e)
                totals["plan_errors"] += 1
                continue
        for k, v in plan["stats"].items():
            totals[k] += v
        if plan["patches"]:
            totals["threads_changed"] += 1
            if apply:
                res = amend_event_payloads(
                    ((thread_id, eid, patch) for eid, patch in plan["patches"]),
                    reason=f"backfill_usage_cost: {name}/{source_id}",
                )
                totals["events_amended"] += res["events_amended"]
    return dict(totals)


def main(
    argv: Optional[list[str]] = None,
    *,
    pairs: Optional[Iterable[tuple[str, Path, str]]] = None,
) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--source", action="append", default=None,
        help="restrict to a source (repeatable; default: all discovered)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    totals = run(
        apply=args.apply, limit=args.limit,
        sources=set(args.source) if args.source else None, pairs=pairs,
    )
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] backfill-usage-cost ({TARGET_TYPE} amendments)")
    print(f"  threads examined:   {totals.get('threads', 0):,}")
    print(f"  threads changed:    {totals.get('threads_changed', 0):,}")
    print(f"  patches planned:    {totals.get('patches', 0):,}")
    if args.apply:
        print(f"  events amended:     {totals.get('events_amended', 0):,}")
    for k in sorted(totals):
        if k.startswith("field:"):
            print(f"    {k[6:]:<22}{totals[k]:,}")
    print(f"  already complete:   {totals.get('already_complete', 0):,}")
    print(f"  unmatched fresh:    {totals.get('unmatched', 0):,}")
    print(f"  ambiguous (skip):   {totals.get('ambiguous', 0):,}")
    print(f"  value drift (kept): {totals.get('value_drift', 0):,}")
    print(f"  plan errors:        {totals.get('plan_errors', 0):,}")


if __name__ == "__main__":
    main()
