"""Enrich already-imported claude.ai / ChatGPT export threads with the fields the
current export parsers now emit.

The export parsers gained fields that older imports never persisted: claude.ai
tool_use/tool_result pairing ids, block annotations (text citations, thinking
summaries, tool structured_content / integration metadata), claude.ai branch
metadata (``parent_message_uuid``), ChatGPT message annotations (citations,
content_references, canvas, assets), code-language annotations, safety ``flag``
blocks, newly-nonempty tether text, and conversation-level metadata folded into
the thread's ``source_metadata``. A thread imported before those fixes holds none
of it. This backfill re-parses each conversation from its export bundle with the
*current* parsers and moves what it can onto the stored rows, through the
sanctioned seams:

1. **Amend-missing** (``_ops.amend``): a fresh event matched to a stored row — by
   ``dedup_key``, by exact content anchor (type, timestamp, block, content hash)
   with provider_message_id agreement, or by the timestamp-free unique-content
   rescue (same type + content hash, exactly one candidate on each side; older
   imports resolved some timestamps differently, so an identical event can sit at
   a moved ``occurred_at``) — donates its missing
   ``annotations`` subkeys (deep-merge, add-missing-only — an existing subkey is
   never overwritten) and its ``branch`` dict (skipped entirely when the stored
   payload already has one). Both keys live outside ``_DEDUP_CONTENT_KEYS``, so the
   merged payload re-hashes to its own key; ``amend.check_patch`` is the gate.

2. **Tool-id pairing** (claude.ai): a stored ``tool_use_complete`` /
   ``tool_execution_completed`` / ``tool_execution_error`` with no ``tool_call_id``
   and ``unpaired: true`` whose fresh twin carries the id. The fresh key differs in
   its *block segment only* (``tool=<id>`` vs ``blk=<i>``/empty), so these are
   matched on (event_type, occurred_at, tool_name) + order among same-type-same-ts,
   guarded by the content hash: the fresh key's hash tail must equal
   ``compute_content_hash`` of the STORED payload, proving the content is
   identical. The payload patch (``tool_call_id``, ``unpaired: False``, plus any
   missing annotations/branch) and the ``dedup_key`` update land together through
   one superseding truth line (``append_event_row``), so store and truth move as
   one — the same property the codex-model backfill's docstring insists on for any
   key rewrite. A fresh key already present in the thread refuses the pair.
   The stored ``api_request_completed`` whose ``content_blocks`` still carry the
   old empty tool ids is left alone — ``content_blocks`` is content-identity
   material — and counted as ``api_summary_stale`` so the report stays honest.

3. **Thread source_metadata**: ``exports.fold_conversation_metadata`` runs only at
   thread creation, so existing threads lack the fold. The missing keys (and only
   the missing keys — an existing value, including ``models``, is never touched)
   are merged and the thread re-staged to truth via the importers'
   ``_restage_thread`` seam.

4. **New content** (flag blocks, newly-nonempty tether text — any fresh event that
   matches nothing stored): counted as ``new_content_not_inserted``, never
   inserted. Inserting belongs to a reconcile-style pass with its own safety
   argument, not this one.

Re-parsing goes through the same parser + builder walk ``exports.py`` /
``assemble_events`` use — but **without** ``log_parse_validation``: the drift
ledger is an import-time observability *write*, and the dry-run here must stay
read-only against the live store.

Ambiguity is skipped, never guessed: a pairing group whose stored and fresh
candidate counts disagree is dropped (``pairing_ambiguous``), a hash-guard failure
(``pairing_hash_mismatch``) or key collision (``pairing_key_collision``) drops the
pair, a conversation whose plan raises is counted (``plan_errors``) and left
alone. Everything is idempotent: a second run finds the annotations present, the
tools paired, the metadata folded, and plans nothing.

Dry-run by default. ``--apply`` writes, with ``--backup`` appending a row-level
JSONL (event ids, fields patched, old/new dedup_key) per change.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import uuid as _uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import select

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.event_builder import compute_content_hash
from thread_archive._thread_import.parsers.chatgpt import ChatGPTParser
from thread_archive._thread_import.parsers.claude import ClaudeParser

from .._importers._state import _restage_thread, get_thread_by_source
from .._importers.exports import (
    _chatgpt_sort_ts,
    _load_chatgpt_export,
    _load_claude_export,
    classify_export,
    fold_conversation_metadata,
)
from .._ops.amend import _append_amendment_records, amend_event_payloads, check_patch
from .._store import Event, Thread, get_session
from .._truth.jsonl_log import append_event_row, log_dir, shared_ingest_lock
from .backfill_reconcile import _block, _content_anchor, _norm_key, _pmid, _ts_key

logger = logging.getLogger(__name__)

REASON = "backfill_export_annotations"

# The stored event types a claude.ai tool block pairs onto.
TOOL_EVENT_TYPES = ("tool_use_complete", "tool_execution_completed", "tool_execution_error")

_HASH_TAIL = re.compile(r"^[0-9a-f]{16}$")


# ── fresh re-parse (no writes) ───────────────────────────────────────────────


def _fresh_events(messages: list) -> list:
    """Freshly-built events for one conversation, mirroring ``assemble_events``'s
    walk (stream per user turn, api_call_id per assistant turn, monotonic
    timestamp inheritance) without touching the store."""
    builder = DefaultEventBuilder()
    out: list = []
    prev = None
    stream: Optional[str] = None
    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            stream = str(_uuid.uuid4())
            evs = builder.build_events(msg, stream, prev_occurred_at=prev)
        elif role == "assistant":
            stream = stream or str(_uuid.uuid4())
            evs = builder.build_events(msg, stream, str(_uuid.uuid4()), prev_occurred_at=prev)
        else:
            evs = builder.build_events(msg, stream or str(_uuid.uuid4()), prev_occurred_at=prev)
        if evs:
            prev = evs[-1].occurred_at
        out.extend(evs)
    return out


def _iter_conversations(path: Path) -> Iterator[tuple[str, str, Callable[[], list]]]:
    """``(source, source_id, parse_thunk)`` per conversation in an export bundle,
    newest-first like the importer. The thunk parses via the current provider
    parser — validation logging deliberately skipped (it writes the drift ledger;
    this pass must be able to run read-only)."""
    kind = classify_export(path)
    if kind == "claude":
        bundle = _load_claude_export(path)
        parser = ClaudeParser()
        convs = [c for c in bundle["conversations"] if c.get("chat_messages")]
        convs.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "", reverse=True)
        for conv in convs:
            source_id = conv.get("uuid", "")
            single = {
                "conversations": [conv],
                "memories": bundle["memories"],
                "projects": bundle["projects"],
                "users": bundle["users"],
            }
            yield "claude", source_id, (lambda s=single: parser.parse_export(s))
    elif kind == "chatgpt":
        conversations = _load_chatgpt_export(path)
        parser = ChatGPTParser()
        convs = [c for c in conversations if isinstance(c, dict) and c.get("mapping")]
        convs.sort(key=_chatgpt_sort_ts, reverse=True)
        for conv in convs:
            source_id = conv.get("id") or conv.get("conversation_id") or ""
            yield "chatgpt", source_id, (lambda c=conv: parser.parse_export([c]))
    else:
        raise ValueError(f"not a claude.ai/ChatGPT export bundle (classified {kind!r}): {path}")


# ── planning ─────────────────────────────────────────────────────────────────


def _missing_field_patch(stored_payload: dict, fresh_payload: dict) -> dict:
    """The amendable patch a fresh twin donates to its stored row: missing
    ``annotations`` subkeys (add-only, existing subkeys never overwritten) and the
    ``branch`` dict when the stored payload has none at all."""
    patch: dict = {}
    fresh_ann = fresh_payload.get("annotations")
    if isinstance(fresh_ann, dict) and fresh_ann:
        stored_ann = stored_payload.get("annotations")
        if "annotations" not in stored_payload:
            patch["annotations"] = dict(fresh_ann)
        elif isinstance(stored_ann, dict):
            missing = {k: v for k, v in fresh_ann.items() if k not in stored_ann}
            if missing:
                patch["annotations"] = {**stored_ann, **missing}
        # present-but-not-a-dict: hand-touched or foreign — leave it alone.
    fresh_branch = fresh_payload.get("branch")
    if isinstance(fresh_branch, dict) and fresh_branch and "branch" not in stored_payload:
        patch["branch"] = dict(fresh_branch)
    return patch


class ThreadPlan:
    __slots__ = ("thread_id", "source_id", "amends", "pairings", "meta_patch", "stats")

    def __init__(self, thread_id: int, source_id: str) -> None:
        self.thread_id = thread_id
        self.source_id = source_id
        self.amends: list[tuple[int, dict]] = []           # (event_id, payload patch)
        self.pairings: list[dict] = []                     # see plan_thread
        self.meta_patch: dict = {}
        self.stats: dict[str, Any] = defaultdict(int)

    @property
    def changes(self) -> int:
        return len(self.amends) + len(self.pairings) + (1 if self.meta_patch else 0)


def _pair_group_key(event_type: str, occurred_at: Any, payload: dict) -> tuple:
    """The tool-pairing anchor: the struct anchor MINUS its block segment (the block
    is exactly what changed — ``tool=<id>`` vs the old ``blk=<i>``/empty) plus the
    tool name. Order among same-key events breaks the tie."""
    return (event_type, _ts_key(occurred_at), payload.get("tool_name"))


def plan_thread(session, thread: Thread, messages: list) -> ThreadPlan:
    """Plan every enrichment for one thread. Pure planning — no writes."""
    plan = ThreadPlan(thread.id, thread.source_id or "")
    fresh = _fresh_events(messages)
    persisted = list(
        session.execute(
            select(Event).where(Event.thread_id == thread.id).order_by(Event.id)
        ).scalars().all()
    )
    plan.stats["fresh"] = len(fresh)
    plan.stats["persisted"] = len(persisted)

    by_key: dict[str, Event] = {}
    content_index: dict[tuple, list[Event]] = defaultdict(list)
    unpaired_groups: dict[tuple, list[Event]] = defaultdict(list)
    struct_ts_index: dict[tuple, list[Event]] = defaultdict(list)
    all_keys: set[str] = set()
    for e in persisted:
        payload = e.payload if isinstance(e.payload, dict) else {}
        norm = _norm_key(e.dedup_key, thread.id)
        if norm:
            by_key[norm] = e
            all_keys.add(norm)
        content_index[_content_anchor(e.event_type, payload, e.occurred_at)].append(e)
        struct_ts_index[(e.event_type, _ts_key(e.occurred_at))].append(e)
        if (
            e.event_type in TOOL_EVENT_TYPES
            and not payload.get("tool_call_id")
            and payload.get("unpaired")
        ):
            unpaired_groups[_pair_group_key(e.event_type, e.occurred_at, payload)].append(e)

    used: set[int] = set()
    planned_keys: set[str] = set(all_keys)
    unmatched_fresh: list = []

    def _plan_amend(stored: Event, f) -> None:
        patch = _missing_field_patch(stored.payload or {}, f.payload)
        if not patch:
            plan.stats["already_complete"] += 1
            return
        problem = check_patch(stored.payload, patch)
        if problem is not None:
            logger.info("t%s e%s: amend refused: %s", thread.id, stored.id, problem)
            plan.stats["amend_refused"] += 1
            return
        plan.amends.append((stored.id, patch))
        for field in patch:
            plan.stats[f"amend_{field}"] += 1

    # Pass 1: identity matches (dedup_key, then exact content anchor + pmid guard).
    for f in fresh:
        stored = by_key.get(f.dedup_key or "")
        if stored is not None:
            if stored.id in used:
                plan.stats["dup_fresh"] += 1
                continue
            used.add(stored.id)
            _plan_amend(stored, f)
            continue
        match = None
        for e in content_index.get(_content_anchor(f.event_type, f.payload, f.occurred_at), []):
            if e.id in used:
                continue
            fp, ep = _pmid(f.payload), _pmid(e.payload or {})
            if fp is not None and ep is not None and fp != ep:
                continue
            match = e
            break
        if match is not None:
            used.add(match.id)
            _plan_amend(match, f)
            continue
        unmatched_fresh.append(f)

    # Pass 2: tool-id pairing over the leftovers (claude.ai — the fresh key moved
    # to a tool=<id> block segment, so identity matching can't see the twin).
    still_unmatched: list = []
    fresh_tool_groups: dict[tuple, list] = defaultdict(list)
    for f in unmatched_fresh:
        if (
            f.event_type in TOOL_EVENT_TYPES
            and f.payload.get("tool_call_id")
            and _pair_group_key(f.event_type, f.occurred_at, f.payload) in unpaired_groups
        ):
            fresh_tool_groups[_pair_group_key(f.event_type, f.occurred_at, f.payload)].append(f)
        else:
            still_unmatched.append(f)

    for group_key, fresh_list in fresh_tool_groups.items():
        stored_list = [e for e in unpaired_groups[group_key] if e.id not in used]
        if len(stored_list) != len(fresh_list):
            # Counts disagreeing means the order tie-break can't be trusted here.
            plan.stats["pairing_ambiguous"] += len(fresh_list)
            still_unmatched.extend(fresh_list)
            continue
        for stored, f in zip(stored_list, fresh_list):
            new_key = f.dedup_key or ""
            tail = new_key.rsplit(":", 1)[-1]
            stored_payload = stored.payload or {}
            if not _HASH_TAIL.match(tail) or compute_content_hash(stored_payload) != tail:
                # The guard: content identical or no pair. A mismatch means the
                # fresh twin's *content* differs from the stored row — not the
                # same event, whatever the clock says.
                plan.stats["pairing_hash_mismatch"] += 1
                still_unmatched.append(f)
                continue
            if new_key in planned_keys:
                plan.stats["pairing_key_collision"] += 1
                still_unmatched.append(f)
                continue
            patch = {
                "tool_call_id": f.payload.get("tool_call_id"),
                "unpaired": False,
                **_missing_field_patch(stored_payload, f.payload),
            }
            problem = check_patch(stored_payload, patch)
            if problem is not None:
                logger.info("t%s e%s: pairing refused: %s", thread.id, stored.id, problem)
                plan.stats["pairing_refused"] += 1
                still_unmatched.append(f)
                continue
            used.add(stored.id)
            planned_keys.add(new_key)
            plan.pairings.append({
                "event_id": stored.id,
                "patch": patch,
                "old_key": stored.dedup_key,
                "new_key": new_key,
            })
            plan.stats["pairs"] += 1

    # Pass 2b: timestamp-free unique-content rescue. Older imports resolved some
    # timestamps differently than the current parser does (block start_timestamp
    # refinement, inheritance changes), so an identical event can sit at a moved
    # occurred_at and defeat the anchored match. When a (event_type, content_hash)
    # pair is UNIQUE on both sides — exactly one leftover fresh, exactly one
    # unconsumed stored — the identification is unambiguous whatever the clock
    # says; anything non-unique stays unmatched rather than guessed. A rescued
    # stored row that is an unpaired tool twin takes the full pairing treatment
    # (id + key move, same guards); the rest amend.
    fresh_counts: dict[tuple, int] = defaultdict(int)
    for f in still_unmatched:
        fresh_counts[(f.event_type, compute_content_hash(f.payload))] += 1
    stored_groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in persisted:
        if e.id in used:
            continue
        stored_groups[(e.event_type, compute_content_hash(e.payload or {}))].append(e)
    remainder: list = []
    for f in still_unmatched:
        gk = (f.event_type, compute_content_hash(f.payload))
        candidates = [e for e in stored_groups.get(gk, []) if e.id not in used]
        if fresh_counts[gk] != 1 or len(candidates) != 1:
            remainder.append(f)
            continue
        stored = candidates[0]
        stored_payload = stored.payload or {}
        fp, ep = _pmid(f.payload), _pmid(stored_payload)
        if fp is not None and ep is not None and fp != ep:
            remainder.append(f)
            continue
        if (
            stored.event_type in TOOL_EVENT_TYPES
            and not stored_payload.get("tool_call_id")
            and stored_payload.get("unpaired")
            and f.payload.get("tool_call_id")
        ):
            new_key = f.dedup_key or ""
            tail = new_key.rsplit(":", 1)[-1]
            # gk equality already proves the content hashes agree; the tail check
            # keeps the guard explicit against a malformed key.
            if not _HASH_TAIL.match(tail) or compute_content_hash(stored_payload) != tail:
                plan.stats["pairing_hash_mismatch"] += 1
                remainder.append(f)
                continue
            if new_key in planned_keys:
                plan.stats["pairing_key_collision"] += 1
                remainder.append(f)
                continue
            patch = {
                "tool_call_id": f.payload.get("tool_call_id"),
                "unpaired": False,
                **_missing_field_patch(stored_payload, f.payload),
            }
            if check_patch(stored_payload, patch) is not None:
                plan.stats["pairing_refused"] += 1
                remainder.append(f)
                continue
            used.add(stored.id)
            planned_keys.add(new_key)
            plan.pairings.append({
                "event_id": stored.id, "patch": patch,
                "old_key": stored.dedup_key, "new_key": new_key,
            })
            plan.stats["pairs"] += 1
            plan.stats["pairs_tsfree"] += 1
            continue
        used.add(stored.id)
        plan.stats["matched_content_tsfree"] += 1
        _plan_amend(stored, f)
    still_unmatched = remainder

    # Pass 3: classify the remainder. A fresh api_request_completed whose stored
    # twin (same type+timestamp, unused) differs only because its content_blocks
    # still carry the old empty tool ids is content-identity — left, counted.
    for f in still_unmatched:
        if f.event_type == "api_request_completed":
            twin = next(
                (e for e in struct_ts_index.get((f.event_type, _ts_key(f.occurred_at)), [])
                 if e.id not in used),
                None,
            )
            if twin is not None:
                used.add(twin.id)
                plan.stats["api_summary_stale"] += 1
                continue
        plan.stats["new_content_not_inserted"] += 1
        kind = f.event_type
        if kind == "content_block" and isinstance(f.payload, dict):
            kind = f"content_block[{f.payload.get('block_type')}]"
        plan.stats.setdefault("new_content_by_type", defaultdict(int))[kind] += 1

    # Thread source_metadata: the fold the importer now applies at creation,
    # missing-keys-only against what the thread already carries.
    folded = fold_conversation_metadata({}, messages)
    current = thread.source_metadata or {}
    plan.meta_patch = {k: v for k, v in folded.items() if k not in current}
    return plan


# ── applying ─────────────────────────────────────────────────────────────────


def _apply_pairings(plan: ThreadPlan, backup) -> dict[str, int]:
    """Write the planned tool-id pairings: payload patch + dedup_key together on the
    store row and one superseding truth line, under the shared ingest lock — the
    amendment mechanism, extended with the key move (safe here because the content
    hash is unchanged, so the new key still hashes to its own payload). Each pair
    is re-validated against the live row before writing; a row that moved since
    planning is skipped, not forced."""
    out = {"paired": 0, "skipped": 0}
    audit: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    with shared_ingest_lock():
        with get_session() as s:
            for p in plan.pairings:
                ev = s.get(Event, p["event_id"])
                payload = ev.payload if ev is not None and isinstance(ev.payload, dict) else None
                new_key = p["new_key"]
                if (
                    ev is None
                    or int(ev.thread_id) != plan.thread_id
                    or payload is None
                    or payload.get("tool_call_id")
                    or compute_content_hash(payload) != new_key.rsplit(":", 1)[-1]
                    or check_patch(payload, p["patch"]) is not None
                ):
                    out["skipped"] += 1
                    continue
                collision = s.execute(
                    select(Event.id).where(
                        Event.thread_id == plan.thread_id,
                        Event.dedup_key == new_key,
                        Event.id != ev.id,
                    ).limit(1)
                ).first()
                if collision is not None:
                    out["skipped"] += 1
                    continue
                old_payload = payload
                ev.payload = {**old_payload, **p["patch"]}
                ev.dedup_key = new_key
                append_event_row(s, ev)  # superseding truth line: new payload AND new key
                audit.append({
                    "type": "amendment", "thread_id": plan.thread_id, "event_id": ev.id,
                    "fields": sorted(p["patch"]),
                    "before": {k: old_payload.get(k) for k in p["patch"]},
                    "old_dedup_key": p["old_key"], "new_dedup_key": new_key,
                    "reason": REASON, "at": now,
                })
                if backup:
                    backup.write(json.dumps({
                        "kind": "pairing", "thread_id": plan.thread_id, "event_id": ev.id,
                        "fields": sorted(p["patch"]),
                        "old_dedup_key": p["old_key"], "new_dedup_key": new_key,
                    }) + "\n")
                out["paired"] += 1
            s.commit()
        if audit:
            _append_amendment_records(log_dir(), audit)
        if backup:
            backup.flush()
    return out


def _apply_meta(plan: ThreadPlan, backup) -> bool:
    """Merge the planned missing source_metadata keys and re-stage the thread record
    to truth (the importers' metadata-update seam). Missing-only, re-checked
    against the live row."""
    with get_session() as s:
        thread = s.get(Thread, plan.thread_id)
        if thread is None:
            return False
        current = thread.source_metadata or {}
        missing = {k: v for k, v in plan.meta_patch.items() if k not in current}
        if not missing:
            return False
        thread.source_metadata = {**current, **missing}
        _restage_thread(s, thread)
        s.commit()
    if backup:
        backup.write(json.dumps({
            "kind": "source_metadata", "thread_id": plan.thread_id,
            "keys_added": sorted(missing),
        }) + "\n")
        backup.flush()
    return True


def _apply_amends(plan: ThreadPlan, backup) -> dict:
    result = amend_event_payloads(
        [(plan.thread_id, eid, patch) for eid, patch in plan.amends], reason=REASON
    )
    if backup:
        for eid, patch in plan.amends:
            backup.write(json.dumps({
                "kind": "amend", "thread_id": plan.thread_id, "event_id": eid,
                "fields": sorted(patch),
            }) + "\n")
        backup.flush()
    return result


# ── driver ───────────────────────────────────────────────────────────────────

_ROLLUP_KEYS = (
    "fresh", "persisted", "already_complete", "dup_fresh", "amend_refused",
    "amend_annotations", "amend_branch", "matched_content_tsfree",
    "pairs", "pairs_tsfree", "pairing_ambiguous",
    "pairing_hash_mismatch", "pairing_key_collision", "pairing_refused",
    "api_summary_stale", "new_content_not_inserted",
)


def run(
    *,
    bundles: list[Path],
    apply: bool = False,
    limit: Optional[int] = None,
    backup_path: Optional[Path] = None,
) -> dict:
    """Plan (and with ``apply`` write) the enrichment for every conversation in the
    given export bundles whose thread exists. Returns a totals summary."""
    totals: dict[str, Any] = defaultdict(int)
    totals["new_content_by_type"] = defaultdict(int)
    examined = 0
    backup = open(backup_path, "a", encoding="utf-8") if (apply and backup_path) else None
    try:
        for bundle in bundles:
            totals["bundles"] += 1
            try:
                conversations = _iter_conversations(Path(bundle))
            except Exception as e:  # noqa: BLE001 — one bad bundle must not stop the run
                logger.warning("bundle %s unreadable: %s", bundle, e)
                totals["bundle_errors"] += 1
                continue
            for source, source_id, parse in conversations:
                if limit is not None and examined >= limit:
                    break
                if not source_id:
                    totals["conversations_no_id"] += 1
                    continue
                totals["conversations"] += 1
                with get_session() as s:
                    thread = get_thread_by_source(s, source, source_id)
                    if thread is None:
                        totals["threads_missing"] += 1
                        continue
                    examined += 1
                    totals["threads_examined"] += 1
                    try:
                        plan = plan_thread(s, thread, parse())
                    except Exception as e:  # noqa: BLE001 — skip, never guess
                        logger.warning("plan failed for %s:%s: %s", source, source_id, e)
                        totals["plan_errors"] += 1
                        continue

                for k in _ROLLUP_KEYS:
                    totals[k] += plan.stats.get(k, 0)
                for kind, n in plan.stats.get("new_content_by_type", {}).items():
                    totals["new_content_by_type"][kind] += n
                totals["amend_events"] += len(plan.amends)
                if plan.meta_patch:
                    totals["meta_threads"] += 1
                    totals["meta_keys"] += len(plan.meta_patch)
                if plan.changes:
                    totals["threads_with_changes"] += 1
                if not apply or not plan.changes:
                    continue

                try:
                    if plan.amends:
                        amended = _apply_amends(plan, backup)
                        totals["amended"] += amended.get("events_amended", 0)
                    if plan.pairings:
                        paired = _apply_pairings(plan, backup)
                        totals["paired"] += paired["paired"]
                        totals["pairing_apply_skipped"] += paired["skipped"]
                    if plan.meta_patch and _apply_meta(plan, backup):
                        totals["meta_applied"] += 1
                    totals["threads_changed"] += 1
                except Exception as e:  # noqa: BLE001 — one bad thread must not abort the run
                    logger.warning("apply failed for thread %s: %s", plan.thread_id, e)
                    totals["apply_errors"] += 1
            else:
                continue
            break  # inner loop hit the limit
    finally:
        if backup:
            backup.close()
    totals["new_content_by_type"] = dict(totals["new_content_by_type"])
    return dict(totals)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--bundle", type=Path, action="append", required=True, dest="bundles",
        help="claude.ai or ChatGPT export zip/dir (repeatable)",
    )
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap threads examined")
    ap.add_argument("--backup", type=Path, default=None, help="row-level backup JSONL (apply)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    totals = run(
        bundles=args.bundles, apply=args.apply, limit=args.limit, backup_path=args.backup
    )
    print(f"[{'APPLIED' if args.apply else 'DRY-RUN'}] backfill-export-annotations")
    print(f"  bundles:                    {totals.get('bundles', 0)}  (errors: {totals.get('bundle_errors', 0)})")
    print(f"  conversations seen:         {totals.get('conversations', 0):,}")
    print(f"  threads examined:           {totals.get('threads_examined', 0):,}  (missing: {totals.get('threads_missing', 0):,})")
    print(f"  threads with changes:       {totals.get('threads_with_changes', 0):,}")
    print(f"  amend patches planned:      {totals.get('amend_events', 0):,}"
          f"  (annotations: {totals.get('amend_annotations', 0):,}, branch: {totals.get('amend_branch', 0):,},"
          f" refused: {totals.get('amend_refused', 0):,})")
    print(f"  ts-free unique rescues:     {totals.get('matched_content_tsfree', 0):,}")
    print(f"  tool pairs planned:         {totals.get('pairs', 0):,}  (ts-free: {totals.get('pairs_tsfree', 0):,})")
    print(f"    guard failures:           hash={totals.get('pairing_hash_mismatch', 0):,}"
          f" collision={totals.get('pairing_key_collision', 0):,}"
          f" ambiguous={totals.get('pairing_ambiguous', 0):,}"
          f" refused={totals.get('pairing_refused', 0):,}")
    print(f"  api summaries left stale:   {totals.get('api_summary_stale', 0):,}")
    print(f"  source_metadata folds:      {totals.get('meta_threads', 0):,} threads, {totals.get('meta_keys', 0):,} keys")
    print(f"  new content (not inserted): {totals.get('new_content_not_inserted', 0):,}")
    for kind, n in sorted(totals.get("new_content_by_type", {}).items(), key=lambda kv: -kv[1])[:12]:
        print(f"    {kind:<40} {n:,}")
    print(f"  plan errors:                {totals.get('plan_errors', 0):,}")
    if any(k in totals for k in ("amended", "paired", "meta_applied", "apply_errors", "pairing_apply_skipped")):
        print(f"  APPLIED: amended={totals.get('amended', 0):,} paired={totals.get('paired', 0):,}"
              f" meta={totals.get('meta_applied', 0):,}"
              f" apply-skipped={totals.get('pairing_apply_skipped', 0):,}"
              f" apply-errors={totals.get('apply_errors', 0):,}")


if __name__ == "__main__":
    main()
