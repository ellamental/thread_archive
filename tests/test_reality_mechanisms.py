"""Reality-integrity mechanism goldens — the failure *shapes* behind real incidents.

Each of these goldens reproduces, on a synthetic corpus, the mechanism behind a
real search failure observed in production — so the suite carries the
regression class without carrying anyone's data. The bar is deliberately low:
"the agent can see it at all" (surfaces in the top ``RECALL_LIMIT`` hits, not
"ranks first"), because the failure mode being guarded is a false "not found"
against a conversation that is right there — the archive telling its operator
that part of their own history didn't happen.

The four mechanisms:

- **rare bigram under a frequency flood** — a two-word query whose first token
  is high-frequency must not have its exact-match threads drowned by newer
  single-token hits (the OR-split failure shape: "doc ock" losing to a flood
  of "docs").
- **tool-call events are searchable at all** — a whole content-type going
  unindexed is invisible to every query; the cliff is zero → many.
- **a distinctive phrase stays findable thread-scoped, across reindex** — the
  record of a conversation must survive index rebuilds, not just the write
  that indexed it.
- **one thread cannot monopolize a grouped result pool** — repeated copies of
  one matching prompt must not fill the event-level candidate window before
  deduplication and hide every other matching conversation.
"""

from __future__ import annotations

import pytest

from thread_archive import _api as api

from .helpers import write_jsonl

# The recall window: buried below this is "not seen".
RECALL_LIMIT = 25


def _session_lines(name: str, day: int, user_text: str, assistant_text: str) -> list[dict]:
    """A one-turn claude-code session dated ``day`` days into 2026-01."""
    stamp = f"2026-01-{day:02d}T10:00"
    return [
        {"type": "user", "uuid": f"u-{name}", "timestamp": f"{stamp}:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": f"a-{name}", "timestamp": f"{stamp}:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": assistant_text}]}},
    ]


def _import(tmp_path, name: str, lines: list[dict]) -> int:
    f = tmp_path / f"{name}.jsonl"
    write_jsonl(f, lines)
    return api.import_path(f).thread_id


def _repeated_user_lines(name: str, day: int, text: str, count: int) -> list[dict]:
    """A burst of distinct user events carrying the same text in one thread."""
    return [
        {
            "type": "user",
            "uuid": f"u-{name}-{i}",
            "timestamp": f"2026-01-{day:02d}T10:{i // 60:02d}:{i % 60:02d}Z",
            "cwd": "/proj",
            "message": {"role": "user", "content": text},
        }
        for i in range(count)
    ]


def test_rare_bigram_survives_frequency_flood(tmp_path) -> None:
    # Two old threads hold the exact bigram; thirty newer threads each use the
    # bigram's first token in ordinary single-token sentences. If ranking
    # regresses to an OR-split ordered by frequency/recency, the flood fills
    # the top-25 window and the real threads vanish from view.
    needles = {
        _import(tmp_path, "needle-1", _session_lines(
            "needle-1", 1, "let's finalize the sprocket octavia mounting design",
            "the sprocket octavia mount needs a thrust bearing")),
        _import(tmp_path, "needle-2", _session_lines(
            "needle-2", 2, "measurements for the sprocket octavia arrived",
            "logging the sprocket octavia measurements")),
    }
    for i in range(30):
        _import(tmp_path, f"flood-{i}", _session_lines(
            f"flood-{i}", 3 + (i % 27),
            f"the sprocket batch {i} passed inspection today",
            f"batch {i} sprocket results recorded"))

    hits = api.search("sprocket octavia", limit=RECALL_LIMIT)
    surfaced = {h["thread_id"] for h in hits}
    assert surfaced & needles, (
        f"the exact-bigram threads {sorted(needles)} are buried below the "
        f"single-token flood (top-{RECALL_LIMIT} threads: {sorted(surfaced)}) — "
        "the OR-split failure shape is back."
    )


@pytest.mark.xfail(
    reason="grouped search caps event candidates before duplicate/thread folding",
    strict=True,
)
def test_duplicate_prompt_flood_cannot_monopolize_grouped_result_pool(tmp_path) -> None:
    # The default result shape spends its limit on threads, not events. A single
    # noisy thread can nevertheless put hundreds of identical events at the head
    # of FTS's event-level candidate pool. If deduplication happens only after
    # that finite pool is fetched, the copies collapse to one displayed row but
    # leave no candidate from the conversation that recorded the actual answer.
    query = "amber lattice deadlock fix"
    answer = _import(tmp_path, "answer", _session_lines(
        "answer", 1,
        "amber lattice deadlock fix: serialize reclamation at the generation barrier",
        "the generation barrier is the definitive resolution",
    ))
    noise = _import(
        tmp_path,
        "duplicate-flood",
        _repeated_user_lines("duplicate-flood", 2, query, RECALL_LIMIT * 10),
    )

    # Control: this is a placement failure, not an indexing failure.
    assert api.search(
        query, thread_id=answer, content_types=["user"], rerank=False,
    )

    hits = api.search(
        query, limit=RECALL_LIMIT, content_types=["user"], rerank=False,
    )
    placed_threads = [h["thread_id"] for h in hits]
    assert noise in placed_threads
    assert answer in placed_threads, (
        f"the answer thread {answer} was buried by repeated events from one noisy "
        f"thread (top-{RECALL_LIMIT} threads: {placed_threads})"
    )
    assert placed_threads.index(answer) < 2


def test_tool_call_events_are_searchable(tmp_path) -> None:
    # The capability cliff: a tool CALL (not a text mention of the tool) must be
    # reachable through content_type='tool'. When tool events aren't indexed,
    # search cannot confirm a tool was ever used — against an operator who is
    # sure it was.
    lines = [
        {"type": "user", "uuid": "u-t", "timestamp": "2026-01-05T10:00:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": "check the flange stock"}},
        {"type": "assistant", "uuid": "a-t", "timestamp": "2026-01-05T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4", "content": [
             {"type": "tool_use", "id": "tu-t", "name": "stock_lookup",
              "input": {"part": "flange", "warehouse": "east"}}]}},
        {"type": "user", "uuid": "u-t2", "parentUuid": "a-t", "timestamp": "2026-01-05T10:00:06Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu-t", "content": "12 in stock"}]}},
    ]
    tid = _import(tmp_path, "tooluse", lines)

    hits = api.search("stock_lookup", content_types=["tool"], limit=RECALL_LIMIT)
    assert hits, "no tool-typed hits for a tool that was called — tool events are not indexed"
    assert tid in {h["thread_id"] for h in hits}


def test_record_phrase_survives_reindex(tmp_path) -> None:
    # The record guard's real fear isn't the first write — it's a rebuild
    # quietly dropping what was findable. Thread-scoped, like the harness's
    # record tier: "is this still indexed", not "does it win global ranking".
    tid = _import(tmp_path, "record", _session_lines(
        "record", 7, "that image was impossible to unlearn once seen",
        "recorded your words exactly"))

    assert api.search("impossible to unlearn", thread_id=tid, limit=RECALL_LIMIT)

    r = api.reindex()
    assert r.get("ok", True) is not False, f"reindex failed: {r}"
    assert api.search("impossible to unlearn", thread_id=tid, limit=RECALL_LIMIT), (
        "the phrase was findable before reindex and gone after — the rebuild "
        "dropped a committed record."
    )
