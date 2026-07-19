"""The export-annotations backfill: threads imported before the export-parser fixes
gain the new fields — annotations, branch, tool pairing ids, folded
source_metadata — and nothing else moves.

The pre-fix state is simulated honestly: the fixtures are imported with the
CURRENT importer, then the stored rows are stripped back to what the old parsers
produced (annotations/branch deleted, tool ids nulled with ``unpaired: true``,
api-summary tool ids blanked, dedup keys recomputed without the ``tool=``
segment), so the backfill runs against exactly the shape the live archive holds.
"""

from __future__ import annotations

import json
import runpy
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from thread_archive._importers.exports import (
    fold_conversation_metadata,
    import_chatgpt_export,
    import_claude_ai_export,
)
from thread_archive._scripts import backfill_export_annotations as mod
from thread_archive._scripts.backfill_reconcile import _block
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.event_builder import compute_content_hash
from thread_archive._thread_import.parsers.claude import ClaudeParser
from thread_archive._truth.jsonl_log import _hash_key_check, _shard_depth, _thread_file, log_dir

# ── fixtures (minimal shapes cribbed from tests/test_exports_dropped_fields.py) ──

_CLAUDE_CONV = {
    "uuid": "conv-bf",
    "name": "Backfill Me",
    "summary": "what this chat was about",
    "created_at": "2026-01-01T10:00:00Z",
    "updated_at": "2026-01-01T10:01:00Z",
    "account": {"uuid": "acct-1"},
    "chat_messages": [
        {
            "uuid": "m1",
            "sender": "human",
            "text": "question",
            "content": [{"type": "text", "text": "question"}],
            "parent_message_uuid": "root-0000",
            "created_at": "2026-01-01T10:00:00Z",
        },
        {
            "uuid": "m2",
            "sender": "assistant",
            "text": "",
            "parent_message_uuid": "m1",
            "created_at": "2026-01-01T10:00:10Z",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "pondering",
                    "summaries": [{"summary": "pondered briefly"}],
                    "signature": "sig",
                },
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "web_search",
                    "input": {"query": "q"},
                    "integration_name": "Web Search",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "name": "web_search",
                    "content": [{"type": "knowledge", "title": "T", "text": "body"}],
                    "structured_content": {"hits": 1},
                    "integration_name": "Web Search",
                },
                {
                    "type": "text",
                    "text": "answer with citation",
                    "citations": [
                        {
                            "uuid": "cit-1",
                            "start_index": 0,
                            "end_index": 6,
                            "details": {"type": "web_search_citation", "url": "https://x"},
                        }
                    ],
                },
                {
                    "type": "flag",
                    "flag": "self_harm_risk",
                    "helpline": {"name": "988 Lifeline", "phone_number": "988"},
                },
            ],
        },
    ],
}

_CHATGPT_CONV = {
    "id": "gconv-bf",
    "title": "GPT Backfill",
    "create_time": 1767261600.0,
    "update_time": 1767261610.0,
    "current_node": "a1",
    "gizmo_id": "g-abc",
    "default_model_slug": "gpt-5-2",
    "is_starred": True,
    "is_archived": False,
    "moderation_results": [],
    "mapping": {
        "root": {"id": "root", "parent": None, "children": ["u1"], "message": None},
        "u1": {"id": "u1", "parent": "root", "children": ["a1"], "message": {
            "id": "u1", "author": {"role": "user"}, "create_time": 1767261600.0,
            "content": {"content_type": "text", "parts": ["cite something"]},
            "status": "finished_successfully", "metadata": {},
        }},
        "a1": {"id": "a1", "parent": "u1", "children": [], "message": {
            "id": "a1", "author": {"role": "assistant"}, "create_time": 1767261605.0,
            "content": {"content_type": "text", "parts": ["cited answer"]},
            "status": "finished_successfully",
            "metadata": {
                "model_slug": "gpt-5-2",
                "citations": [{
                    "start_ix": 0, "end_ix": 5, "citation_format_type": "tether_og",
                    "metadata": {"type": "webpage", "title": "A Page", "url": "https://cited.example"},
                }],
                "canvas": {"textdoc_id": "doc-1", "version": 2, "title": "Doc"},
            },
        }},
    },
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _claude_bundle(root, convs=None, *, name="claude_export"):
    d = root / name
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps(convs or [_CLAUDE_CONV]), encoding="utf-8")
    (d / "users.json").write_text("[]", encoding="utf-8")
    return d


def _conv_with_twin_user_turn(uuid: str) -> dict:
    """``_CLAUDE_CONV`` with its opening turn repeated verbatim under ``uuid`` — two
    fresh events sharing one content anchor (and one dedup_key when the uuid is
    reused, since the key anchors on the provider message id)."""
    conv = json.loads(json.dumps(_CLAUDE_CONV))
    twin = json.loads(json.dumps(conv["chat_messages"][0]))
    twin["uuid"] = uuid
    conv["chat_messages"].insert(1, twin)
    return conv


def _conv_with_repeated_tool_block(*, start_timestamp: str | None = None) -> dict:
    """``_CLAUDE_CONV`` with its tool_use block repeated verbatim — same tool id, same
    input, so both fresh events carry one dedup_key. ``start_timestamp`` puts the
    repeat on its own clock, which is what splits the two across pairing passes."""
    conv = json.loads(json.dumps(_CLAUDE_CONV))
    blocks = conv["chat_messages"][1]["content"]
    twin = json.loads(json.dumps(blocks[1]))
    if start_timestamp:
        twin["start_timestamp"] = start_timestamp
    blocks.insert(2, twin)
    return conv


def _chatgpt_bundle(root):
    d = root / "chatgpt_export"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([_CHATGPT_CONV]), encoding="utf-8")
    (d / "user.json").write_text(json.dumps({"id": "u"}), encoding="utf-8")
    return d


def _thread_id(source: str) -> int:
    with get_session() as s:
        return s.execute(select(Thread).where(Thread.source == source)).scalar_one().id


def _events(tid: int) -> list:
    with get_session() as s:
        return list(
            s.execute(select(Event).where(Event.thread_id == tid).order_by(Event.id)).scalars()
        )


def _one(events, event_type):
    matched = [e for e in events if e.event_type == event_type]
    assert len(matched) == 1, f"expected one {event_type}, got {len(matched)}"
    return matched[0]


def _degrade(tid: int, *, strip_branch: bool = True) -> None:
    """Strip stored rows back to the pre-fix parser output: no annotations, no
    branch (claude), tool ids nulled + ``unpaired``, api-summary tool ids blank,
    dedup keys recomputed as the old builder would have (no ``tool=`` segment)."""
    with get_session() as s:
        rows = s.execute(
            select(Event).where(Event.thread_id == tid).order_by(Event.id)
        ).scalars().all()
        for ev in rows:
            p = json.loads(json.dumps(ev.payload))
            p.pop("annotations", None)
            if strip_branch:
                p.pop("branch", None)
            if ev.event_type in mod.TOOL_EVENT_TYPES and p.get("tool_call_id"):
                p["tool_call_id"] = None
                p["unpaired"] = True
            if ev.event_type == "api_request_completed":
                for b in p.get("content_blocks") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        b["id"] = ""
            if ev.dedup_key:
                anchor = ev.dedup_key.rsplit(":", 3)[0]
                ev.dedup_key = f"{anchor}:{ev.event_type}:{_block(p)}:{compute_content_hash(p)}"
            ev.payload = p
        s.commit()


def _set_meta(tid: int, meta: dict) -> None:
    with get_session() as s:
        s.get(Thread, tid).source_metadata = meta
        s.commit()


def _set_payload(tid: int, event_type: str, fn) -> int:
    """Rewrite the payload of the thread's single event of ``event_type`` through
    ``fn``, leaving its dedup_key where it is. Returns the event id."""
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == event_type
        )).scalar_one()
        ev.payload = fn(json.loads(json.dumps(ev.payload)))
        s.commit()
        return ev.id


def _copy_event(event_id: int, **overrides) -> int:
    """Insert a second row carrying an existing event's content, keyless (the shape a
    thread holds when the same block landed twice). Returns the new row's id."""
    with get_session() as s:
        src = s.get(Event, event_id)
        row = Event(
            thread_id=src.thread_id, stream_id=src.stream_id, api_call_id=src.api_call_id,
            event_type=src.event_type, payload=json.loads(json.dumps(src.payload)),
            occurred_at=src.occurred_at, dedup_key=None,
        )
        for field, value in overrides.items():
            setattr(row, field, value)
        s.add(row)
        s.commit()
        return row.id


def _unkey(tid: int, event_type: str, *, drift_seconds: int = 0) -> int:
    """Drop a stored row's dedup_key (the pre-dedup-key-era shape), optionally moving
    it off its parsed timestamp too. Returns the event id."""
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == event_type
        )).scalar_one()
        ev.dedup_key = None
        if drift_seconds:
            ev.occurred_at = ev.occurred_at + timedelta(seconds=drift_seconds)
        s.commit()
        return ev.id


def _delete_event(tid: int, event_type: str, block_type: str | None = None) -> None:
    with get_session() as s:
        rows = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == event_type
        )).scalars().all()
        if block_type is not None:
            rows = [e for e in rows if (e.payload or {}).get("block_type") == block_type]
        s.delete(rows[0])
        s.commit()


def _folded_meta(conv=None) -> dict:
    """The conversation-level metadata the importer folds onto a thread at creation."""
    single = {
        "conversations": [conv or _CLAUDE_CONV], "memories": [], "projects": [], "users": [],
    }
    return fold_conversation_metadata({}, ClaudeParser().parse_export(single))


def _seed_claude(archive_home, conv=None):
    init_db()
    bundle = _claude_bundle(archive_home, [conv or _CLAUDE_CONV])
    assert import_claude_ai_export(bundle).imported == 1
    tid = _thread_id("claude")
    _degrade(tid)
    _set_meta(tid, {"provider": "claude", "surface": "web", "summary": "OLD SUMMARY"})
    return bundle, tid


def _truth_latest(tid: int) -> dict:
    """Latest truth record per event id (last-wins, as reindex loads them)."""
    path = _thread_file(log_dir(), tid, _shard_depth(log_dir()))
    latest: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "event":
            latest[rec["id"]] = rec
    return latest


# ── claude.ai ────────────────────────────────────────────────────────────────


def test_degraded_state_is_the_pre_fix_shape(archive_home) -> None:
    _, tid = _seed_claude(archive_home)
    events = _events(tid)
    use = _one(events, "tool_use_complete")
    assert use.payload["tool_call_id"] is None and use.payload["unpaired"] is True
    assert "tool=" not in use.dedup_key and "blk=" in use.dedup_key
    assert "annotations" not in use.payload and "branch" not in use.payload
    completed = _one(events, "api_request_completed")
    assert all(b["id"] == "" for b in completed.payload["content_blocks"] if b.get("type") == "tool_use")


def test_dry_run_plans_everything_and_writes_nothing(archive_home) -> None:
    bundle, tid = _seed_claude(archive_home)
    before = [(e.id, json.dumps(e.payload, sort_keys=True), e.dedup_key) for e in _events(tid)]

    totals = mod.run(bundles=[bundle], apply=False)
    assert totals["threads_examined"] == 1
    assert totals["pairs"] == 2  # tool_use_complete + tool_execution_completed
    assert totals["amend_events"] >= 3  # branch on user/start/stream, annotations on thinking/text
    assert totals["amend_annotations"] >= 2
    assert totals["amend_branch"] >= 3
    assert totals["api_summary_stale"] == 1
    assert totals["meta_threads"] == 1  # account_uuid missing
    assert totals.get("pairing_hash_mismatch", 0) == 0
    assert totals.get("pairing_key_collision", 0) == 0

    after = [(e.id, json.dumps(e.payload, sort_keys=True), e.dedup_key) for e in _events(tid)]
    assert before == after, "dry run must not write"
    with get_session() as s:
        assert s.get(Thread, tid).source_metadata == {
            "provider": "claude", "surface": "web", "summary": "OLD SUMMARY",
        }


def test_apply_pairs_amends_folds_and_is_idempotent(archive_home, tmp_path) -> None:
    bundle, tid = _seed_claude(archive_home)
    backup = tmp_path / "backup.jsonl"

    totals = mod.run(bundles=[bundle], apply=True, backup_path=backup)
    assert totals["paired"] == 2
    assert totals.get("apply_errors", 0) == 0
    assert totals.get("pairing_apply_skipped", 0) == 0

    events = _events(tid)
    use = _one(events, "tool_use_complete")
    res = _one(events, "tool_execution_completed")
    for ev in (use, res):
        assert ev.payload["tool_call_id"] == "toolu_1"
        assert ev.payload["unpaired"] is False
        assert "tool=toolu_1" in ev.dedup_key
        # The content-hash guard held: the moved key still hashes to its payload.
        assert _hash_key_check(ev.payload, ev.dedup_key) is True
    assert use.payload["annotations"]["integration_name"] == "Web Search"
    assert use.payload["branch"]["parent_id"] == "m1"
    assert res.payload["annotations"]["structured_content"] == {"hits": 1}

    think = _one(events, "thinking_complete")
    assert think.payload["annotations"]["summaries"] == [{"summary": "pondered briefly"}]
    text = next(e for e in events if e.event_type == "text_complete"
                and e.payload["text"] == "answer with citation")
    assert text.payload["annotations"]["citations"][0]["details"]["url"] == "https://x"
    user = _one(events, "user_message_sent")
    assert user.payload["branch"]["parent_id"] == "root-0000"

    # The stale api summary is left alone (content-identity), just counted.
    completed = _one(events, "api_request_completed")
    assert all(b["id"] == "" for b in completed.payload["content_blocks"] if b.get("type") == "tool_use")
    assert totals["api_summary_stale"] == 1

    # source_metadata: missing keys folded, existing values untouched.
    with get_session() as s:
        meta = s.get(Thread, tid).source_metadata
    assert meta["summary"] == "OLD SUMMARY"
    assert meta["account_uuid"] == "acct-1"

    # Truth superseded: latest line per paired event carries new payload AND key.
    truth = _truth_latest(tid)
    for ev in (use, res):
        assert truth[ev.id]["dedup_key"] == ev.dedup_key
        assert truth[ev.id]["payload"]["tool_call_id"] == "toolu_1"

    # Backup names every change with old/new keys for the pairings.
    lines = [json.loads(ln) for ln in backup.read_text().splitlines()]
    pairings = [ln for ln in lines if ln["kind"] == "pairing"]
    assert len(pairings) == 2
    assert all(p["old_dedup_key"] != p["new_dedup_key"] for p in pairings)
    assert any(ln["kind"] == "source_metadata" for ln in lines)
    assert any(ln["kind"] == "amend" for ln in lines)

    # Idempotent: a second run plans nothing.
    again = mod.run(bundles=[bundle], apply=True)
    assert again.get("pairs", 0) == 0
    assert again.get("amend_events", 0) == 0
    assert again.get("meta_threads", 0) == 0
    assert again.get("threads_with_changes", 0) == 0


def test_pairing_hash_mismatch_is_skipped(archive_home) -> None:
    bundle, tid = _seed_claude(archive_home)
    # Drift the stored tool_use's *content* — no longer the same event.
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "tool_use_complete"
        )).scalar_one()
        p = json.loads(json.dumps(ev.payload))
        p["input"] = {"query": "DIFFERENT"}
        anchor = ev.dedup_key.rsplit(":", 3)[0]
        ev.dedup_key = f"{anchor}:{ev.event_type}:{_block(p)}:{compute_content_hash(p)}"
        ev.payload = p
        s.commit()

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["pairing_hash_mismatch"] == 1
    assert totals["pairs"] == 1  # the untouched tool_execution_completed still pairs
    use = _one(_events(tid), "tool_use_complete")
    assert use.payload["tool_call_id"] is None, "guard failure must leave the row alone"


def test_pairing_key_collision_is_refused(archive_home) -> None:
    bundle, tid = _seed_claude(archive_home)
    # An existing row already holding the key the pairing would move onto.
    with get_session() as s:
        res = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "tool_execution_completed"
        )).scalar_one()
        res_id, old_key = res.id, res.dedup_key
        anchor = old_key.rsplit(":", 3)[0]
        colliding = f"{anchor}:tool_execution_completed:tool=toolu_1:{compute_content_hash(res.payload)}"
        s.add(Event(
            thread_id=tid, stream_id="dummy", event_type="content_block",
            payload={"block_type": "x", "data": {}},
            occurred_at=datetime.now(timezone.utc), dedup_key=colliding,
        ))
        s.commit()

    # Plan level: the fresh twin dedups (by key) against the row that already holds
    # its key, so no pairing is even planned for the stored unpaired row — it is
    # left untouched rather than forced onto a taken key.
    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["pairs"] == 1  # tool_use_complete still pairs
    res = _one(_events(tid), "tool_execution_completed")
    assert res.payload["tool_call_id"] is None

    # Apply level: a pairing whose new_key collides with an existing row in the
    # thread is skipped by the write-time re-check, never forced.
    plan = mod.ThreadPlan(tid, "conv-bf")
    plan.pairings.append({
        "event_id": res_id,
        "patch": {"tool_call_id": "toolu_1", "unpaired": False},
        "old_key": old_key, "new_key": colliding,
    })
    out = mod._apply_pairings(plan, None)
    assert out == {"paired": 0, "skipped": 1}
    res = _one(_events(tid), "tool_execution_completed")
    assert res.payload["tool_call_id"] is None and res.dedup_key == old_key


def test_new_content_is_counted_never_inserted(archive_home) -> None:
    bundle, tid = _seed_claude(archive_home)
    with get_session() as s:
        flag = next(
            e for e in s.execute(select(Event).where(
                Event.thread_id == tid, Event.event_type == "content_block"
            )).scalars() if e.payload.get("block_type") == "flag"
        )
        s.delete(flag)
        s.commit()
    n_before = len(_events(tid))

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["new_content_not_inserted"] >= 1
    assert totals["new_content_by_type"].get("content_block[flag]") == 1
    assert len(_events(tid)) == n_before, "this script never inserts"


# ── ChatGPT ──────────────────────────────────────────────────────────────────


def test_chatgpt_annotations_and_meta_backfill(archive_home, tmp_path) -> None:
    init_db()
    bundle = _chatgpt_bundle(archive_home)
    assert import_chatgpt_export(bundle).imported == 1
    tid = _thread_id("chatgpt")
    branch_before = {
        e.id: (e.payload or {}).get("branch") for e in _events(tid)
    }
    assert any(branch_before.values()), "chatgpt import persists branch already"
    _degrade(tid, strip_branch=False)
    _set_meta(tid, {"provider": "chatgpt", "surface": "web"})

    totals = mod.run(bundles=[bundle], apply=True, backup_path=tmp_path / "b.jsonl")
    assert totals["threads_examined"] == 1
    assert totals["amend_annotations"] >= 1
    assert totals.get("amend_branch", 0) == 0, "present branch is skipped, not rewritten"
    assert totals.get("pairs", 0) == 0

    events = _events(tid)
    completed = _one(events, "api_request_completed")
    ann = completed.payload["annotations"]
    assert ann["citations"][0]["metadata"]["url"] == "https://cited.example"
    assert ann["canvas"]["textdoc_id"] == "doc-1"
    assert {e.id: (e.payload or {}).get("branch") for e in events} == branch_before

    with get_session() as s:
        meta = s.get(Thread, tid).source_metadata
    assert meta["gizmo_id"] == "g-abc"
    assert meta["default_model_slug"] == "gpt-5-2"
    assert meta["is_starred"] is True
    assert meta["is_archived"] is False

    again = mod.run(bundles=[bundle], apply=True)
    assert again.get("threads_with_changes", 0) == 0


def test_annotation_merge_is_add_missing_only(archive_home) -> None:
    """An existing annotations subkey is never overwritten — only absent subkeys land."""
    bundle, tid = _seed_claude(archive_home)
    with get_session() as s:
        think = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "thinking_complete"
        )).scalar_one()
        think.payload = {**think.payload, "annotations": {"summaries": ["HAND-EDITED"]}}
        s.commit()

    mod.run(bundles=[bundle], apply=True)
    think = _one(_events(tid), "thinking_complete")
    assert think.payload["annotations"]["summaries"] == ["HAND-EDITED"]


def test_timestamp_drift_is_rescued_when_content_is_unique(archive_home) -> None:
    """A stored event at a moved occurred_at (older imports resolved timestamps
    differently) still receives its amendment — and an unpaired tool twin still
    pairs — via the ts-free unique-content rescue."""
    bundle, tid = _seed_claude(archive_home)
    with get_session() as s:
        rows = s.execute(select(Event).where(
            Event.thread_id == tid,
            Event.event_type.in_(("thinking_complete", "tool_use_complete")),
        )).scalars().all()
        for ev in rows:
            ev.occurred_at = ev.occurred_at + timedelta(seconds=7)
            # The live rows this rescues are pre-dedup-key-era: NULL keys, so
            # neither the key match nor the anchored match can see them.
            ev.dedup_key = None
        s.commit()

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["matched_content_tsfree"] >= 1
    assert totals["pairs_tsfree"] == 1
    assert totals["pairs"] == 2  # ts-anchored result pair + ts-free use pair
    events = _events(tid)
    think = _one(events, "thinking_complete")
    assert think.payload["annotations"]["summaries"] == [{"summary": "pondered briefly"}]
    use = _one(events, "tool_use_complete")
    assert use.payload["tool_call_id"] == "toolu_1"
    assert "tool=toolu_1" in use.dedup_key
    assert _hash_key_check(use.payload, use.dedup_key) is True


def test_thread_missing_from_store_is_counted(archive_home) -> None:
    init_db()
    bundle = _claude_bundle(archive_home)  # never imported
    totals = mod.run(bundles=[bundle], apply=False)
    assert totals["threads_missing"] == 1
    assert totals.get("threads_examined", 0) == 0


# ── matching guards ──────────────────────────────────────────────────────────


def test_keyless_row_matches_on_its_content_anchor(archive_home) -> None:
    """A pre-dedup-key-era row still sitting at its parsed timestamp is found by the
    exact content anchor — no ts-free rescue needed — and amends like a keyed twin."""
    bundle, tid = _seed_claude(archive_home)
    _unkey(tid, "thinking_complete")

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals.get("matched_content_tsfree", 0) == 0
    think = _one(_events(tid), "thinking_complete")
    assert think.payload["annotations"]["summaries"] == [{"summary": "pondered briefly"}]


def test_twin_turns_take_one_stored_row_each(archive_home) -> None:
    """Two turns sharing a content anchor consume distinct rows: the second fresh twin
    steps over the row the first took and lands on its own provider id."""
    bundle, tid = _seed_claude(archive_home, conv=_conv_with_twin_user_turn("m1b"))
    with get_session() as s:
        for ev in s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "user_message_sent"
        )).scalars():
            ev.dedup_key = None
        s.commit()

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals.get("dup_fresh", 0) == 0
    users = [e for e in _events(tid) if e.event_type == "user_message_sent"]
    assert len(users) == 2
    assert all(e.payload["branch"]["parent_id"] == "root-0000" for e in users)
    assert {e.payload["provider_data"]["provider_message_id"] for e in users} == {"m1", "m1b"}


def test_turn_repeated_under_one_id_is_counted_as_duplicate_fresh(archive_home) -> None:
    """An export repeating a message under the same uuid builds two fresh events with
    one key; the stored side holds a single row, so the second is counted rather than
    matched onto a row already taken."""
    bundle, tid = _seed_claude(archive_home, conv=_conv_with_twin_user_turn("m1"))

    totals = mod.run(bundles=[bundle], apply=False)
    assert totals["dup_fresh"] == 1
    assert len([e for e in _events(tid) if e.event_type == "user_message_sent"]) == 1


def test_provider_id_disagreement_blocks_every_match(archive_home) -> None:
    """Identical content under a different provider message id is a different turn.
    Neither the anchored match nor the ts-free rescue may claim it."""
    bundle, tid = _seed_claude(archive_home)
    _unkey(tid, "user_message_sent")
    _set_payload(tid, "user_message_sent", lambda p: {
        **p, "provider_data": {**p["provider_data"], "provider_message_id": "someone-else"}
    })

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["new_content_by_type"].get("user_message_sent") == 1
    user = _one(_events(tid), "user_message_sent")
    assert "branch" not in user.payload, "a mismatched turn must not be amended"


def test_redacted_payload_refuses_the_amendment(archive_home) -> None:
    """Redaction leaves the row's dedup_key in place, so a fresh twin still finds it —
    and the amend gate refuses to merge fields into a sealed marker."""
    bundle, tid = _seed_claude(archive_home)
    marker = {"_redacted": {"key_id": "k-1", "at": "2026-01-02T00:00:00Z"}}
    eid = _set_payload(tid, "thinking_complete", lambda p: dict(marker))

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["amend_refused"] == 1
    with get_session() as s:
        assert s.get(Event, eid).payload == marker


def test_annotations_merge_fills_only_the_absent_subkeys(archive_home) -> None:
    """A partially-annotated payload keeps every subkey it has and gains the rest."""
    bundle, tid = _seed_claude(archive_home)
    _set_payload(tid, "tool_execution_completed",
                 lambda p: {**p, "annotations": {"integration_name": "HAND-SET"}})

    mod.run(bundles=[bundle], apply=True)
    res = _one(_events(tid), "tool_execution_completed")
    assert res.payload["annotations"] == {
        "integration_name": "HAND-SET", "structured_content": {"hits": 1},
    }


def test_foreign_annotations_value_is_left_alone(archive_home) -> None:
    """An ``annotations`` that isn't a dict is hand-touched or foreign — there is no
    safe way to fold into it, so it stands as it is while the rest of the patch lands."""
    bundle, tid = _seed_claude(archive_home)
    eid = _set_payload(tid, "thinking_complete", lambda p: {**p, "annotations": "hand-written"})

    mod.run(bundles=[bundle], apply=True)
    with get_session() as s:
        payload = s.get(Event, eid).payload
    assert payload["annotations"] == "hand-written"
    assert payload["branch"]["parent_id"] == "m1"


def test_api_summary_with_no_stored_twin_is_new_content(archive_home) -> None:
    """The stale-summary exemption needs an unused stored twin at the same timestamp.
    With none, the fresh summary is ordinary new content — counted, not inserted."""
    bundle, tid = _seed_claude(archive_home)
    _delete_event(tid, "api_request_completed")
    n_before = len(_events(tid))

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals.get("api_summary_stale", 0) == 0
    assert totals["new_content_by_type"].get("api_request_completed") == 1
    assert len(_events(tid)) == n_before


# ── pairing guards ───────────────────────────────────────────────────────────


def test_pairing_is_dropped_when_the_group_counts_disagree(archive_home) -> None:
    """One stored row against two fresh twins: the order tie-break can't be trusted,
    so the whole group is dropped rather than guessed at."""
    bundle, tid = _seed_claude(archive_home, conv=_conv_with_repeated_tool_block())

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["pairing_ambiguous"] == 2
    assert totals["pairs"] == 1  # the tool_result twin is unambiguous and still pairs
    use = _one(_events(tid), "tool_use_complete")
    assert use.payload["tool_call_id"] is None and "blk=" in use.dedup_key


def test_pairing_refuses_a_key_it_has_already_planned(archive_home) -> None:
    """Two stored twins, two fresh twins carrying one dedup_key: the first pairs, the
    second would move onto the key this plan just claimed — dropped."""
    bundle, tid = _seed_claude(archive_home, conv=_conv_with_repeated_tool_block())
    _copy_event(_one(_events(tid), "tool_use_complete").id)

    totals = mod.run(bundles=[bundle], apply=True)
    # Dropped twice over: by the anchored pass, then by the rescue offered the same twin.
    assert totals["pairing_key_collision"] == 2
    assert totals["pairs"] == 2  # one tool_use twin + the tool_result
    uses = [e for e in _events(tid) if e.event_type == "tool_use_complete"]
    assert [e.payload["tool_call_id"] for e in uses] == ["toolu_1", None]


def test_pairing_refuses_a_payload_the_amend_gate_rejects(archive_home) -> None:
    """``check_patch`` gates the pairing patch as it gates an amendment: a payload it
    refuses keeps its blank tool id and its old key."""
    bundle, tid = _seed_claude(archive_home)
    _set_payload(tid, "tool_use_complete", lambda p: {**p, "_redacted": {"key_id": "k-1"}})

    totals = mod.run(bundles=[bundle], apply=True)
    # Refused twice over: by the anchored pass, then by the rescue offered the same twin.
    assert totals["pairing_refused"] == 2
    assert totals["pairs"] == 1  # the tool_result still pairs
    use = _one(_events(tid), "tool_use_complete")
    assert use.payload["tool_call_id"] is None and "blk=" in use.dedup_key


def test_ts_free_pairing_refuses_a_payload_the_amend_gate_rejects(archive_home) -> None:
    """The same gate on the rescue path: a drifted row the amend gate refuses is left
    unpaired instead of being rescued onto a new key."""
    bundle, tid = _seed_claude(archive_home)
    _set_payload(tid, "tool_use_complete", lambda p: {**p, "_redacted": {"key_id": "k-1"}})
    _unkey(tid, "tool_use_complete", drift_seconds=7)

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["pairing_refused"] == 1
    assert totals.get("pairs_tsfree", 0) == 0
    use = _one(_events(tid), "tool_use_complete")
    assert use.payload["tool_call_id"] is None and use.dedup_key is None


def test_ts_free_pairing_refuses_a_malformed_fresh_key(archive_home, monkeypatch) -> None:
    """The tail check is the explicit half of the content guard: no 16-hex hash on the
    fresh key, no pair — the key move would otherwise land an unhashable key."""
    bundle, tid = _seed_claude(archive_home)
    _unkey(tid, "tool_use_complete", drift_seconds=7)

    class _KeylessToolBuilder(DefaultEventBuilder):
        """A builder whose tool events come out unkeyed — the malformed-key shape."""

        def build_events(self, *args, **kwargs):
            events = super().build_events(*args, **kwargs)
            for e in events:
                if e.event_type == "tool_use_complete":
                    e.dedup_key = None
            return events

    monkeypatch.setattr(mod, "DefaultEventBuilder", _KeylessToolBuilder)
    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["pairing_hash_mismatch"] == 1
    assert totals.get("pairs_tsfree", 0) == 0
    use = _one(_events(tid), "tool_use_complete")
    assert use.payload["tool_call_id"] is None


def test_ts_free_pairing_refuses_a_key_the_plan_already_claimed(archive_home) -> None:
    """The repeated tool block reaches the rescue after the anchored pass claimed its
    key: two rows would end up sharing one identity, so the rescue drops it."""
    bundle, tid = _seed_claude(
        archive_home, conv=_conv_with_repeated_tool_block(start_timestamp="2026-01-01T10:00:12Z")
    )
    use = _one(_events(tid), "tool_use_complete")
    _copy_event(use.id, occurred_at=use.occurred_at + timedelta(seconds=30))

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["pairing_key_collision"] == 1
    assert totals["pairs"] == 2  # the anchored tool_use twin + the tool_result
    assert totals.get("pairs_tsfree", 0) == 0
    uses = [e for e in _events(tid) if e.event_type == "tool_use_complete"]
    assert [e.payload["tool_call_id"] for e in uses] == ["toolu_1", None]


# ── apply seams ──────────────────────────────────────────────────────────────


def test_apply_pairings_skips_rows_that_moved_since_planning(archive_home) -> None:
    """Every pair is re-validated against the live row: an event that vanished and one
    already carrying a tool id are skipped, never forced."""
    bundle, tid = _seed_claude(archive_home)
    mod.run(bundles=[bundle], apply=True)
    use = _one(_events(tid), "tool_use_complete")
    patch = {"tool_call_id": "toolu_1", "unpaired": False}

    plan = mod.ThreadPlan(tid, "conv-bf")
    plan.pairings = [
        {"event_id": 10_000_000, "patch": patch, "old_key": "gone", "new_key": use.dedup_key},
        {"event_id": use.id, "patch": patch, "old_key": use.dedup_key, "new_key": use.dedup_key},
    ]
    assert mod._apply_pairings(plan, None) == {"paired": 0, "skipped": 2}


def test_apply_meta_skips_a_thread_that_vanished(archive_home) -> None:
    _seed_claude(archive_home)
    plan = mod.ThreadPlan(10_000_000, "conv-gone")
    plan.meta_patch = {"account_uuid": "acct-1"}
    assert mod._apply_meta(plan, None) is False


def test_meta_fold_lost_to_a_concurrent_writer_is_not_counted(archive_home, monkeypatch) -> None:
    """The fold is re-checked against the live row: keys another writer folded between
    plan and apply are left alone, and the run claims no fold it did not make."""
    bundle, tid = _seed_claude(archive_home)
    amend = mod.amend_event_payloads

    def _folding_first(patches, *, reason=None):
        # Another writer lands the whole fold while this run is still amending events.
        _set_meta(tid, {**_folded_meta(), "summary": "OLD SUMMARY"})
        return amend(patches, reason=reason)

    monkeypatch.setattr(mod, "amend_event_payloads", _folding_first)
    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["meta_threads"] == 1
    assert totals.get("meta_applied", 0) == 0
    with get_session() as s:
        assert s.get(Thread, tid).source_metadata["account_uuid"] == "acct-1"


def test_metadata_only_thread_skips_the_event_writers(archive_home) -> None:
    """A thread whose events are already enriched but whose fold was lost applies the
    metadata alone."""
    bundle, tid = _seed_claude(archive_home)
    mod.run(bundles=[bundle], apply=True)
    _set_meta(tid, {"provider": "claude"})

    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["amend_events"] == 0
    assert totals.get("pairs", 0) == 0
    assert totals["meta_applied"] == 1
    assert totals["threads_changed"] == 1


# ── driver + CLI ─────────────────────────────────────────────────────────────


def test_unreadable_bundle_is_counted_and_the_run_continues(archive_home) -> None:
    """One bad bundle must not stop the run — the readable one behind it is examined."""
    bundle, tid = _seed_claude(archive_home)
    junk = archive_home / "not_an_export"
    junk.mkdir()
    (junk / "readme.txt").write_text("nothing here", encoding="utf-8")

    totals = mod.run(bundles=[junk, bundle], apply=False)
    assert totals["bundles"] == 2
    assert totals["bundle_errors"] == 1
    assert totals["threads_examined"] == 1
    with pytest.raises(ValueError, match="export bundle"):
        list(mod._iter_conversations(junk))


def test_conversation_without_an_id_is_skipped(archive_home) -> None:
    """An export row with no uuid cannot be matched to a thread — counted, not guessed."""
    init_db()
    anonymous = json.loads(json.dumps(_CLAUDE_CONV))
    anonymous.pop("uuid")
    anonymous["updated_at"] = "2025-01-01T00:00:00Z"  # sorts behind the identified one
    bundle = _claude_bundle(archive_home, [_CLAUDE_CONV, anonymous])

    totals = mod.run(bundles=[bundle], apply=False)
    assert totals["conversations_no_id"] == 1
    assert totals["conversations"] == 1
    assert totals["threads_missing"] == 1


def test_limit_caps_the_threads_examined(archive_home) -> None:
    init_db()
    second = json.loads(json.dumps(_CLAUDE_CONV))
    second["uuid"] = "conv-bf-2"
    second["updated_at"] = "2025-01-01T00:00:00Z"
    bundle = _claude_bundle(archive_home, [_CLAUDE_CONV, second])
    assert import_claude_ai_export(bundle).imported == 2

    totals = mod.run(bundles=[bundle], apply=False, limit=1)
    assert totals["threads_examined"] == 1
    assert totals["conversations"] == 1


def test_plan_errors_are_counted_and_leave_the_thread_alone(archive_home, monkeypatch) -> None:
    bundle, tid = _seed_claude(archive_home)
    before = [(e.id, json.dumps(e.payload, sort_keys=True), e.dedup_key) for e in _events(tid)]

    def _boom(session, thread, messages):
        raise RuntimeError("planning blew up")

    monkeypatch.setattr(mod, "plan_thread", _boom)
    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["plan_errors"] == 1
    assert totals["threads_examined"] == 1
    after = [(e.id, json.dumps(e.payload, sort_keys=True), e.dedup_key) for e in _events(tid)]
    assert before == after


def test_apply_errors_are_counted(archive_home, monkeypatch) -> None:
    bundle, tid = _seed_claude(archive_home)

    def _boom(patches, *, reason=None):
        raise RuntimeError("write blew up")

    monkeypatch.setattr(mod, "amend_event_payloads", _boom)
    totals = mod.run(bundles=[bundle], apply=True)
    assert totals["apply_errors"] == 1
    assert totals.get("threads_changed", 0) == 0
    use = _one(_events(tid), "tool_use_complete")
    assert use.payload["tool_call_id"] is None, "the failed thread is left as it was"


def test_main_dry_run_then_apply(archive_home, tmp_path, capsys) -> None:
    bundle, tid = _seed_claude(archive_home)
    _delete_event(tid, "content_block", "flag")  # gives the report a new-content breakdown

    mod.main(["--bundle", str(bundle)])
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "content_block[flag]" in out
    assert "APPLIED:" not in out
    assert _one(_events(tid), "tool_use_complete").payload["tool_call_id"] is None

    backup = tmp_path / "cli-backup.jsonl"
    mod.main(["--bundle", str(bundle), "--apply", "--limit", "5", "--backup", str(backup), "-v"])
    out = capsys.readouterr().out
    assert "[APPLIED]" in out
    assert "APPLIED: amended=" in out and "paired=2" in out
    assert _one(_events(tid), "tool_use_complete").payload["tool_call_id"] == "toolu_1"
    assert [json.loads(ln) for ln in backup.read_text().splitlines()]


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_module_is_runnable_as_a_script(archive_home, monkeypatch, capsys) -> None:
    """``python -m thread_archive._scripts.backfill_export_annotations`` runs the CLI."""
    bundle, _ = _seed_claude(archive_home)
    monkeypatch.setattr(
        sys, "argv", ["backfill-export-annotations", "--bundle", str(bundle)]
    )
    runpy.run_module(
        "thread_archive._scripts.backfill_export_annotations", run_name="__main__"
    )
    assert "[DRY-RUN]" in capsys.readouterr().out
