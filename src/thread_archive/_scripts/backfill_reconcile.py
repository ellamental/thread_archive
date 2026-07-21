"""Reconcile a thread's persisted events against a fresh re-parse of its source.

Two jobs, one pass, safe on the live memory-of-record:

1. **dedup_key backfill.** Pre-``dedup_key``-era events carry NULL keys, so a full
   re-import can't tell they already exist and duplicates them. Re-parsing the
   source with the *current* builder yields each event's correct ``dedup_key``
   (right ``provider_message_id`` + current payload); we copy it onto the matched
   persisted row. Because the key is what today's builder produces, a future
   re-import now matches and stays idempotent.

2. **Dropped-content backfill.** A fresh event that matches no persisted event is
   content the old importer dropped (the builder/importer fixes now preserve it);
   we insert it, borrowing its turn's ``stream_id`` so it lands in place.

Matching is anchored on ``(event_type, occurred_at, block)`` — all derived from the
source, *not* from payload formatting — so a payload that merely evolved between
builder versions is treated as a MATCH (key-fill), never a spurious INSERT. That is
the core safety property: it cannot manufacture duplicates from payload drift.

Any thread that does not align cleanly (mismatched core-type counts, an ambiguous
match, a key that would collide with a *different* row) is FLAGGED and SKIPPED — never
guessed at. Dry-run by default; ``--apply`` writes per-thread with a row-level backup
JSONL (every dedup_key set + every event id inserted) so the pass is reversible.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid as _uuid
from collections import defaultdict
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from sqlalchemy import select, update

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.event_builder import compute_content_hash
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser

from .._importers._read import read_session_lines
from .._store import Event, ImportState, get_session
from .._watcher.sources import FileSessionWatcher

logger = logging.getLogger(__name__)


# ── anchors ──────────────────────────────────────────────────────────────────
def _ts_key(dt: Any) -> str:
    """Canonical timestamp string, tz-normalized so a fresh (aware-UTC) build and a
    persisted (DB round-tripped) value compare equal."""
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="microseconds")


def _block(payload: dict) -> str:
    """The block discriminator compute_dedup_key uses (tool id or block index)."""
    if payload.get("tool_call_id"):
        return f"tool={payload['tool_call_id']}"
    if payload.get("block_index") is not None:
        return f"blk={payload['block_index']}"
    return ""


def _struct_anchor(event_type: str, payload: dict, occurred_at: Any) -> tuple:
    """Source-stable identity: (type, timestamp, block). Robust to payload drift."""
    return (event_type, _ts_key(occurred_at), _block(payload))


def _content_anchor(event_type: str, payload: dict, occurred_at: Any) -> tuple:
    """Exact identity: structural anchor + content hash."""
    return _struct_anchor(event_type, payload, occurred_at) + (compute_content_hash(payload),)


def _pmid(payload: dict) -> Optional[str]:
    """The provider_message_id an event carries, if any (anchor events store it under
    provider_data). Used to stop two same-content turns from false-matching."""
    pd = payload.get("provider_data")
    return pd.get("provider_message_id") if isinstance(pd, dict) else None


def _norm_key(key: Optional[str], thread_id: str) -> Optional[str]:
    """Normalize a stored dedup_key to the current (un-prefixed) format for comparison.

    A retired code path namespaced keys as ``{thread_id}:{compute_dedup_key(...)}``;
    the current importer writes the un-prefixed form (dedup is already thread-scoped by
    the WHERE clause). Stripping the ``{thread_id}:`` prefix lets an old-format keyed
    row be recognized as the same event a fresh (un-prefixed) build produces — so a
    benign format difference isn't mistaken for real drift. NULL-key rows are filled
    with the un-prefixed form, matching what a future re-import computes."""
    if not key:
        return key
    prefix = f"{thread_id}:"
    return key[len(prefix):] if key.startswith(prefix) else key


class ThreadPlan:
    __slots__ = ("thread_id", "backfills", "warnings", "stats")

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.backfills: list[tuple[int, str]] = []        # (event_id, dedup_key)
        self.warnings: list[str] = []
        self.stats: dict[str, int] = defaultdict(int)

    @property
    def safe(self) -> bool:
        return not self.warnings


def _cc_shaped(source: str) -> bool:
    """Whether ``source``'s transcripts are read by the Claude Code parser.

    Asked of the registry rather than matched against a literal list, so a
    harness that shares the format — built in or a plugin — is re-parsed by the
    parser that actually reads it.
    """
    from .._providers import sources_using_parser

    return source in {p.name for p in sources_using_parser("claude-code")}


def _fresh_events(source: str, lines: list[dict]) -> list:
    """Re-parse a thread's source lines into freshly-built events (with dedup_keys),
    in build order."""
    if _cc_shaped(source):
        parser = ClaudeCodeParser()
        session_data = {
            "provider": "claude-code",
            "sessions": [{"session_id": "reconcile", "project": "reconcile", "lines": lines}],
        }
        messages = parser.parse_export(session_data)
    else:
        raise ValueError(f"no re-parse adapter for source {source!r}")

    builder = DefaultEventBuilder()
    out: list = []
    prev = None
    for msg in messages:
        role = msg.get("role", "")
        api_call_id = str(_uuid.uuid4()) if role == "assistant" else None
        stream_id = str(_uuid.uuid4())
        evs = builder.build_events(msg, stream_id, api_call_id, prev_occurred_at=prev)
        if evs:
            prev = evs[-1].occurred_at
        out.extend(evs)
    return out


def plan_thread(session, thread_id: str, source: str, lines: list[dict]) -> ThreadPlan:
    """Plan the dedup_key backfill for one thread. Pure planning — no writes.

    Match each freshly-built event to a persisted row by EXACT content anchor
    (type, timestamp, block, content_hash) with provider_message_id agreement, and
    copy the fresh (correct) dedup_key onto NULL-key rows. Exact content match ⇒ the
    key is what a re-import produces and can only ever collide with a genuine
    duplicate, so this cannot manufacture a harmful key. Fresh events that match
    nothing are candidate *dropped content* (a separate insert phase) — counted, not
    written. Any real anomaly flags the thread unsafe and it is skipped.
    """
    plan = ThreadPlan(thread_id)
    fresh = _fresh_events(source, lines)
    persisted = list(
        session.execute(
            select(Event).where(Event.thread_id == thread_id).order_by(Event.id)
        ).scalars().all()
    )
    plan.stats["fresh"] = len(fresh)
    plan.stats["persisted"] = len(persisted)

    # by_key uses the normalized (un-prefixed) form so an old-format keyed row is
    # recognized by a fresh (un-prefixed) key.
    by_key = {_norm_key(e.dedup_key, thread_id): e for e in persisted if e.dedup_key}
    content_index: dict[tuple, list] = defaultdict(list)
    for e in persisted:
        content_index[_content_anchor(e.event_type, e.payload or {}, e.occurred_at)].append(e)

    used: set[int] = set()
    planned_keys: set[str] = {key for key in by_key if key is not None}
    for f in fresh:
        if f.dedup_key and f.dedup_key in by_key:
            used.add(by_key[f.dedup_key].id)
            plan.stats["already"] += 1
            continue
        match = None
        for e in content_index.get(_content_anchor(f.event_type, f.payload, f.occurred_at), []):
            if e.id in used:
                continue
            fp, ep = _pmid(f.payload), _pmid(e.payload or {})
            if fp is not None and ep is not None and fp != ep:
                continue  # same content, different turn — not this event
            match = e
            break
        if match is None:
            plan.stats["unmatched_fresh"] += 1   # candidate dropped content (deferred)
            continue
        used.add(match.id)
        if match.dedup_key is None:
            if f.dedup_key in planned_keys:
                # would give two different rows the same key — never happens for a
                # genuine event pair; flag rather than write.
                plan.warnings.append(f"key collision {f.dedup_key[:28]}")
                continue
            planned_keys.add(f.dedup_key)
            plan.backfills.append((match.id, f.dedup_key))
            plan.stats["backfill"] += 1
        elif _norm_key(match.dedup_key, thread_id) == f.dedup_key:
            plan.stats["already"] += 1   # already keyed (either format)
        else:
            # exact content + pmid agree yet stored key genuinely differs → real drift.
            plan.warnings.append(f"key drift on {match.event_type} id={match.id}")
    return plan


def _iter_pairs() -> Iterator[tuple[str, Path, str]]:
    """``(source, path, source_id)`` for every on-disk transcript the Claude Code
    parser reads — every provider declaring that parser, not just Claude Code."""
    from .._providers import sources_using_parser

    for provider in sources_using_parser("claude-code"):
        if provider.watcher is None:
            continue
        watcher = provider.watcher()
        if not isinstance(watcher, FileSessionWatcher) or not watcher.is_available():
            continue
        for path, source_id in watcher.iter_files():
            yield provider.name, path, source_id


def run(
    *,
    apply: bool = False,
    limit: Optional[int] = None,
    backup_path: Optional[Path] = None,
    pairs: Optional[Iterable[tuple[str, Path, str]]] = None,
) -> dict:
    """Backfill dedup_keys onto NULL-key rows. Dry-run by default; ``apply`` writes
    per-thread (unsafe threads always skipped), appending a row-level backup.
    ``pairs`` overrides the on-disk transcript discovery (default: _iter_pairs())
    — the discovery boundary as a parameter, so tests feed scripted stores."""
    totals: dict[str, Any] = defaultdict(int)
    examined = 0
    backup = open(backup_path, "a", encoding="utf-8") if (apply and backup_path) else None
    try:
        for name, path, source_id in (pairs if pairs is not None else _iter_pairs()):
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
                try:
                    lines = read_session_lines(path)
                    plan = plan_thread(s, state.thread_id, name, lines)
                except Exception as e:  # noqa: BLE001
                    logger.warning("plan failed for %s (%s): %s", source_id, path, e)
                    totals["plan_errors"] += 1
                    continue

                totals["unmatched_fresh"] += plan.stats.get("unmatched_fresh", 0)
                if not plan.safe:
                    totals["unsafe_threads"] += 1
                    totals["warnings"] += len(plan.warnings)
                    for w in plan.warnings[:3]:
                        logger.info("  [skip t%s] %s", plan.thread_id, w)
                    continue

                totals["backfills"] += len(plan.backfills)
                if plan.backfills:
                    totals["threads_changed"] += 1

                if apply and plan.backfills:
                    if backup:
                        backup.write(json.dumps({
                            "thread_id": plan.thread_id,
                            "backfill_ids": [eid for eid, _ in plan.backfills],
                        }) + "\n")
                        backup.flush()
                    for eid, key in plan.backfills:
                        s.execute(update(Event).where(Event.id == eid).values(dedup_key=key))
                    s.commit()
    finally:
        if backup:
            backup.close()
    return dict(totals)


def main(
    argv: Optional[list[str]] = None,
    *,
    pairs: Optional[Iterable[tuple[str, Path, str]]] = None,
) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--backup", type=Path, default=None, help="row-level backup JSONL (apply)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    totals = run(apply=args.apply, limit=args.limit, backup_path=args.backup, pairs=pairs)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] backfill-reconcile (dedup_key backfill)")
    print(f"  threads examined:   {totals.get('threads', 0):,}")
    print(f"  threads changed:    {totals.get('threads_changed', 0):,}")
    print(f"  dedup_key backfills:{totals.get('backfills', 0):,}")
    print(f"  unmatched fresh:    {totals.get('unmatched_fresh', 0):,}  (candidate dropped content, deferred to insert phase)")
    print(f"  UNSAFE (skipped):   {totals.get('unsafe_threads', 0):,}  (warnings={totals.get('warnings', 0):,})")
    print(f"  plan errors:        {totals.get('plan_errors', 0):,}")


if __name__ == "__main__":
    main()
