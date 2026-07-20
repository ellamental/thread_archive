"""Reality-integrity mechanism goldens — the failure *shapes* behind real incidents.

Each of these goldens reproduces, on a synthetic corpus, the mechanism behind a
real search failure observed in production — so the suite carries the
regression class without carrying anyone's data. The bar is deliberately low:
"the agent can see it at all" (surfaces in the top ``RECALL_LIMIT`` hits, not
"ranks first"), because the failure mode being guarded is a false "not found"
against a conversation that is right there — the archive telling its operator
that part of their own history didn't happen.

The mechanisms:

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
- **literal identifiers survive split-token floods** — prose containing the
  tokenizer-equivalent words must not keep an exact underscore identifier out
  of the pool before the literal-match pass can run.
- **a copied query is not an answered query** — a lexically strong but explicitly
  unanswered head must not suppress the cross-encoder that can recognize the
  differently worded answer below it.
- **assistant widening is not blocked by one stray token** — a partial user/meta
  hit must not prevent the MCP default scope from reaching an exact answer in
  assistant text.
- **long-document reranking sees the relevant occurrence** — an early incidental
  query term must not make the cross-encoder miss a later answering passage.
- **primary evidence outranks generated synthesis** — a short stored summary must
  yield to a comparably matching user record instead of winning on density.
- **substrings are not query terms** — a partial lexical fallback must not turn
  ``author`` into a strong two-term match for ``auth failure``.
- **the reranker head includes the rescuable boundary** — a target immediately
  beyond the fixed cross-encoder pool must not be permanently unreachable.
- **semantic scopes rank inside the scope** — a lower-similarity in-provider hit
  must survive a flood of nearer vectors from excluded providers.
- **reindex preserves placement** — rebuilding derived state must retain the
  ranked thread order, not merely leave every record findable somewhere.
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


@pytest.mark.xfail(
    reason="split-token MATCH fills the pool before the literal identifier pass",
    strict=True,
)
def test_literal_identifier_survives_split_token_frequency_flood(tmp_path) -> None:
    # FTS tokenizes an underscore identifier into the same phrase as split prose.
    # The exact-literal LIKE pass is therefore the only arm that can distinguish
    # the old implementation record from a large set of newer prose mentions.
    query = "frobnicate_widget"
    answer = _import(tmp_path, "identifier-answer", _session_lines(
        "identifier-answer", 1,
        "frobnicate_widget owns the retry lock and generation counter",
        "the literal identifier names the implementation that fixed the race",
    ))
    for i in range(30):
        text = f"frobnicate widget inventory batch {i}"
        _import(
            tmp_path,
            f"identifier-flood-{i}",
            _repeated_user_lines(f"identifier-flood-{i}", 2, text, 10),
        )

    assert api.search(
        query, thread_id=answer, content_types=["user"], rerank=False,
    )
    hits = api.search(
        query, limit=RECALL_LIMIT, content_types=["user"], rerank=False,
    )
    placed_threads = [h["thread_id"] for h in hits]
    assert answer in placed_threads, (
        f"literal identifier thread {answer} was buried by split-token prose "
        f"(top-{RECALL_LIMIT} threads: {placed_threads})"
    )
    assert placed_threads.index(answer) < 3


@pytest.mark.xfail(
    reason="a strong copied query suppresses the answer-disambiguating reranker",
    strict=True,
)
def test_answer_below_copied_query_still_reaches_reranker(
    tmp_path, monkeypatch,
) -> None:
    from thread_archive._retrieval import rerank, search

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
    hits = search(
        query, limit=RECALL_LIMIT, content_types=["user"], reranker=scripted,
    )

    assert scorer.pools, "the strong copied query incorrectly suppressed reranking"
    assert hits[0]["thread_id"] == answer
    assert hits[0]["thread_id"] != copied


@pytest.mark.xfail(
    reason="one literal token blocks the MCP default-scope assistant widening",
    strict=True,
)
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


@pytest.mark.xfail(
    reason="reranking centers a long document on its first incidental term",
    strict=True,
)
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


def test_primary_user_evidence_outranks_generated_summary(tmp_path) -> None:
    from sqlalchemy import update

    from thread_archive._retrieval import index_thread_meta
    from thread_archive._store import Thread, use_session

    query = "quartz scheduler rollback"
    answer = _import(tmp_path, "primary-evidence", _session_lines(
        "primary-evidence", 1,
        "quartz scheduler rollback: restore the last durable lease before replay",
        "the operator recorded the rollback procedure",
    ))
    summary_only = _import(tmp_path, "summary-distractor", _session_lines(
        "summary-distractor", 2,
        "unrelated maintenance notes",
        "nothing here discusses the scheduler incident",
    ))
    with use_session() as s:
        s.execute(update(Thread).where(Thread.id == summary_only).values(
            summary="quartz scheduler rollback",
        ))
        s.commit()
    index_thread_meta()

    hits = api.search(
        query, limit=RECALL_LIMIT, content_types=["user", "summary"], rerank=False,
    )
    assert {answer, summary_only} <= {h["thread_id"] for h in hits}
    assert hits[0]["thread_id"] == answer
    assert hits[0]["content_type"] == "user"


@pytest.mark.xfail(
    reason="four-character ranking terms match as substrings inside longer words",
    strict=True,
)
def test_substring_collision_does_not_outrank_real_query_term(tmp_path) -> None:
    query = "auth failure"
    answer = _import(tmp_path, "auth-answer", _session_lines(
        "auth-answer", 1,
        "auth failed after the refresh token expired",
        "rotating the token restored login",
    ))
    collision = _import(tmp_path, "author-collision", _session_lines(
        "author-collision", 2,
        "the author reported a failure in the manuscript review",
        "the editor requested another draft",
    ))

    hits = api.search(
        query, limit=RECALL_LIMIT, content_types=["user"], rerank=False,
    )
    assert {answer, collision} <= {h["thread_id"] for h in hits}
    assert hits[0]["thread_id"] == answer, (
        "the ranker counted 'auth' inside 'author' and promoted the collision"
    )


@pytest.mark.xfail(
    reason="the fixed 24-document reranker head cannot rescue position 25",
    strict=True,
)
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
