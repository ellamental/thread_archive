"""Live-capture streams render and index like file imports.

File importers emit per-block ``text_complete``/``thinking_complete`` twins
beside each ``api_request_completed``; live-capture sources (loom, needle,
officiant, …) emit token ``text_delta``/``thinking_delta`` events — or
nothing granular at all — and the assembled turn exists only in the summary's
``content_blocks``. One rule serves both surfaces: an api_call's content comes
from its twins when they exist, else from the summary — matched by api_call_id
*and* by text, because a doubly captured thread (file import + live stream of
the same session) carries the twins under different api_call_ids than the
stream's summaries.

Reader side: :func:`read._absorb_stream_deltas`. Index side: the twin gate in
:func:`fts.index_events` / :func:`fts.rebuild_fts`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._retrieval import read_thread, search
from thread_archive._retrieval.fts import rebuild_fts
from thread_archive._store import Event, Thread, get_session, init_db, use_session


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


def _seed_thread(tid: int, events: list[tuple], source: str = "demo-harness") -> int:
    """events: (event_type, payload, api_call_id) triples in id order."""
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title=f"stream {tid}", thread_type="conversation",
                     source=source, source_id=f"sess-{tid}",
                     inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for i, (et, payload, ac) in enumerate(events, start=1):
            s.add(Event(id=tid * 1000 + i, thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, api_call_id=ac, occurred_at=_dt(i)))
        s.commit()
    return tid


_STREAMED_TURN = [
    ("user_message_sent", {"content": "what is the plan"}, None),
    ("api_request_started", {"model": "claude-opus-4"}, "call-1"),
    ("thinking_delta", {"text": "let me ", "block_index": 0}, "call-1"),
    ("thinking_delta", {"text": "think", "block_index": 0}, "call-1"),
    ("text_delta", {"text": "the plan ", "block_index": 1}, "call-1"),
    ("text_delta", {"text": "is GAMMA", "block_index": 1}, "call-1"),
    ("api_request_completed", {
        "model": "claude-opus-4",
        "content_blocks": [
            {"type": "thinking", "thinking": "let me think"},
            {"type": "text", "text": "the plan is GAMMA"},
        ],
    }, "call-1"),
    ("stream_completed", {"reason": "complete"}, None),
]


def test_delta_stream_renders_assistant_text(archive_home) -> None:
    tid = _seed_thread(40, _STREAMED_TURN)
    chat = read_thread(tid, mode="chat")
    assert "the plan is GAMMA" in chat
    full = read_thread(tid, mode="full")
    assert "the plan is GAMMA" in full
    assert "[thinking]\nlet me think" in full
    # never per-token lines, never machinery noise
    assert "[text_delta]" not in full and "[thinking_delta]" not in full


def test_summary_only_call_renders(archive_home) -> None:
    """No deltas at all (officiant-shaped): the summary alone carries the turn."""
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("api_request_completed", {
            "content_blocks": [{"type": "text", "text": "OFFICIANT says hello"}],
        }, "call-2"),
    ]
    tid = _seed_thread(41, events, source="officiant")
    assert "OFFICIANT says hello" in read_thread(tid, mode="chat")


def test_orphan_deltas_stitch_without_summary(archive_home) -> None:
    """A stream killed mid-call: no api_request_completed, deltas concatenate."""
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("text_delta", {"text": "partial ", "block_index": 0}, "call-3"),
        ("text_delta", {"text": "ANSWER", "block_index": 0}, "call-3"),
    ]
    tid = _seed_thread(42, events)
    assert "partial ANSWER" in read_thread(tid, mode="chat")


def test_twinned_call_does_not_double_render(archive_home) -> None:
    """File-import shape: twins beside the summary — the summary must stay silent."""
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("text_complete", {"text": "the answer is DELTA"}, "call-4"),
        ("api_request_completed", {
            "content_blocks": [{"type": "text", "text": "the answer is DELTA"}],
        }, "call-4"),
    ]
    tid = _seed_thread(43, events, source="claude-code")
    assert read_thread(tid, mode="chat").count("the answer is DELTA") == 1


def test_doubly_captured_turn_renders_once(archive_home) -> None:
    """The twin lives under a DIFFERENT api_call_id than the stream's summary
    (file import + live capture of the same session merged into one thread) —
    the text-level match must catch what the call-id match can't."""
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("text_complete", {"text": "the answer is EPSILON"}, "file-call-9"),
        ("api_request_completed", {
            "content_blocks": [{"type": "text", "text": "the answer is EPSILON"}],
        }, "live-call-9"),
    ]
    tid = _seed_thread(44, events, source="claude-code")
    assert read_thread(tid, mode="chat").count("the answer is EPSILON") == 1


# ── the index side: the same gate, same rule ─────────────────────────────────

def _fts_contents(tid: int) -> list[tuple[str, str]]:
    from sqlalchemy import select

    from thread_archive._store import EventFts

    with get_session() as s:
        return [
            (r.content, r.content_type)
            for r in s.execute(select(EventFts).where(EventFts.thread_id == tid)).scalars()
        ]


def test_rebuild_indexes_untwinned_summary(archive_home) -> None:
    tid = _seed_thread(45, _STREAMED_TURN)
    with get_session() as s:
        rebuild_fts(s)
        s.commit()
    rows = _fts_contents(tid)
    assert ("the plan is GAMMA", "text") in rows
    assert ("let me think", "thinking") in rows
    # deltas themselves are never indexed
    assert not any("block_index" in c for c, _ in rows)
    hits = search("GAMMA", limit=5)
    assert hits and hits[0]["thread_id"] == tid


def test_rebuild_skips_twinned_summary(archive_home) -> None:
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("text_complete", {"text": "the answer is DELTA"}, "call-4"),
        ("api_request_completed", {
            "content_blocks": [{"type": "text", "text": "the answer is DELTA"}],
        }, "call-4"),
    ]
    tid = _seed_thread(46, events, source="claude-code")
    with get_session() as s:
        rebuild_fts(s)
        s.commit()
    texts = [c for c, ct in _fts_contents(tid) if ct == "text"]
    assert texts.count("the answer is DELTA") == 1  # the twin's copy, not the summary's


def test_rebuild_skips_doubly_captured_summary(archive_home) -> None:
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("text_complete", {"text": "the answer is EPSILON"}, "file-call-9"),
        ("api_request_completed", {
            "content_blocks": [{"type": "text", "text": "the answer is EPSILON"}],
        }, "live-call-9"),
    ]
    tid = _seed_thread(47, events, source="claude-code")
    with get_session() as s:
        rebuild_fts(s)
        s.commit()
    texts = [c for c, ct in _fts_contents(tid) if ct == "text"]
    assert texts.count("the answer is EPSILON") == 1


def test_rebuild_stitches_deltas_when_summary_has_no_blocks(archive_home) -> None:
    """A recovery-path summary without content_blocks: the call's text exists
    only as deltas — the indexer stitches them, anchored to the summary event,
    and deep verify judges the same stitched text as covered."""
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("text_delta", {"text": "recovered ", "block_index": 0}, "call-5"),
        ("text_delta", {"text": "ZETA", "block_index": 0}, "call-5"),
        ("api_request_completed", {"model": "m"}, "call-5"),  # no content_blocks
    ]
    tid = _seed_thread(49, events, source="demo-harness-recovered")
    with get_session() as s:
        rebuild_fts(s)
        s.commit()
    rows = _fts_contents(tid)
    assert ("recovered ZETA", "text") in rows
    # reader renders the same stitched text, anchored at the summary's event id
    out = read_thread(tid, mode="chat")
    assert "recovered ZETA" in out
    assert f"event:{tid * 1000 + 4}" in out  # the api_request_completed's id
    # deep verify's coverage check judges the same stitched text as covered
    # (the seeded events have no truth files, so only the fts section applies)
    from thread_archive import _api as ta

    deep_fts = ta.verify(deep=True)["deep"]["fts"]
    assert deep_fts["unindexed_events"] == 0


def test_rebuild_indexes_arcless_call_at_delta_anchor(archive_home) -> None:
    """A stream killed before its summary arrived: no api_request_completed
    exists, so the stitched text anchors at the block's last delta event — the
    same event id the reader renders it under."""
    events = [
        ("user_message_sent", {"content": "hi"}, None),
        ("text_delta", {"text": "dying ", "block_index": 0}, "call-6"),
        ("text_delta", {"text": "words ETA", "block_index": 0}, "call-6"),
        # no api_request_completed for call-6 — the stream died here
    ]
    tid = _seed_thread(50, events)
    with get_session() as s:
        rebuild_fts(s)
        s.commit()
    rows = _fts_contents(tid)
    assert ("dying words ETA", "text") in rows
    out = read_thread(tid, mode="chat")
    assert "dying words ETA" in out
    assert f"event:{tid * 1000 + 3}" in out  # the last delta's id, both surfaces


def test_incremental_index_gates_like_rebuild(archive_home) -> None:
    """index_events (the import write seam) applies the same twin gate."""
    from thread_archive._retrieval.fts import ensure_fts, index_events

    tid = _seed_thread(48, _STREAMED_TURN)
    with get_session() as s:
        ensure_fts(s)
        events = s.query(Event).filter(Event.thread_id == tid).order_by(Event.id).all()
        n = index_events(s, events)
        s.commit()
    assert n > 0
    rows = _fts_contents(tid)
    assert ("the plan is GAMMA", "text") in rows
    texts = [c for c, _ in rows]
    assert texts.count("the plan is GAMMA") == 1
