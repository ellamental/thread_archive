"""Strip the legacy ``{thread_id}:`` prefix from dedup_keys.

A retired code path wrote ``dedup_key`` as ``{thread_id}:{compute_dedup_key(...)}``;
the current importer writes the bare ``compute_dedup_key(...)`` form (dedup is already
thread-scoped by the ``WHERE thread_id = ...`` clause, so the prefix is redundant, and
there is no global unique index that would need it). The two formats coexist, so a
re-import — which computes the bare form — won't recognize a prefixed row and would
duplicate it. This one-shot pass aligns the prefixed rows to the current bare format.

Safe: it only removes a leading ``{thread_id}:`` that is literally present (a bare
key's anchor is a uuid / ``c=<hash>`` / ``queue-…`` / ``file-snapshot-N`` / ``tool=…`` —
never the thread's own integer id, so a bare key can't be mistaken for a prefixed one).
A stripped key can only equal another row's key when they are the same event identity.
Dry-run by default; ``--apply`` writes per-thread with a row-level backup (id + original
key) so it is reversible.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import Text, func, select, update

from .._store import Event, get_session

logger = logging.getLogger(__name__)

_PREFIXED = Event.dedup_key.like(func.cast(Event.thread_id, Text).concat(":%"))


def _prefixed_thread_ids(session, limit: Optional[int]) -> list[int]:
    """Threads holding at least one ``{thread_id}:``-prefixed dedup_key."""
    q = select(Event.thread_id).where(Event.dedup_key.is_not(None), _PREFIXED).group_by(Event.thread_id)
    if limit is not None:
        q = q.limit(limit)
    return list(session.execute(q).scalars().all())


def plan_thread(session, thread_id: int) -> tuple[list[tuple[int, str, str]], dict]:
    """Return (updates, stats): (event_id, original_key, bare_key) for each prefixed row."""
    prefix = f"{thread_id}:"
    rows = list(
        session.execute(
            select(Event.id, Event.dedup_key).where(
                Event.thread_id == thread_id, Event.dedup_key.is_not(None)
            )
        ).all()
    )
    existing_bare = {k for _i, k in rows if not k.startswith(prefix)}
    stats: dict[str, int] = defaultdict(int)
    updates: list[tuple[int, str, str]] = []
    for eid, key in rows:
        if not key.startswith(prefix):
            continue
        bare = key[len(prefix):]
        if bare in existing_bare:
            # same identity already present bare — a genuine duplicate row; de-prefixing
            # is still correct (both collapse to one identity).
            stats["collapses_onto_existing"] += 1
        updates.append((eid, key, bare))
        stats["stripped"] += 1
    return updates, stats


def run(*, apply: bool = False, limit: Optional[int] = None, backup_path: Optional[Path] = None) -> dict:
    totals: dict[str, Any] = defaultdict(int)
    backup = open(backup_path, "a", encoding="utf-8") if (apply and backup_path) else None
    try:
        with get_session() as s:
            thread_ids = _prefixed_thread_ids(s, limit)
        totals["threads"] = len(thread_ids)
        for tid in thread_ids:
            with get_session() as s:
                updates, stats = plan_thread(s, tid)
                totals["stripped"] += len(updates)
                totals["collapses"] += stats.get("collapses_onto_existing", 0)
                if not updates:
                    continue
                totals["threads_changed"] += 1
                if apply:
                    if backup:
                        backup.write(json.dumps({
                            "thread_id": tid,
                            "rows": [{"id": eid, "old": old} for eid, old, _bare in updates],
                        }) + "\n")
                        backup.flush()
                    for eid, _old, bare in updates:
                        s.execute(update(Event).where(Event.id == eid).values(dedup_key=bare))
                    s.commit()
    finally:
        if backup:
            backup.close()
    return dict(totals)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap threads")
    ap.add_argument("--backup", type=Path, default=None, help="row-level backup JSONL (apply)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    totals = run(apply=args.apply, limit=args.limit, backup_path=args.backup)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] denamespace-dedup-keys")
    print(f"  threads with prefixed keys:  {totals.get('threads', 0):,}")
    print(f"  threads changed:             {totals.get('threads_changed', 0):,}")
    print(f"  keys de-prefixed:            {totals.get('stripped', 0):,}")
    print(f"  collapse-onto-existing-bare: {totals.get('collapses', 0):,}")


if __name__ == "__main__":
    main()
