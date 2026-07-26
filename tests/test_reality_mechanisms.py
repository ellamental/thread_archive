"""Mechanism contracts of the search pipeline, tested on synthetic corpora.

Each test pins a deterministic contract of a shipped mechanism — an indexing
capability, a scope rule, or a rerank code path — not a ranking preference.
The bar is "the agent can see it at all" (surfaces in the top ``RECALL_LIMIT``
hits), because the failure mode guarded is a false "not found" against a
conversation that is right there. Ranking *quality* is not asserted here: that
is measured against the snapshot-bound gold files (see search_lab/README.md).

The contracts:

- **tool-call events are searchable at all** — a whole content-type going
  unindexed is invisible to every query; the cliff is zero → many.
- **a distinctive phrase stays findable thread-scoped, across reindex** — the
  record of a conversation must survive index rebuilds, not just the write
  that indexed it.
- **reindex preserves placement** — rebuilding derived state must retain the
  ranked thread order, not merely leave every record findable somewhere.
- **the MCP default scope is the whole transcript** — an answer living only in
  assistant text or in the assistant's reasoning must be reachable from a bare
  search, and a partial hit must not hide it.
- **tool output is preserved but unsearchable** — dropping it from the index
  must never drop it from the record: the thread stays reachable by the question
  that prompted it and by the call that ran it, with the output intact on read.
- **semantic scopes rank inside the scope** — a lower-similarity in-provider
  hit must survive a flood of nearer vectors from excluded providers.
- **a copied query is not an answered query** — a lexically strong but
  explicitly unanswered head must not suppress the cross-encoder that can
  recognize the differently worded answer below it.
- **long-document reranking sees the relevant occurrence** — an early
  incidental query term must not make the cross-encoder miss a later
  answering passage.
- **the reranker head includes the rescuable boundary** — a target immediately
  beyond the fixed cross-encoder pool must not be permanently unreachable.
"""

from __future__ import annotations

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


class _MarkerScorer:
    """Scripted cross-encoder: the document carrying ``marker`` is the answer."""

    def __init__(self, marker: str) -> None:
        self.marker = marker.lower()
        self.pools: list[list[list[str]]] = []

    def predict(self, pairs, batch_size, show_progress_bar):
        pairs = list(pairs)
        self.pools.append(pairs)
        return [1.0 if self.marker in doc.lower() else 0.0 for _, doc in pairs]


class _FixedQueryEmbedder:
    """Model-free semantic query arm over vectors seeded directly by a test."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed_query(self, text: str) -> list[float]:
        return self.vector


def _vector(x: float, y: float = 0.0) -> list[float]:
    return [x, y] + [0.0] * 766


def _think_turn(name: str, day: int, user_text: str, thinking_text: str,
                text_text: str) -> list[dict]:
    """A turn whose answer lives in the assistant's thinking, not its visible text."""
    stamp = f"2026-01-{day:02d}T10:00"
    return [
        {"type": "user", "uuid": f"u-{name}", "timestamp": f"{stamp}:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": f"a-{name}", "timestamp": f"{stamp}:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4", "content": [
             {"type": "thinking", "thinking": thinking_text},
             {"type": "text", "text": text_text}]}},
    ]


def _tool_result_turn(name: str, day: int, user_text: str, result_text: str,
                      is_error: bool = False) -> list[dict]:
    """A turn whose answer lives in a tool result (or a tool error)."""
    stamp = f"2026-01-{day:02d}T10:00"
    return [
        {"type": "user", "uuid": f"u-{name}", "timestamp": f"{stamp}:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": f"a-{name}", "timestamp": f"{stamp}:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4", "content": [
             {"type": "tool_use", "id": f"tu-{name}", "name": "run", "input": {}}]}},
        {"type": "user", "uuid": f"tr-{name}", "parentUuid": f"a-{name}",
         "timestamp": f"{stamp}:06Z", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": f"tu-{name}",
              "is_error": is_error, "content": result_text}]}},
    ]


# ── indexing and reindex durability ──────────────────────────────────────────

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


def test_reindex_preserves_ranked_thread_placement(tmp_path) -> None:
    query = "cobalt ledger recovery"
    focused = _import(tmp_path, "placement-focused", _session_lines(
        "placement-focused", 1,
        "cobalt ledger recovery: replay the durable checkpoint",
        "checkpoint replay completed",
    ))
    _import(tmp_path, "placement-reversed", _session_lines(
        "placement-reversed", 2,
        "recovery notes for the ledger in the cobalt service",
        "notes only",
    ))
    _import(tmp_path, "placement-long", _session_lines(
        "placement-long", 3,
        query + " " + ("diagnostic dump " * 100),
        "raw diagnostic material",
    ))
    _import(tmp_path, "placement-scattered", _session_lines(
        "placement-scattered", 4,
        "cobalt service " + ("unrelated material " * 30) + "ledger recovery",
        "an inconclusive investigation",
    ))

    before = api.search(
        query, limit=RECALL_LIMIT, content_types=["user"], rerank=False,
    )
    before_threads = [h["thread_id"] for h in before]
    assert before_threads and before_threads[0] == focused

    rebuilt = api.reindex()
    assert rebuilt.get("ok", True) is not False
    after = api.search(
        query, limit=RECALL_LIMIT, content_types=["user"], rerank=False,
    )
    assert [h["thread_id"] for h in after] == before_threads


# ── agent-facing MCP default scope (user/title, widens to everything) ────────
#
# The agent-facing thread_search default is user/title. In a corpus that
# is mostly tool content, an answer that exists ONLY in a tool result, a tool
# error, or the assistant's own reasoning would be a false not-found — so when
# the default scope comes up dry the search widens once to the whole transcript,
# and a strong match anywhere in it is kept (a low-weight tool/thinking hit is a
# real find even below a weak user hit). The controls prove the answer is indexed
# and reachable once its own content type is named; the second assert proves the
# widen reaches it unaided.

def test_partial_default_scope_hit_does_not_hide_assistant_answer(tmp_path) -> None:
    from thread_archive._mcp.server import thread_search

    query = "capture restart gate"
    answer = _import(tmp_path, "assistant-answer", _session_lines(
        "assistant-answer", 1,
        "please investigate why the recent audio went missing",
        "the capture restart gate caused the recent audio to disappear",
    ))
    _import(tmp_path, "partial-user-hit", _session_lines(
        "partial-user-hit", 2,
        "capture inventory checklist",
        "no restart diagnosis was performed",
    ))

    # Control: the answer is searchable when assistant text is requested.
    assert str(answer) in thread_search(query, content_type="text", rerank=False)
    rendered = thread_search(query, rerank=False)
    assert str(answer) in rendered, (
        "one partial default-scope hit prevented widening to the exact assistant answer"
    )


def test_tool_output_is_unsearchable_but_still_readable(tmp_path) -> None:
    """Tool output is preserved and replays in full; it is simply not matchable.

    The capability cliff this guards is *preservation*, not retrieval: dropping
    tool output from the index must never drop it from the record. A grep dump
    stops competing with real answers in search, and the conversation that ran it
    is still reachable — by what the person asked and by the tool call itself —
    with the output right there when the thread is opened.
    """
    from thread_archive._mcp.server import thread_read, thread_search

    # the question and the output share no vocabulary, so each assertion below
    # isolates one of them
    thread = _import(tmp_path, "tr-answer", _tool_result_turn(
        "tr-answer", 1, "did the parts suite run clean",
        "FAILED tests/test_flange.py::test_stress - flange stress regression"))

    # not matchable — not under the default scope, and not even when named
    assert str(thread) not in thread_search("flange stress regression", rerank=False)
    assert str(thread) not in thread_search(
        "flange stress regression", content_type="tool_result", rerank=False)
    # still reachable: the question that prompted it, and the call that ran it
    assert str(thread) in thread_search("did the parts suite run clean", rerank=False)
    assert str(thread) in thread_search("run", content_type="tool", rerank=False)
    # and the output itself is intact in the record
    assert "FAILED tests/test_flange.py::test_stress" in thread_read(
        str(thread), mode="full", tool_results=True)


def test_mcp_default_scope_reaches_thinking_answer(tmp_path) -> None:
    from thread_archive._mcp.server import thread_search

    query = "ziggurat allocator arena overflow"
    answer = _import(tmp_path, "think", _think_turn(
        "think", 1, "why did it crash",
        "the ziggurat allocator overflowed its arena boundary",
        "looks like a crash"))
    _import(tmp_path, "partial", _session_lines("partial", 2, "ziggurat status check", "ok"))
    assert str(answer) in thread_search(query, content_type="thinking", rerank=False)
    assert str(answer) in thread_search(query, rerank=False), (
        "the reasoning that holds the answer is unreachable from the MCP default scope")


def test_tool_error_output_follows_the_same_rule(tmp_path) -> None:
    """A failing tool's output is tool output: unsearchable, fully preserved."""
    from thread_archive._mcp.server import thread_read, thread_search

    thread = _import(tmp_path, "err", _tool_result_turn(
        "err", 1, "why did the obsidian import blow up",
        "ERROR: constraint violation on column epoch_id", is_error=True))

    assert str(thread) not in thread_search("epoch_id constraint violation", rerank=False)
    assert str(thread) in thread_search("why did the obsidian import blow up", rerank=False)
    assert "constraint violation on column epoch_id" in thread_read(
        str(thread), mode="full", tool_results=True)


def test_summaries_stay_out_of_search_unless_named(tmp_path) -> None:
    """Stored thread summaries are derived text (the librarian's, not the
    record), so neither the default scope nor its auto-widen may read them —
    only an explicit content_type reaches them."""
    from sqlalchemy import update

    from thread_archive._mcp.server import thread_search
    from thread_archive._retrieval import index_thread_meta
    from thread_archive._store import Thread, use_session

    query = "glockenspiel recital logistics"
    tid = _import(tmp_path, "summarized", _session_lines(
        "summarized", 1, "plan the school concert", "planned it"))
    with use_session() as s:
        s.execute(update(Thread).where(Thread.id == tid).values(
            summary="Sorted out the glockenspiel recital logistics."))
        s.commit()
    index_thread_meta()

    # Control: the summary doc is indexed and reachable once named.
    assert str(tid) in thread_search(query, content_type="summary", rerank=False)
    # A dry default scope guarantees the widen fires here — and even the widened
    # everything-scope must not surface the summary-only vocabulary.
    assert str(tid) not in thread_search(query, rerank=False), (
        "a stored summary leaked into a search that never asked for summaries")


# ── semantic scope filtering ─────────────────────────────────────────────────

def test_semantic_source_scope_ranks_inside_allowed_provider(
    tmp_path, monkeypatch,
) -> None:
    from sqlalchemy import select, update

    from thread_archive._retrieval import search, vectors
    from thread_archive._store import EventFts, Thread, use_session

    answer = _import(tmp_path, "semantic-scope-answer", _session_lines(
        "semantic-scope-answer", 1,
        "the stale projection was rebuilt from its canonical basis",
        "the recovery completed",
    ))
    noise = _import(
        tmp_path,
        "semantic-scope-noise",
        _repeated_user_lines(
            "semantic-scope-noise", 2, "generic neighboring concept", 120,
        ),
    )
    with use_session() as s:
        s.execute(update(Thread).where(Thread.id == answer).values(source="cursor"))
        s.execute(update(Thread).where(Thread.id == noise).values(source="claude-code"))
        answer_event = s.execute(
            select(EventFts.event_id).where(
                EventFts.thread_id == answer, EventFts.content_type == "user",
            )
        ).scalar_one()
        noise_events = list(s.execute(
            select(EventFts.event_id).where(
                EventFts.thread_id == noise, EventFts.content_type == "user",
            )
        ).scalars())
        s.commit()

    vectors.ensure_index()
    vectors.index_vectors([
        *((event_id, "user", _vector(1.0)) for event_id in noise_events),
        (answer_event, "user", _vector(0.8, 0.6)),
    ])
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED", raising=False)
    hits = search(
        "phase-space recovery", limit=RECALL_LIMIT, content_types=["user"],
        source=["cursor"], rerank=False,
        embedder=_FixedQueryEmbedder(_vector(1.0)),
    )

    assert hits and hits[0]["thread_id"] == answer
    assert all(h["thread_id"] != noise for h in hits)


# ── cross-encoder plumbing (scripted scorers; no model load) ─────────────────

def test_answer_below_copied_query_still_reaches_reranker(
    tmp_path, monkeypatch,
) -> None:
    from thread_archive._retrieval import SearchParams, rerank, search

    query = "watcher disappearing root cause"
    answer = _import(tmp_path, "causal-answer", _session_lines(
        "causal-answer", 1,
        "the watcher stopped after launchd dropped its KeepAlive lease",
        "the lost KeepAlive lease was the causal failure",
    ))
    copied = _import(tmp_path, "copied-query", _session_lines(
        "copied-query", 2,
        "watcher disappearing root cause — copied question; no answer recorded",
        "investigation not started",
    ))

    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    scorer = _MarkerScorer("KeepAlive lease")
    scripted = rerank.Reranker(model=scorer)
    # Auto-re-rank ships off (latency); rerank_auto=True exercises the echo-head
    # path this test is about (a copied query must still reach the cross-encoder).
    hits = search(
        query, limit=RECALL_LIMIT, content_types=["user"], reranker=scripted,
        params=SearchParams(rerank_auto=True),
    )

    assert scorer.pools, "the strong copied query incorrectly suppressed reranking"
    assert hits[0]["thread_id"] == answer
    assert hits[0]["thread_id"] != copied


def test_long_document_rerank_window_uses_answering_occurrence(
    tmp_path, monkeypatch,
) -> None:
    from thread_archive._retrieval import rerank, search

    query = "watcher restart failure explanation"
    answer_text = (
        "watcher mentioned incidentally. "
        + ("unrelated preface material. " * 300)
        + "restart failure explanation: the generation barrier rejected stale state"
    )
    answer = _import(tmp_path, "long-answer", _session_lines(
        "long-answer", 1, answer_text, "the later passage contains the conclusion",
    ))
    copied = _import(tmp_path, "short-copy", _session_lines(
        "short-copy", 2,
        "watcher restart failure explanation — copied question with no conclusion",
        "no analysis recorded",
    ))

    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    scorer = _MarkerScorer("generation barrier")
    scripted = rerank.Reranker(model=scorer)
    hits = search(
        query, limit=RECALL_LIMIT, content_types=["user"], rerank=True,
        reranker=scripted,
    )

    assert scorer.pools
    assert any(
        "generation barrier" in doc.lower() for _, doc in scorer.pools[0]
    ), "the reranker window stopped at an early incidental term"
    assert hits[0]["thread_id"] == answer
    assert hits[0]["thread_id"] != copied


def test_relevant_hit_just_beyond_rerank_pool_can_be_rescued(
    tmp_path, monkeypatch,
) -> None:
    from thread_archive._retrieval import rank, rerank, search

    query = "orbital cache repair"
    answer_text = (
        query + " " + ("background diagnostic material " * 80)
        + "generation semaphore is the verified resolution"
    )
    answer = _import(tmp_path, "rerank-boundary-answer", _session_lines(
        "rerank-boundary-answer", 1, answer_text, "the semaphore fix was applied",
    ))
    for i in range(rank.RERANK_POOL):
        _import(tmp_path, f"rerank-head-{i}", _session_lines(
            f"rerank-head-{i}", 2,
            f"{query} checklist candidate {i}; no resolution recorded",
            "investigation pending",
        ))

    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)
    scorer = _MarkerScorer("generation semaphore")
    scripted = rerank.Reranker(model=scorer)
    hits = search(
        query, limit=rank.RERANK_POOL + 1, content_types=["user"], rerank=True,
        reranker=scripted,
    )

    assert answer in {h["thread_id"] for h in hits}
    assert scorer.pools
    assert any(
        "generation semaphore" in doc.lower() for _, doc in scorer.pools[0]
    ), "the answer landed immediately beyond the fixed reranker head"
    assert hits[0]["thread_id"] == answer
