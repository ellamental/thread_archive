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
from datetime import datetime, timezone

from sqlalchemy import select

from thread_archive._importers.exports import import_chatgpt_export, import_claude_ai_export
from thread_archive._scripts import backfill_export_annotations as mod
from thread_archive._scripts.backfill_reconcile import _block
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._thread_import.event_builder import compute_content_hash
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


def _claude_bundle(root):
    d = root / "claude_export"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([_CLAUDE_CONV]), encoding="utf-8")
    (d / "users.json").write_text("[]", encoding="utf-8")
    return d


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


def _seed_claude(archive_home):
    init_db()
    bundle = _claude_bundle(archive_home)
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
    from datetime import timedelta

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
