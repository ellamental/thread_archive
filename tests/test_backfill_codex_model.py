"""The codex-model backfill: placeholder turns are re-attributed to the model that
served them, in the store AND in the truth, and the repaired rows are byte-identical
to what a fresh import now produces — so a later re-import of the same session dedups
against them instead of doubling the thread.

The pre-fix state is reproduced honestly: the importer's model rule is stubbed out for
the seeding import, which is exactly what the old importer did (it looked only at
``session_meta.model``, which Codex no longer writes), so store and truth both land in
the state the live archive is actually in.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from thread_archive._importers import codex as codex_mod
from thread_archive._importers import import_codex_session_incremental
from thread_archive._scripts import backfill_codex_model as mod
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._truth.jsonl_log import _hash_key_check, _shard_depth, _thread_file, log_dir

SESSION = [
    {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"id": "sess", "cwd": "/proj", "model_provider": "openai"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:01Z",
     "payload": {"type": "user_message", "message": "first turn", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
     "payload": {"type": "task_started", "turn_id": "t1"}},
    {"type": "turn_context", "timestamp": "2026-01-01T10:00:03Z",
     "payload": {"turn_id": "t1", "model": "gpt-5.6-sol"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:04Z",
     "payload": {"type": "agent_message", "message": "answered by sol"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:01:00Z",
     "payload": {"type": "user_message", "message": "second turn", "turn_id": "t2"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:01:01Z",
     "payload": {"type": "thread_settings_applied",
                 "thread_settings": {"model": "gpt-5.6-thinking"}}},
    {"type": "turn_context", "timestamp": "2026-01-01T10:01:02Z",
     "payload": {"turn_id": "t2", "model": "gpt-5.6-thinking"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:01:03Z",
     "payload": {"type": "agent_message", "message": "answered by thinking"}},
]

# A codex old enough to name no model anywhere — nothing to recover from.
SESSION_MODEL_LESS = [
    {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"id": "old", "cwd": "/proj"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:01Z",
     "payload": {"type": "user_message", "message": "hello", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
     "payload": {"type": "agent_message", "message": "hi"}},
]


@pytest.fixture(autouse=True)
def _pin_home(archive_home):
    """Prove the archive is pinned to the tmp home, before and after every test here.

    These tests exercise a repair that rewrites truth files in place. A home that
    escapes the fixture — an env patch undone too widely, a stale engine — would aim
    that repair at the real archive's truth, so the pin is asserted, not assumed."""
    assert log_dir().is_relative_to(archive_home)
    yield
    assert log_dir().is_relative_to(archive_home)


def _write(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _seed_prefix_import(archive_home, lines, name, source_id) -> int:
    """Import as the pre-fix importer did — blind to every model-naming line.

    The stub is scoped to its own MonkeyPatch context, NOT undone on the fixture's:
    ``archive_home``'s tmp home is itself a monkeypatched env var, so a shared
    ``undo()`` would unpin the home mid-test and point the run at the real archive.
    """
    f = archive_home / name
    _write(f, lines)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(codex_mod, "_codex_line_model", lambda line: None)
        return import_codex_session_incremental(f, source_id).thread_id


def _models(thread_id: int) -> list[tuple[str, str]]:
    with get_session() as s:
        rows = s.execute(
            select(Event.event_type, Event.payload)
            .where(Event.thread_id == thread_id, Event.event_type.in_(mod.MODEL_EVENTS))
            .order_by(Event.id)
        ).all()
    return [(t, p["model"]) for t, p in rows]


def _truth_events(thread_id: int) -> list[dict]:
    d = log_dir()
    path = _thread_file(d, thread_id, _shard_depth(d))
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a torn line is content too — just not an event record
        if rec.get("type") == "event":
            out.append(rec)
    return out


def test_placeholder_is_the_pre_fix_state(archive_home) -> None:
    init_db()
    tid = _seed_prefix_import(archive_home, SESSION, "codex.jsonl", "s1")
    assert {m for _, m in _models(tid)} == {"codex"}


def test_backfill_dry_run_reports_without_writing(archive_home) -> None:
    init_db()
    tid = _seed_prefix_import(archive_home, SESSION, "codex.jsonl", "s1")

    totals = mod.run(apply=False)
    assert totals["threads_with_placeholder"] == 1
    assert totals["threads_repaired"] == 1
    assert totals["events"] == 4  # started + completed, on two turns
    assert totals["by_model"] == {"gpt-5.6-sol": 2, "gpt-5.6-thinking": 2}
    assert {m for _, m in _models(tid)} == {"codex"}, "dry run must not write"


def test_backfill_apply_rewrites_store_and_truth_and_is_idempotent(
    archive_home, tmp_path
) -> None:
    init_db()
    tid = _seed_prefix_import(archive_home, SESSION, "codex.jsonl", "s1")
    backup = tmp_path / "backup.jsonl"

    totals = mod.run(apply=True, backup_path=backup)
    assert totals.get("write_errors", 0) == 0
    assert totals["truth_lines"] == 4

    # Each turn is attributed to the model that served it — the switch lands on turn 2.
    assert _models(tid) == [
        ("api_request_started", "gpt-5.6-sol"),
        ("api_request_completed", "gpt-5.6-sol"),
        ("api_request_started", "gpt-5.6-thinking"),
        ("api_request_completed", "gpt-5.6-thinking"),
    ]

    # Truth carries the same rewrite, and every other line survived it.
    truth = {e["id"]: e for e in _truth_events(tid)}
    with get_session() as s:
        rows = s.execute(select(Event).where(Event.thread_id == tid).order_by(Event.id)).scalars().all()
    assert set(truth) == {e.id for e in rows}, "no truth line lost or invented"
    for ev in rows:
        assert truth[ev.id]["payload"] == ev.payload
        assert truth[ev.id]["dedup_key"] == ev.dedup_key
        # The key still hashes to the payload it names.
        if ev.dedup_key:
            assert _hash_key_check(ev.payload, ev.dedup_key) is not False

    backed_up = [json.loads(ln) for ln in backup.read_text().splitlines()]
    assert len(backed_up) == 4
    assert all(b["old_model"] == "codex" and b["old_dedup_key"] != b["new_dedup_key"] for b in backed_up)

    # Re-running finds nothing: the placeholder is gone, so no thread plans an update.
    again = mod.run(apply=True)
    assert again.get("threads_with_placeholder", 0) == 0
    assert again.get("events", 0) == 0


def test_backfilled_rows_match_a_fresh_import(archive_home) -> None:
    """The repaired identity is the one a fresh import now computes — which is what
    makes a future re-import of the same session dedup instead of double the thread."""
    init_db()
    tid = _seed_prefix_import(archive_home, SESSION, "codex.jsonl", "s1")
    mod.run(apply=True)

    fresh = archive_home / "codex-fresh.jsonl"
    _write(fresh, SESSION)
    fresh_tid = import_codex_session_incremental(fresh, "s2").thread_id

    def _identity(thread_id: int) -> list[tuple[str, str, str]]:
        with get_session() as s:
            rows = s.execute(
                select(Event).where(Event.thread_id == thread_id).order_by(Event.id)
            ).scalars().all()
        return [(e.event_type, json.dumps(e.payload, sort_keys=True), e.dedup_key or "") for e in rows]

    assert _identity(tid) == _identity(fresh_tid)


def test_thread_naming_no_model_is_left_alone(archive_home) -> None:
    init_db()
    tid = _seed_prefix_import(archive_home, SESSION_MODEL_LESS, "old.jsonl", "s-old")

    totals = mod.run(apply=True)
    assert totals["threads_with_placeholder"] == 1
    assert totals["threads_unresolved"] == 1
    assert totals["events_unresolved"] == 2
    assert totals.get("events", 0) == 0
    assert {m for _, m in _models(tid)} == {"codex"}, "unrecoverable stays honest, not guessed"


def test_rollout_answers_what_the_archive_cannot(archive_home, monkeypatch) -> None:
    """A thread imported before the importer preserved unmodeled lines holds no
    turn_context block at all — its only surviving record of the model is Codex's own
    session file, which the backfill replays through the same importer path."""
    init_db()
    stripped = [ln for ln in SESSION if ln["type"] != "turn_context"
                and ln["payload"].get("type") != "thread_settings_applied"]
    tid = _seed_prefix_import(archive_home, stripped, "codex.jsonl", "s1")

    rollout = archive_home / "rollout-s1.jsonl"
    _write(rollout, SESSION)  # the full file, still on disk
    monkeypatch.setattr(mod, "rollout_index", lambda: {"s1": rollout})

    totals = mod.run(apply=True)
    assert totals["threads_repaired"] == 1
    assert totals.get("conflicts", 0) == 0
    assert _models(tid) == [
        ("api_request_started", "gpt-5.6-sol"),
        ("api_request_completed", "gpt-5.6-sol"),
        ("api_request_started", "gpt-5.6-thinking"),
        ("api_request_completed", "gpt-5.6-thinking"),
    ]


def test_disagreeing_sources_leave_the_turn_alone(archive_home, monkeypatch) -> None:
    """Archive and rollout naming different models for one turn is a bug in one of
    them; the repair has no way to pick, so it skips rather than guesses."""
    init_db()
    tid = _seed_prefix_import(archive_home, SESSION, "codex.jsonl", "s1")

    lying = [dict(ln, payload=dict(ln["payload"], model="gpt-9-imaginary"))
             if ln["type"] == "turn_context" else ln for ln in SESSION]
    rollout = archive_home / "rollout-s1.jsonl"
    _write(rollout, lying)
    monkeypatch.setattr(mod, "rollout_index", lambda: {"s1": rollout})

    totals = mod.run(apply=True)
    assert totals["conflicts"] == 2
    assert totals.get("events", 0) == 0
    assert {m for _, m in _models(tid)} == {"codex"}


def test_rekey_preserves_anchor_and_block_and_rehashes(archive_home) -> None:
    # codex's anchor is a timestamp — it contains colons, so the key must split from
    # the right, and an anchorless (c=hash) key moves with its content.
    payload = {"model": "gpt-5.6-sol", "provider_data": {"provider_message_id": "x"}}
    key = mod.rekey("2026-01-01T10:00:02.5Z:api_request_started::0123456789abcdef", payload)
    anchor, etype, block, tail = key.rsplit(":", 3)
    assert (anchor, etype, block) == ("2026-01-01T10:00:02.5Z", "api_request_started", "")
    assert _hash_key_check(payload, key) is True

    anchorless = mod.rekey("c=0123456789abcdef:api_request_completed::0123456789abcdef", payload)
    assert _hash_key_check(payload, anchorless) is True
    assert anchorless.startswith(f"c={anchorless.rsplit(':', 1)[-1]}:"), "anchor follows content"

    # A key with no hash tail carries no content claim — left exactly as found.
    assert mod.rekey("some-legacy-key", payload) == "some-legacy-key"


@pytest.mark.parametrize("bad", ['{"type": "event", "id": ', "not json at all"])
def test_torn_truth_lines_survive_the_patch(archive_home, bad) -> None:
    """A torn line is copied through, not dropped — the repair must not be the thing
    that loses content the log already holds."""
    init_db()
    tid = _seed_prefix_import(archive_home, SESSION, "codex.jsonl", "s1")
    path = _thread_file(log_dir(), tid, _shard_depth(log_dir()))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(bad + "\n")

    mod.run(apply=True)
    assert bad in path.read_text(encoding="utf-8").splitlines()
    assert len(_truth_events(tid)) > 0
