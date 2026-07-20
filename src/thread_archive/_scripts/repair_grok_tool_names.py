"""One-off repair for three grok threads (ids 3716011 / 3716012 / 3716013,
sessions of 2026-06-29) whose live tail-import baked in chunking artifacts: a
``tool_result`` processed in a later poll than its ``tool_calls`` line lost the
id->name map and recorded ``tool_name: "unknown"``, and several thinking/text
blocks were anchored at a stale previous-turn timestamp. The monorepo pg store
tailed the same sessions on its own schedule and froze *different* artifacts,
so the ingest-cutover soak's byte-level content check flags these threads as
divergent on every rotation pass — blocking the week-of-aligned-runs cut bar.

The fix aligns both stores to a canonical deterministic parse: this repo's own
importer run over the *complete* ``chat_history.jsonl`` files (no chunk
boundaries). This script applies the standalone half of the patch plan (21
events); its sibling in the monorepo
(``archive/src/archive/scripts/repair_grok_tool_names.py``) applies the pg
half. The plan (``repair_grok_tool_names_plan_20260704.json``, untracked in
``host/repair-dumps/`` — it holds real conversation payloads, so it never
enters git) carries old + new values; old payloads are asserted before writing and
the changed rows are dumped to a backup file first. Truth-file history is
inherent: the corrected event lines append via the normal ``append_event_row``
seam and reindex is last-wins by id, so the pre-repair lines remain in the
per-thread JSONL as history. FTS rows for the patched events are re-indexed
(tool_name is an FTS column); vectors are left alone (embedded text is
unchanged).

Preview (read-only): .venv/bin/python src/thread_archive/_scripts/repair_grok_tool_names.py
Apply:               .venv/bin/python src/thread_archive/_scripts/repair_grok_tool_names.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import delete

from thread_archive._api import open_archive
from thread_archive._retrieval.fts import index_events
from thread_archive._store import use_session
from thread_archive._store.models import Event, EventFts
from thread_archive._truth.jsonl_log import append_event_row

# Undo/plan dumps live in host/repair-dumps (outside the package tree, so they
# never ship in the wheel — tests/meta/test_package_tree.py ratchets this).
DUMPS_DIR = Path(__file__).resolve().parents[3] / "host" / "repair-dumps"
PLAN_PATH = DUMPS_DIR / "repair_grok_tool_names_plan_20260704.json"


def backup_path_for(plan_path: Path, *, now: Optional[datetime] = None) -> Path:
    """Where a run dumps the rows it is about to change: beside the plan it read,
    stamped at the moment the dump is written so consecutive runs never overwrite
    each other's undo file."""
    stamp = now or datetime.now(timezone.utc)
    return plan_path.parent / f"repair_grok_tool_names_backup_{stamp:%Y%m%dT%H%M%SZ}.json"


def canonical_json(payload) -> str:
    obj = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def run(
    *,
    apply: bool = False,
    plan_path: Optional[Path] = None,
    backup_path: Optional[Path] = None,
) -> int:
    """Apply (or preview) the plan's standalone half. Returns the process exit code."""
    plan_path = plan_path or PLAN_PATH
    patches = json.loads(plan_path.read_text())["sa"]
    print(f"{len(patches)} standalone patches loaded from {plan_path.name}")

    open_archive(None)
    with use_session() as session:
        events: dict[int, Event] = {}
        for p in patches:
            ev = session.get(Event, p["event_id"])
            if ev is None:
                print(f"ABORT: event {p['event_id']} not found")
                return 1
            if canonical_json(ev.payload) != p["old_payload"]:
                print(f"ABORT: event {p['event_id']} payload no longer matches plan")
                return 1
            if ev.event_type != p["old_event_type"]:
                print(f"ABORT: event {p['event_id']} type {ev.event_type} != plan")
                return 1
            events[p["event_id"]] = ev

        for p in patches:
            changes = []
            if p["old_event_type"] != p["new_event_type"]:
                changes.append(f"type {p['old_event_type']} -> {p['new_event_type']}")
            if p["old_occurred_at"] != p["new_occurred_at"]:
                changes.append(f"ts {p['old_occurred_at']} -> {p['new_occurred_at']}")
            if p["old_payload"] != p["new_payload"]:
                changes.append("payload")
            print(f"  {p['event_id']}: {', '.join(changes)}")

        if not apply:
            print("\npreview only — rerun with --apply to write")
            return 0

        backup = backup_path or backup_path_for(plan_path)
        backup.write_text(json.dumps(
            [{"id": ev.id, "thread_id": ev.thread_id, "event_type": ev.event_type,
              "occurred_at": str(ev.occurred_at), "payload": ev.payload,
              "dedup_key": ev.dedup_key}
             for ev in events.values()], indent=1, default=str))
        print(f"\nbackup written: {backup}")

        for p in patches:
            ev = events[p["event_id"]]
            ev.event_type = p["new_event_type"]
            ev.occurred_at = datetime.fromisoformat(p["new_occurred_at"])
            ev.payload = json.loads(p["new_payload"])
            if p.get("new_dedup_key"):
                ev.dedup_key = p["new_dedup_key"]
            append_event_row(session, ev)

        ids = list(events)
        # The shadow delete cascades into event_search via the sync triggers.
        session.execute(delete(EventFts).where(EventFts.event_id.in_(ids)))
        session.flush()
        indexed = index_events(session, list(events.values()))
        session.commit()
        print(f"applied {len(patches)} standalone event repairs; re-indexed {indexed} FTS rows")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: preview only)")
    ap.add_argument("--plan", type=Path, default=PLAN_PATH, help="patch plan JSON")
    ap.add_argument(
        "--backup", type=Path, default=None,
        help="undo dump for the rows about to change "
             "(default: beside the plan, stamped when it is written)",
    )
    args = ap.parse_args(argv)
    return run(apply=args.apply, plan_path=args.plan, backup_path=args.backup)


if __name__ == "__main__":
    sys.exit(main())
