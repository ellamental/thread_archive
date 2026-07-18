"""Attribute already-imported Codex turns to the model that served them.

Codex names its model per *turn* — ``turn_context.model``, plus a
``thread_settings_applied`` line on a mid-session switch — not in ``session_meta``.
The importer read only ``session_meta.model``, so every Codex turn imported before
that fix carries the placeholder ``model: "codex"`` on its ``api_request_started``
and ``api_request_completed`` events: the one field that answers "which model wrote
this?" was a constant across the whole provider.

The model is read back from two sources, in order of durability:

1. **The archive itself.** The importer preserves ``turn_context`` /
   ``thread_settings_applied`` lines verbatim as ``content_block`` events (Archivist,
   not Filter), so most threads carry their own answer and need no outside file. Each
   preserved block is reconstructed into the line it came from and put through the
   importer's own :func:`~thread_archive._importers.codex.codex_line_model`.
2. **The on-disk rollout**, for turns the archive can't answer — threads imported
   before the preservation existed dropped those lines, so the only surviving copy is
   Codex's own session file. Its model changes are read with the same rule and matched
   to turns by the clock (see :func:`rollout_timeline`), and where both sources can
   answer they must agree, or the turn is left alone — a conflict is a bug in one of
   the records, not a tiebreak.

Either way the rule is the importer's, so the backfill cannot drift from what a fresh
import now says: a turn takes the model named inside it, else the last one named
before it, and a mid-session switch takes effect from its own turn onward.

Why rewriting in place is safe here — even though rewriting payloads generally isn't:

- **Only the placeholder is touched.** An event whose model is already a real name
  is never rewritten, so a re-run is idempotent and a hand-corrected row is left alone.
- **The key follows the content.** ``model`` is a dedup-content key, so a rewritten
  payload no longer hashes to its own ``dedup_key``. Each patched event's key is
  re-hashed onto its new payload (anchor and block position preserved), which is what
  a fresh import of the same session would compute — so a future re-import dedups
  against these rows instead of doubling them, and the store's key-hash invariant
  (``_store_rows_failing_key_hash``) still holds afterward.
- **Store and truth move together**, under the reindex lock held exclusive, so no
  writer commits or appends mid-repair. The truth file is patched line-by-line —
  every other line is copied through byte-for-byte, torn lines and unmapped fields
  included, so nothing is lost the way a full re-emit could lose it — then atomically
  replaced. A live appender's cached handle detects the swap by inode and reopens.

FTS is untouched by construction: ``model`` is not indexed content.

Dry-run by default (reports what it *would* rewrite). Pass ``--apply`` to write, with
``--backup`` for a row-level record of every id, old/new model, and old/new key.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from .._importers._read import read_session_lines
from .._importers.codex import codex_line_model
from .._store import Event, Thread, get_session
from .._thread_import.event_builder import compute_content_hash
from .._truth.jsonl_log import (
    _hold_reindex_lock,
    _json_default,
    _shard_depth,
    _thread_file,
    log_dir,
)
from .._watcher.sources import codex_watcher

logger = logging.getLogger(__name__)

PLACEHOLDER = "codex"
# The two event types the builder stamps with the message's model.
MODEL_EVENTS = ("api_request_started", "api_request_completed")
_HASH_TAIL = re.compile(r"[0-9a-f]{16}")


def _block_model(payload: dict) -> Optional[str]:
    """The model named by a preserved codex ``content_block`` event, or None.

    The block holds the provider line verbatim under ``data.raw`` with its line type
    beside it, so the line is reconstructed and put through the importer's rule — one
    definition of "which model does this line name", used by both paths.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    raw = data.get("raw")
    if not isinstance(raw, dict):
        return None
    return codex_line_model({"type": data.get("codex_line_type"), "payload": raw})


def rollout_timeline(path: Path) -> list[tuple[datetime, str]]:
    """The rollout's model changes as ``(when, model)``, in order.

    Every line that names a model (by the importer's rule) is a change point. A turn is
    matched to this timeline by *time*, not by message id: a thread imported before the
    importer preserved unmodeled lines drew its turn boundaries around a different set
    of lines, so the message ids it recorded need not be the ones a replay computes —
    the clock is the one thing both records share.
    """
    out: list[tuple[datetime, str]] = []
    for line in read_session_lines(path):
        named = codex_line_model(line)
        when = _as_utc(line.get("timestamp"))
        if named and when is not None:
            out.append((when, named))
    out.sort(key=lambda point: point[0])
    return out


def _as_utc(when: Any) -> Optional[datetime]:
    """A rollout timestamp or a stored ``occurred_at`` as an aware UTC datetime."""
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(when, datetime):
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def _model_at(timeline: list[tuple[datetime, str]], end: Optional[datetime]) -> Optional[str]:
    """The model a turn ending at ``end`` ran on: the last change point at or before it.

    A turn opens before it names its model (codex emits ``task_started`` first), so the
    turn is matched on where it *ends* — which puts a change point inside the turn on
    that turn, exactly as the importer's in-flight correction does. A turn that ends
    before any change point at all is served by the first model the session names."""
    if not timeline:
        return None
    if end is None:
        return None
    chosen = None
    for when, model in timeline:
        if when <= end:
            chosen = model
        else:
            break
    return chosen or timeline[0][1]


def plan_thread(
    session, thread_id: int, timeline: Optional[list[tuple[datetime, str]]] = None,
) -> tuple[dict[int, str], int, int]:
    """``({event id: model}, placeholder_events, conflicts)`` for one thread.

    Walks the thread's events in order, tracking the model the same way the importer
    walks the line stream: a turn (one ``api_call_id``) is served by the last model
    named *within* it, else by the model in effect entering it. ``timeline`` — the
    rollout's model changes — answers the turns the archive can't, matched by when the
    turn ended; a turn the two sources name differently is counted as a conflict and
    left untouched. ``placeholder_events`` is how many placeholder events the thread
    holds; what it exceeds ``len(updates)`` by stays unresolved, not silently "done".
    """
    rows = session.execute(
        select(Event.id, Event.event_type, Event.api_call_id, Event.payload, Event.occurred_at)
        .where(Event.thread_id == thread_id)
        .order_by(Event.id)
    ).all()

    # Per turn, in first-event order: events to stamp, the model it names, when it ended.
    turns: dict[str, dict[str, Any]] = {}
    for eid, etype, call_id, payload, occurred_at in rows:
        if call_id is None or not isinstance(payload, dict):
            continue
        turn = turns.setdefault(call_id, {"stamp": [], "named": None, "end": None})
        if etype in MODEL_EVENTS:
            if payload.get("model") == PLACEHOLDER:
                turn["stamp"].append(eid)
            when = _as_utc(occurred_at)
            if when is not None and (turn["end"] is None or when > turn["end"]):
                turn["end"] = when
        elif etype == "content_block":
            named = _block_model(payload)
            if named:
                turn["named"] = named  # last one inside the turn wins

    updates: dict[int, str] = {}
    placeholders = 0
    conflicts = 0
    model = PLACEHOLDER
    for turn in turns.values():
        placeholders += len(turn["stamp"])
        archived = turn["named"]
        rolled = _model_at(timeline or [], turn["end"])
        if archived and rolled and archived != rolled:
            # Two records of the same turn disagreeing is a bug in one of them, and
            # nothing here can tell which. Leave the turn as it is, loudly.
            logger.warning(
                "thread %s: turn ending %s named %r in the archive but %r in its rollout",
                thread_id, turn["end"], archived, rolled,
            )
            conflicts += 1
            continue
        model = archived or rolled or model
        if model != PLACEHOLDER:
            for eid in turn["stamp"]:
                updates[eid] = model
    return updates, placeholders, conflicts


def rekey(key: str, payload: dict) -> str:
    """Re-hash a ``dedup_key`` onto a rewritten payload, keeping its identity.

    The key is ``{anchor}:{event_type}:{block}:{content_hash}``; the anchor is the
    provider message id, or — for an event that has none — ``c=<content_hash>``, which
    moves with the content too. Split from the right: only the anchor can contain a
    colon (codex's is a timestamp). A key with no hash tail carries no content claim
    to keep honest, so it is left alone.
    """
    parts = key.rsplit(":", 3)
    if len(parts) != 4 or not _HASH_TAIL.fullmatch(parts[3]):
        return key
    anchor, event_type, block, _old = parts
    content_hash = compute_content_hash(payload)
    if anchor.startswith("c="):
        anchor = f"c={content_hash}"
    return f"{anchor}:{event_type}:{block}:{content_hash}"


def patch_truth(thread_id: int, patched: dict[int, dict]) -> int:
    """Rewrite a thread's truth file so the given events carry their new payload+key.

    Every line the repair doesn't own is copied through verbatim; the file is fsynced
    and atomically replaced.
    """
    d = log_dir()
    path = _thread_file(d, thread_id, _shard_depth(d))
    if not path.exists():
        logger.warning("thread %s: no truth file at %s", thread_id, path)
        return 0

    tmp = path.with_name(path.name + ".codex-model-tmp")
    written = 0
    with path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as out:
        for line in src:
            rec = None
            if line.strip():
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = None  # torn/garbage line — pass it through untouched
            if (
                isinstance(rec, dict)
                and rec.get("type") == "event"
                and rec.get("id") in patched
                and isinstance(rec.get("payload"), dict)
            ):
                new = patched[rec["id"]]
                rec["payload"]["model"] = new["model"]
                rec["dedup_key"] = new["dedup_key"]
                out.write(json.dumps(rec, default=_json_default, ensure_ascii=False) + "\n")
                written += 1
                continue
            out.write(line)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return written


def _codex_threads(session, limit: Optional[int]) -> list[tuple[int, Optional[str]]]:
    """Every codex thread as ``(id, source_id)``, oldest first. Which of them still hold
    placeholder turns is decided by :func:`plan_thread` reading the payloads — not by a
    JSON-path predicate in SQL, which the store's dialect needn't support."""
    q = select(Thread.id, Thread.source_id).where(Thread.source == "codex").order_by(Thread.id)
    if limit is not None:
        q = q.limit(limit)
    return [(tid, sid) for tid, sid in session.execute(q).all()]


def rollout_index() -> dict[str, Path]:
    """``{source_id: rollout path}`` for every codex session still on disk, resolved by
    the watcher that named those source ids in the first place."""
    watcher = codex_watcher()
    if not watcher.is_available():
        return {}
    return {source_id: path for path, source_id in watcher.iter_files()}


def run(
    *, apply: bool = False, limit: Optional[int] = None, backup_path: Optional[Path] = None,
) -> dict:
    """Repair every codex thread holding placeholder-stamped turns. Returns a summary."""
    totals: dict[str, Any] = defaultdict(int)
    totals["by_model"] = defaultdict(int)
    backup = open(backup_path, "a", encoding="utf-8") if (apply and backup_path) else None

    # Exclusive for the whole run: writers hold the same lock shared around their
    # truth appends + commits, so nothing lands between a store patch and its truth
    # patch. The run is seconds; a blocked watcher pass simply retries.
    try:
        rollouts = rollout_index()
        with _hold_reindex_lock():
            with get_session() as s:
                threads = _codex_threads(s, limit)
            totals["threads_seen"] = len(threads)

            for tid, source_id in threads:
                with get_session() as s:
                    path = rollouts.get(source_id or "")
                    timeline: list[tuple[datetime, str]] = []
                    if path is not None:
                        try:
                            timeline = rollout_timeline(path)
                        except Exception as e:  # noqa: BLE001 — a pruned/torn rollout is not fatal
                            logger.warning("thread %s: rollout %s unreadable: %s", tid, path, e)
                            totals["rollout_errors"] += 1
                    updates, placeholders, conflicts = plan_thread(s, tid, timeline)
                    totals["conflicts"] += conflicts
                    if not placeholders:
                        continue
                    totals["threads_with_placeholder"] += 1
                    totals["events_unresolved"] += placeholders - len(updates)
                    if not updates:
                        totals["threads_unresolved"] += 1
                        continue
                    totals["threads_repaired"] += 1
                    totals["events"] += len(updates)
                    for model in updates.values():
                        totals["by_model"][model] += 1
                    if not apply:
                        continue

                    rows = s.execute(
                        select(Event).where(Event.id.in_(list(updates)))
                    ).scalars().all()
                    patched: dict[int, dict] = {}
                    for ev in rows:
                        payload = dict(ev.payload)
                        old_model, old_key = payload.get("model"), ev.dedup_key
                        payload["model"] = updates[ev.id]
                        ev.payload = payload
                        if ev.dedup_key:
                            ev.dedup_key = rekey(ev.dedup_key, payload)
                        patched[ev.id] = {"model": ev.payload["model"], "dedup_key": ev.dedup_key}
                        if backup:
                            backup.write(json.dumps({
                                "thread_id": tid, "event_id": ev.id,
                                "old_model": old_model, "new_model": ev.payload["model"],
                                "old_dedup_key": old_key, "new_dedup_key": ev.dedup_key,
                            }) + "\n")
                    try:
                        s.commit()
                    except Exception as e:  # noqa: BLE001 — one bad thread must not abort the run
                        logger.warning("store patch failed for thread %s: %s", tid, e)
                        totals["write_errors"] += 1
                        s.rollback()
                        continue
                    if backup:
                        backup.flush()
                    totals["truth_lines"] += patch_truth(tid, patched)
    finally:
        if backup:
            backup.close()

    totals["by_model"] = dict(totals["by_model"])
    return dict(totals)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap threads examined")
    ap.add_argument("--backup", type=Path, default=None, help="row-level backup JSONL (apply)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    totals = run(apply=args.apply, limit=args.limit, backup_path=args.backup)
    print(f"[{'APPLIED' if args.apply else 'DRY-RUN'}] backfill-codex-model")
    print(f"  codex threads seen:       {totals.get('threads_seen', 0)}")
    print(f"  threads with placeholder: {totals.get('threads_with_placeholder', 0)}")
    print(f"  threads repaired:         {totals.get('threads_repaired', 0)}")
    print(f"  threads unresolved:       {totals.get('threads_unresolved', 0)}")
    print(f"  events rewritten:         {totals.get('events', 0)}")
    print(f"  events left unresolved:   {totals.get('events_unresolved', 0)}")
    print(f"  truth lines rewritten:    {totals.get('truth_lines', 0)}")
    for model, n in sorted(totals.get("by_model", {}).items(), key=lambda kv: -kv[1]):
        print(f"    {model:<24} {n}")
    if totals.get("conflicts"):
        print(f"  turns skipped (archive/rollout disagree): {totals['conflicts']}")
    if totals.get("rollout_errors") or totals.get("write_errors"):
        print(f"  rollout errors: {totals.get('rollout_errors', 0)}"
              f"  write errors: {totals.get('write_errors', 0)}")


if __name__ == "__main__":
    main()
