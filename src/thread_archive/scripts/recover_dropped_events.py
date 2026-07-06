"""Backfill import-dropped content the builder now preserves.

The ``DefaultEventBuilder`` used to silently drop three kinds of content it didn't
model — IDE context blocks (``<ide_selection>`` / ``<ide_opened_file>``), unmodeled
assistant block types (``server_tool_use``, ``web_search_tool_result``, images, …),
and messages whose role isn't user/assistant/system. The builder fix preserves them
going forward as ``ide_context`` / ``content_block`` / ``message`` events. This
one-shot backfill recovers the same events for **already-imported** CC-shaped threads
(``claude-code`` + ``cloth``) by re-parsing their still-on-disk source transcripts.

Why this is safe on the live memory-of-record — even though a full re-import is not:
it only ever inserts events whose ``event_type`` is in :data:`RECOVERABLE_TYPES`.
Those types were **never written before the fix**, so a fresh parse's ``dedup_key``
for them cannot collide with any existing row. Existing events are never rewritten,
so the bulk-seed dedup-key mismatch that makes a blanket re-import double events (the
hazard ``adopt_if_unwatermarked`` guards) simply doesn't apply here. Re-running is
idempotent: an already-recovered event's key is now present, so it's skipped.

Recovered events reuse the **existing** turn's ``stream_id`` / ``api_call_id`` (looked
up by the source message id recovered from each event's dedup-key anchor) so they land
in the right turn; a fully-dropped turn (e.g. an ide-only turn, or an unknown-role
turn) has no existing event to borrow from and gets a fresh stream.

Scope: CC-shaped sources only — that's where ``ide_context`` originates and the bulk of
the archive lives. Other providers (codex/grok/cursor/opencode/…) build events through
their own paths; their unmodeled-block/role incidence is low and is a separate follow-up.

Dry-run by default (reports what it *would* recover). Pass ``--apply`` to write.
Threads are re-parsed and committed one at a time, so the run is resumable and keeps
its lock windows short against the concurrently-writing watcher — but for a clean apply,
pausing the archive watcher first is recommended.
"""

from __future__ import annotations

import argparse
import logging
import uuid as _uuid
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import select

from thread_import import DefaultEventBuilder
from thread_import.parsers.claude_code import ClaudeCodeParser

from ..importers._events import _to_event
from ..importers._read import read_session_lines
from ..retrieval.fts import index_events
from ..store import Event, ImportState, get_session
from ..truth import write_events
from ..watcher.sources import ClaudeCodeWatcher, cloth_watcher

logger = logging.getLogger(__name__)

# Event types the fix newly preserves — the only rows this backfill ever inserts.
RECOVERABLE_TYPES = frozenset({"ide_context", "content_block", "message"})


def _existing(session, thread_id: int) -> tuple[set[str], dict[str, tuple[str, Optional[str]]]]:
    """Return ``(dedup_keys, anchor→(stream_id, api_call_id))`` for a thread's events.

    The anchor is the source message id (dedup-key prefix); it lets a recovered event
    borrow the ``stream_id`` / ``api_call_id`` of the turn it belongs to."""
    rows = session.execute(select(Event).where(Event.thread_id == thread_id)).scalars().all()
    keys = {e.dedup_key for e in rows if e.dedup_key}
    anchor: dict[str, tuple[str, Optional[str]]] = {}
    for e in rows:
        if e.dedup_key:
            anchor.setdefault(e.dedup_key.split(":", 1)[0], (e.stream_id, e.api_call_id))
    return keys, anchor


def plan_thread(session, thread_id: int, lines: list[dict]) -> list[Event]:
    """Re-parse a thread's source lines and return the recoverable Event rows not yet
    present. Pure planning — writes nothing."""
    parser = ClaudeCodeParser()
    builder = DefaultEventBuilder()
    session_data = {
        "provider": "claude-code",
        "sessions": [{"session_id": "recover", "project": "recover", "lines": lines}],
    }
    messages = parser.parse_export(session_data)
    existing_keys, anchor = _existing(session, thread_id)

    out: list[Event] = []
    prev = None
    for msg in messages:
        pmid = msg.get("provider_message_id") or msg.get("uuid") or ""
        stream_id, api_call_id = anchor.get(pmid, (str(_uuid.uuid4()), None))
        events = builder.build_events(msg, stream_id, api_call_id, prev_occurred_at=prev)
        if events:
            prev = events[-1].occurred_at
        for e in events:
            if (
                e.event_type in RECOVERABLE_TYPES
                and e.dedup_key
                and e.dedup_key not in existing_keys
            ):
                out.append(_to_event(thread_id, e))
                existing_keys.add(e.dedup_key)  # guard against in-file duplicates
    return out


def _iter_pairs() -> Iterator[tuple[str, Path, str]]:
    """Yield ``(source, path, source_id)`` for every on-disk CC-shaped transcript."""
    for watcher, name in ((ClaudeCodeWatcher(), "claude-code"), (cloth_watcher(), "cloth")):
        if not watcher.is_available():
            continue
        for path, source_id in watcher._iter_files():
            yield name, path, source_id


def run(*, apply: bool = False, limit: Optional[int] = None) -> dict:
    """Walk on-disk CC-shaped transcripts, backfilling recoverable events onto their
    already-imported threads. Returns a summary dict."""
    totals = {
        "files_seen": 0,
        "threads_mapped": 0,
        "threads_recovered": 0,
        "events_recovered": 0,
        "by_type": {},
        "read_errors": 0,
        "write_errors": 0,
    }
    examined = 0
    for name, path, source_id in _iter_pairs():
        totals["files_seen"] += 1
        if limit is not None and examined >= limit:
            break
        with get_session() as s:
            state = s.execute(
                select(ImportState).where(
                    ImportState.source == name, ImportState.source_id == source_id
                )
            ).scalar_one_or_none()
            if state is None or not state.thread_id:
                continue  # never imported (or merged elsewhere) — nothing to backfill
            totals["threads_mapped"] += 1
            examined += 1
            try:
                lines = read_session_lines(path)
            except Exception as e:  # noqa: BLE001
                logger.warning("read failed for %s (%s): %s", source_id, path, e)
                totals["read_errors"] += 1
                continue

            rows = plan_thread(s, state.thread_id, lines)
            if not rows:
                continue
            totals["threads_recovered"] += 1
            totals["events_recovered"] += len(rows)
            for r in rows:
                totals["by_type"][r.event_type] = totals["by_type"].get(r.event_type, 0) + 1

            if apply:
                try:
                    write_events(s, rows)
                    index_events(s, rows)
                    s.commit()
                except Exception as e:  # noqa: BLE001 — one bad thread must not abort the run
                    logger.warning("write failed for thread %s (%s): %s", state.thread_id, source_id, e)
                    totals["write_errors"] += 1
                    s.rollback()
    return totals


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write recovered events (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap threads examined (sampling)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    totals = run(apply=args.apply, limit=args.limit)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] recover-dropped-events")
    print(f"  files seen:        {totals['files_seen']}")
    print(f"  threads mapped:    {totals['threads_mapped']}")
    print(f"  threads w/ recovery: {totals['threads_recovered']}")
    print(f"  events recovered:  {totals['events_recovered']}")
    for t, n in sorted(totals["by_type"].items()):
        print(f"    {t:<14} {n}")
    if totals["read_errors"] or totals["write_errors"]:
        print(f"  read errors: {totals['read_errors']}  write errors: {totals['write_errors']}")


if __name__ == "__main__":
    main()
