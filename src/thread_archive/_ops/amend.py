"""Event-sourced payload amendment: change an event's stored values without
mutating history.

The truth log is append-only, but append-only does not mean the *values* are
frozen — an edit is a new record, not a rewrite. The truth layer already has
the reconcile half built in: loads are last-wins by event id (the reindex
loader's ``INSERT OR REPLACE``, in file line order), ``scan_truth_counts``
reports superseded same-id lines as expected history rather than drift, and
redaction rewrites every line carrying a target id precisely because superseded
copies persist. :func:`amend_event_payloads` is the sanctioned *writer* for
that mechanism: it appends a full superseding event record (same id, same
``dedup_key``, same timestamps, amended payload) to the thread's truth file
through the ordinary staged-drain seam — so the truth line is fsynced before
the index COMMIT it belongs to, and the drain's crash framing (intent journal,
rollback) covers it like any import. The prior line remains in the file as the
event's recorded history.

Two invariants bound what an amendment may touch:

* **Content is identity.** ``dedup_key`` embeds a hash of the payload's
  content fields (``_DEDUP_CONTENT_KEYS`` — text, content blocks, model, …);
  re-import idempotency and the verify/rebuild hash gates all key off it. An
  amendment therefore may only merge keys *outside* that set (cost, token
  counts, provider annotations, …) — the merged payload re-hashes to the same
  key, so every gate stays green and a re-import of the source still matches.
  Editing content fields is refused: that is redaction's jurisdiction (replace
  everywhere + keyed bundle), not a merge.
* **Redacted payloads are sealed.** A marker payload carries no fields to
  merge into; amending one would bloat a record whose content is deliberately
  elsewhere.

Provenance rides ``truth/amendments.jsonl`` (append-only, beside
``redactions.jsonl``): one record per amended event — the fields set, their
prior values, the reason, the timestamp. The truth line itself stays
schema-clean (``rebuild_truth_from_store`` refuses unmapped fields), so the
audit trail lives in the sidecar, and the before-values make any amendment
reversible by a compensating amendment.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

from .._store import Event, get_session
from .._thread_import.event_builder import _DEDUP_CONTENT_KEYS
from .._truth.jsonl_log import (
    _fsync_dir,
    _json_default,
    append_event_row,
    is_redacted_payload,
    log_dir,
    shared_ingest_lock,
)

logger = logging.getLogger(__name__)

AMENDMENTS_FILE = "amendments.jsonl"

# Payload keys an amendment may never touch: the content-hash material behind
# dedup_key (imported from the builder so the two sets cannot drift), plus the
# redaction marker envelope.
_PROTECTED_KEYS = frozenset(_DEDUP_CONTENT_KEYS) | {"_redacted"}
_METRIC_KEYS = frozenset(
    {
        "input_tokens",
        "input_tokens_includes_cache",
        "cache_read_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cost",
    }
)


def _amendments_path(d: Path) -> Path:
    return d / AMENDMENTS_FILE


def _append_amendment_records(d: Path, recs: list[dict]) -> None:
    p = _amendments_path(d)
    is_new = not p.exists()
    with open(p, "a", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False, default=_json_default))
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if is_new:
        _fsync_dir(d)


def load_amendments(d: Path | None = None) -> list[dict]:
    """Every record in the amendments log, in append order."""
    from .._truth.jsonl_log import _iter_jsonl

    return list(_iter_jsonl(_amendments_path(d or log_dir())))


def check_patch(payload: object, patch: dict) -> str | None:
    """Why ``patch`` may not be merged into ``payload`` — None when it may.

    The validation seam, exposed so planners (backfill dry-runs) can classify
    without writing."""
    if not isinstance(patch, dict) or not patch:
        return "empty patch"
    bad = sorted(set(patch) & _PROTECTED_KEYS)
    if bad:
        return (
            f"patch touches content-identity field(s) {bad} — content edits change "
            "the dedup_key hash and are redaction's jurisdiction, not amendment's"
        )
    if is_redacted_payload(payload):
        return "payload is redacted — unredact first if this event needs amending"
    if not isinstance(payload, dict):
        return "payload is not an object"
    return None


def amend_event_payloads(
    patches: Iterable[tuple[str, int, dict]], *, reason: str | None = None
) -> dict:
    """Merge ``patch`` into each ``(thread_id, event_id, patch)`` event's payload.

    Merge-only (no key removal): each patched key's new value replaces or adds
    to the payload; every other key is untouched. Keys whose stored value
    already equals the patch value are dropped from the patch; an event whose
    patch fully no-ops is skipped. Invalid targets (unknown event, thread
    mismatch, redacted payload, content-field patch) raise ``ValueError`` —
    the batch is validated per-thread before that thread commits, so a bad
    entry aborts its own thread's batch, never a previously committed one.

    Writes go through the ordinary session seam — superseding truth line staged
    and fsynced before the index COMMIT — one commit per thread, under the
    shared ingest lock (amendment is a writer like any import; reindex's
    exclusive lock excludes it). The audit record lands in
    ``truth/amendments.jsonl`` after the commit that made it true.

    Returns ``{"events_amended": n, "events_skipped": n, "threads": n}``.
    """
    by_thread: dict[str, list[tuple[int, dict]]] = {}
    for thread_id, event_id, patch in patches:
        by_thread.setdefault(str(thread_id), []).append((int(event_id), patch))

    d = log_dir()
    now = datetime.now(timezone.utc).isoformat()
    amended = skipped = 0
    metrics_dirty = False
    with shared_ingest_lock():
        for thread_id, entries in sorted(by_thread.items()):
            audit: list[dict] = []
            with get_session() as s:
                for event_id, patch in entries:
                    ev = s.get(Event, event_id)
                    if ev is None or str(ev.thread_id) != thread_id:
                        raise ValueError(f"event {event_id} is not in thread {thread_id}")
                    problem = check_patch(ev.payload, patch)
                    if problem == "empty patch":
                        skipped += 1
                        continue
                    if problem is not None:
                        raise ValueError(f"event {event_id}: {problem}")
                    old = ev.payload or {}
                    effective = {k: v for k, v in patch.items() if old.get(k) != v}
                    if not effective:
                        skipped += 1
                        continue
                    ev.payload = {**old, **effective}
                    metrics_dirty = metrics_dirty or bool(
                        set(effective) & _METRIC_KEYS
                    )
                    append_event_row(s, ev)  # the superseding truth line
                    audit.append({
                        "type": "amendment", "thread_id": thread_id, "event_id": event_id,
                        "fields": sorted(effective),
                        "before": {k: old.get(k) for k in effective},
                        "reason": reason, "at": now,
                    })
                    amended += 1
                s.commit()
            if audit:
                _append_amendment_records(d, audit)
        if metrics_dirty:
            # The incremental stats cursor only notices appended event ids; an
            # amendment changes a row behind that cursor. Drop the disposable
            # projection and rewind it so the next stats read rebuilds from the
            # amended event log instead of serving stale token or cost totals.
            with get_session() as s:
                s.execute(text("DELETE FROM thread_metrics"))
                s.execute(text("DELETE FROM request_cache_metrics"))
                s.execute(
                    text(
                        "INSERT OR IGNORE INTO metrics_cursor "
                        "(id, through_event_id, cache_requests_ready) "
                        "VALUES (1, 0, 0)"
                    )
                )
                s.execute(
                    text(
                        "UPDATE metrics_cursor SET through_event_id = 0, "
                        "cache_requests_ready = 0 "
                        "WHERE id = 1"
                    )
                )
                s.commit()
    logger.info(
        "amend: %d event(s) amended across %d thread(s) (%d no-op)",
        amended, len(by_thread), skipped,
    )
    return {"events_amended": amended, "events_skipped": skipped, "threads": len(by_thread)}
