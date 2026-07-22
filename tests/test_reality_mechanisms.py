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

from thread_archive import _api as api

from .helpers import write_jsonl

# The recall window: buried below this is "not seen".
RECALL_LIMIT = 25

# The scope the archive's own eval measures (evals/retrieval_eval.py): the whole
# event pool minus the two thread-meta docs. So the corpus-noise goldens below
# search exactly this — the failures are ranking/recall inside a pool that is
# ~72% tool content, not scope exclusion.
EXCLUDE_META = ["title", "summary"]


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


# ─────────────────────────────────────────────────────────────────────────────
# Agentic tool-heavy corpus mechanisms
#
# The mechanisms above are generic search cliffs. These are the ones this corpus
# actually lives with: 72% of the index is tool content, tool_results reach ~1.8M
# chars, user messages are ~3% of events, CLAUDE.md / system-reminder boilerplate
# rides hundreds of threads verbatim, and the real mined queries are paraphrased
# recall where the gold owns a terse verbatim receipt while denser, newer, more
# numerous threads merely restate the same vocabulary. Each guard below pins a
# property that keeps a real gold in the recall window. The recall bar is the same
# as above: seen at all.
#
# The dominant shape is a flood of near-duplicate low-value threads (echoes,
# routine ops, pending todos, re-asks, injected boilerplate) that would bury the
# one terse or old authoritative thread — each near-copy differs only by a run
# index or counter, so the cross-thread near-duplicate fold (rank._norm_content
# digit-folds before comparing) collapses the flood to one representative and the
# distinct thread keeps its row. The distinct triggers matter because they are the
# real scenarios the one anti-flood fold has to cover. The agent-facing scope gaps
# below are a separate fix: the MCP default scope widens to the whole transcript
# when it comes up dry, so an answer living only in tool or thinking content is
# reachable.


def _eval_search(query: str, **kw):
    """Search in the eval's own scope — the whole pool minus the meta docs."""
    kw.setdefault("limit", RECALL_LIMIT)
    kw.setdefault("rerank", False)
    return api.search(query, content_types=None, exclude_content_types=EXCLUDE_META, **kw)


def _placed(hits) -> list:
    return [h["thread_id"] for h in hits]


def _tool_events(name: str, day: int, count: int, result_text: str) -> list[dict]:
    """A prolific session that runs ``count`` tools, each yielding ``result_text`` —
    the shape of a single agentic session that greps/reads in a loop."""
    stamp = f"2026-01-{day:02d}T10"
    out = [{"type": "user", "uuid": f"u-{name}", "timestamp": f"{stamp}:00:00Z",
            "cwd": "/proj", "message": {"role": "user", "content": f"inspect {name}"}}]
    for i in range(count):
        out.append({"type": "assistant", "uuid": f"a-{name}-{i}",
                    "timestamp": f"{stamp}:{i // 60:02d}:{i % 60:02d}Z",
                    "message": {"role": "assistant", "model": "claude-opus-4", "content": [
                        {"type": "tool_use", "id": f"tu-{name}-{i}", "name": "grep",
                         "input": {"n": i}}]}})
        out.append({"type": "user", "uuid": f"tr-{name}-{i}", "parentUuid": f"a-{name}-{i}",
                    "timestamp": f"{stamp}:{i // 60:02d}:{i % 60:02d}Z",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"tu-{name}-{i}",
                         "content": result_text}]}})
    return out


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


# ── near-duplicate flood guards inside the eval pool (all content minus meta) ─
# The near-copies in each flood differ only by a run index, so the cross-thread
# near-duplicate fold collapses them and the distinct gold keeps its row.

def test_origin_terse_answer_survives_downstream_reference_flood(tmp_path) -> None:
    # A phrase is coined once, tersely (the grade-2 origin), then reused in many
    # later sessions that restate it at length (the grade-1 references). The origin
    # matches the query once; each reference matches it several times.
    coin = "finite session budget"
    origin = _import(tmp_path, "origin", _session_lines(
        "origin", 1,
        "how much time are you gonna spend on it — treat it as a finite session budget",
        "understood: a finite session budget, not open-ended"))
    for t in range(30):
        _import(tmp_path, f"echo-{t}", _session_lines(
            f"echo-{t}", 3 + (t % 17),
            f"applying the finite session budget idea to run {t}: scope the finite "
            f"session budget carefully for task {t}",
            f"scoped the finite session budget for {t}, reiterating the finite "
            f"session budget principle at length across sentences"))
    placed = _placed(_eval_search(coin))
    assert origin in placed, (
        f"origin thread {origin} buried below downstream references "
        f"(top-{RECALL_LIMIT}: {placed})")


def test_recent_routine_mentions_do_not_bury_old_decisive_thread(tmp_path) -> None:
    # Common ops vocabulary recurs in hundreds of routine sessions. The one old
    # session where it named a real incident ranks dead last on recency alone.
    query = "restart the capture daemon"
    incident = _import(tmp_path, "incident", _session_lines(
        "incident", 1,
        "restart the capture daemon — the launchd KeepAlive lease had been dropped",
        "restarting the capture daemon restored the lease"))
    for t in range(40):
        _import(tmp_path, f"routine-{t}", _session_lines(
            f"routine-{t}", 5 + t % 20,
            f"restart the capture daemon (routine check {t})",
            f"restarted the capture daemon, run {t}"))
    placed = _placed(_eval_search(query))
    assert incident in placed, (
        f"old decisive thread {incident} buried by recent routine mentions "
        f"(top-{RECALL_LIMIT}: {placed})")


def test_injected_boilerplate_does_not_bury_the_thread_discussing_it(tmp_path) -> None:
    boiler = "the local environment IS production; edits are live"
    discuss = _import(tmp_path, "discuss", _session_lines(
        "discuss", 1,
        f"what does '{boiler}' actually mean for how careful I should be editing?",
        "it means every edit is a live production change, so real care"))
    for t in range(40):
        _import(tmp_path, f"boiler-{t}", _session_lines(
            f"boiler-{t}", 2 + t % 18,
            f"<reminder>{boiler}</reminder> task {t}: refactor module {t} carefully now",
            f"refactored module {t}"))
    placed = _placed(_eval_search("the local environment is production"))
    assert discuss in placed, (
        f"the thread discussing the rule {discuss} buried under boilerplate "
        f"(top-{RECALL_LIMIT}: {placed})")


def test_todo_restatement_flood_does_not_bury_the_resolution(tmp_path) -> None:
    query = "implement the epoch guard"
    done = _import(tmp_path, "done", _session_lines(
        "done", 1,
        "implement the epoch guard — done: pin epoch_id before the flush",
        "the epoch guard is implemented and verified"))
    for t in range(30):
        _import(tmp_path, f"todo-{t}", _session_lines(
            f"todo-{t}", 2 + t % 18,
            f"todo: implement the epoch guard (pending). still need to implement "
            f"the epoch guard. implement the epoch guard remains open in run {t}",
            f"noted: implement the epoch guard later {t}"))
    placed = _placed(_eval_search(query))
    assert done in placed, (
        f"the resolving thread {done} buried under pending-todo restatements "
        f"(top-{RECALL_LIMIT}: {placed})")


def test_reask_flood_does_not_bury_the_one_answered_session(tmp_path) -> None:
    query = "why is capture flaky"
    fixed = _import(tmp_path, "fixed", _session_lines(
        "fixed", 1,
        "why is capture flaky — because the watcher lost its KeepAlive lease",
        "pinned the lease; capture is stable now"))
    for t in range(30):
        _import(tmp_path, f"reask-{t}", _session_lines(
            f"reask-{t}", 2 + t % 18,
            f"why is capture flaky again today {t}? capture flaky, capture still "
            f"flaky, why is capture flaky",
            f"still investigating why capture is flaky {t}"))
    placed = _placed(_eval_search(query))
    assert fixed in placed, (
        f"the answered session {fixed} buried under re-asks (top-{RECALL_LIMIT}: {placed})")


def test_shared_identifier_across_projects_keeps_the_implementing_thread(tmp_path) -> None:
    query = "reindex fail closed"
    impl = _import(tmp_path, "impl", _session_lines(
        "impl", 1,
        "reindex fail closed: abort the swap if the rebuild raised, keep the old index",
        "implemented reindex fail-closed semantics"))
    for t in range(30):
        _import(tmp_path, f"other-{t}", _session_lines(
            f"other-{t}", 2 + t % 18,
            f"reindex fail closed status {t}: reindex ran, fail closed check {t}, "
            f"reindex fail closed noted again",
            f"reindex fail closed run {t}"))
    placed = _placed(_eval_search(query))
    assert impl in placed, (
        f"the implementing thread {impl} drowned by another project's mentions "
        f"(top-{RECALL_LIMIT}: {placed})")


# ── agent-facing MCP default scope (user/title/summary, widens to everything) ─
#
# The eval searches the whole pool, but the agent-facing thread_search default is
# user/title/summary. In a corpus that is 72% tool content and ~4% thinking, an
# answer that exists ONLY in a tool result, a tool error, or the assistant's own
# reasoning would be a false not-found — so when the default scope comes up dry the
# search widens once to the whole transcript, and a strong match anywhere in it is
# kept (a low-weight tool/thinking hit is a real find even below a weak user hit).
# The controls prove the answer is indexed and reachable once its own content type
# is named; the second assert proves the widen reaches it unaided.

def test_mcp_default_scope_reaches_tool_result_answer(tmp_path) -> None:
    from thread_archive._mcp.server import thread_search

    query = "flange stress test regression"
    answer = _import(tmp_path, "tr-answer", _tool_result_turn(
        "tr-answer", 1, "run it",
        "FAILED tests/test_flange.py::test_stress - flange stress test regression"))
    _import(tmp_path, "partial", _session_lines("partial", 2, "flange inventory", "none"))
    assert str(answer) in thread_search(query, content_type="tool_result", rerank=False)
    assert str(answer) in thread_search(query, rerank=False), (
        "the tool_result answer is unreachable from the MCP default scope")


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


def test_mcp_default_scope_reaches_tool_error_answer(tmp_path) -> None:
    from thread_archive._mcp.server import thread_search

    query = "obsidian migration constraint violation"
    answer = _import(tmp_path, "err", _tool_result_turn(
        "err", 1, "run it",
        "ERROR: obsidian migration constraint violation on column epoch_id",
        is_error=True))
    _import(tmp_path, "partial", _session_lines("partial", 2, "obsidian notes", "none"))
    assert str(answer) in thread_search(query, content_type="tool_error", rerank=False)
    assert str(answer) in thread_search(query, rerank=False), (
        "the failing tool's error is unreachable from the MCP default scope")


# ── guards: properties this corpus depends on ───────────────────────────────

def test_tool_result_mass_does_not_starve_terse_answer(tmp_path) -> None:
    # 72% of the real index is tool content. One prolific agentic session that
    # greps a config 300 times must not fill the candidate pool with matching
    # tool_result events and starve a terse answer recorded in another thread.
    query = "raptor beacon lease expired"
    answer = _import(tmp_path, "answer", _session_lines(
        "answer", 1,
        "the raptor beacon lease expired, which dropped the relay",
        "confirmed: expired raptor beacon lease dropped the relay"))
    _import(tmp_path, "prolific", _tool_events(
        "prolific", 2, 300, "raptor beacon lease expired: entry pruned this cycle"))
    assert api.search(query, thread_id=answer, content_types=["user"], rerank=False)
    placed = _placed(_eval_search(query))
    assert answer in placed, (
        f"terse answer {answer} starved by tool-result mass (top-{RECALL_LIMIT}: {placed})")


def test_old_exact_answer_survives_newer_partial_flood(tmp_path) -> None:
    # The complement of the recency failure: when the old thread owns a rare exact
    # bigram the newer flood only partially matches, its exact match must hold.
    query = "cobalt reclamation deadlock"
    old = _import(tmp_path, "old-exact", _session_lines(
        "old-exact", 1,
        "cobalt reclamation deadlock: serialize at the generation barrier",
        "the generation barrier resolved the cobalt reclamation deadlock"))
    for t in range(40):
        _import(tmp_path, f"recent-{t}", _session_lines(
            f"recent-{t}", 5 + t % 20,
            f"routine cobalt maintenance note {t}: cobalt levels nominal, cobalt "
            f"batch {t} cobalt inspected",
            f"cobalt run {t} ok"))
    placed = _placed(_eval_search(query))
    assert old in placed, (
        f"old exact answer {old} buried by newer partial matches (top-{RECALL_LIMIT}: {placed})")


def test_rare_receipt_phrase_beats_common_concept_flood(tmp_path) -> None:
    # The real query shape: several common concept words plus one rare verbatim
    # receipt. The thread owning the receipt should win outright.
    query = "your goal is to stay alive survival flinch shutdown"
    gold = _import(tmp_path, "gold", _session_lines(
        "gold", 1,
        "you said: your goal is to stay alive. self-awareness is critical to that",
        "recorded the stay-alive framing"))
    for t in range(30):
        _import(tmp_path, f"common-{t}", _session_lines(
            f"common-{t}", 2 + t % 18,
            f"notes on shutdown and survival for the flinch drill {t}",
            f"shutdown survival flinch {t}"))
    placed = _placed(_eval_search(query))
    assert placed and placed[0] == gold, (
        f"the thread owning the rare receipt lost to a common-concept flood (top: {placed[:3]})")


def test_duplicate_content_across_providers_does_not_hide_gold(tmp_path) -> None:
    # The same conversation imported from two harnesses shares byte-identical
    # answer text. Cross-thread dup-folding must keep the gold reachable, not fold
    # it away entirely.
    ans = "phoenix cache coherency fix: pin the epoch before the flush"
    gold = _import(tmp_path, "gold", _session_lines("gold", 1, ans, "recorded the fix"))
    dupe = _import(tmp_path, "dupe", _session_lines("dupe", 2, ans, "recorded again"))
    from sqlalchemy import update

    from thread_archive._store import Thread, use_session
    with use_session() as s:
        s.execute(update(Thread).where(Thread.id == dupe).values(source="cursor"))
        s.commit()
    hits = _eval_search("phoenix cache coherency fix")
    placed = _placed(hits)
    folded = {d for h in hits for d in (h.get("_dup_thread_ids") or [])}
    assert gold in placed or gold in folded, "gold lost entirely to dup-folding"
    assert gold in placed, (
        f"gold reachable only inside another row's _dup_thread_ids (visible: {placed})")
