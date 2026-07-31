"""What each retrieval stage must leave on a hit for the next one to work.

The pipeline is stages over one mutable record: ``fts.build_event_hit``
constructs an :class:`~thread_archive._retrieval._types.EventHit`, fusion
annotates it, the ranker scores off those annotations, enrichment fills the
display fields, and the renderer reads the result. Every stage writes into the
same dict.

That design is fine, and it is fast. What it has no defence against is a stage
quietly stopping: **every annotation is read with a zero default**
(``result.get("_rrf", 0.0)``, ``.get("_lex", 0.0)``, ``.get("_bm25", 0.0)`` in
:mod:`~thread_archive._retrieval.rank`), so an arm that stops annotating does not
raise, does not log, and does not fail a test that only checks results come back
— it flattens that term of the score to zero for every hit and the search keeps
answering, worse. The renderer is the same shape: it reads display fields with
``or`` fallbacks, so a missing title degrades to ``thread <id>``.

A type checker cannot reach any of this. Splitting the TypedDict per stage looks
like it would, but the stages mutate in place, so every seam would need a
``cast`` — annotations that read as enforced while resting on unchecked
assertions. This is the check that actually holds: drive the real pipeline and
assert what each stage left behind.

The suite is pinned model-free, so the vector arm sits out here — which is worth
asserting in its own right (a hit claiming a semantic score nothing computed
would be scored on it). Fusion is covered through its own pure function, so the
one stage a lexical-only archive never reaches is still pinned.
"""

from __future__ import annotations

import json

import pytest

from thread_archive import _api as ta
from thread_archive._retrieval import _rrf_merge

#: What ``build_event_hit`` constructs and every later stage assumes. Not
#: optional anywhere: the renderer indexes several of these directly (``h[
#: 'thread_id']``, ``h['event_type']``), so an absent one is an exception in the
#: middle of an answer rather than a degraded score.
BASE_KEYS = {
    "event_id", "thread_id", "thread_title", "event_type", "content_type",
    "snippet", "full_content", "occurred_at",
}

SESSION = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "cwd": "/proj",
     "message": {"role": "user", "content": "the retry backoff in rank.py needs work"}},
    {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
     "message": {"role": "assistant", "model": "claude-opus-4", "content": [
         {"type": "text", "text": "agreed, the retry backoff is wrong"},
         {"type": "tool_use", "id": "t1", "name": "Edit",
          "input": {"file_path": "/proj/rank.py"}}]}},
]


@pytest.fixture
def seeded(archive_home, tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in SESSION) + "\n", encoding="utf-8")
    ta.import_path(f)
    return archive_home


def test_every_hit_carries_the_shape_every_stage_assumes(seeded) -> None:
    hits = ta.search("retry backoff")
    assert hits
    for h in hits:
        assert BASE_KEYS <= set(h), f"missing {sorted(BASE_KEYS - set(h))}"


def test_the_lexical_arm_leaves_the_scores_the_ranker_weights(seeded) -> None:
    """``_lex`` and ``_bm25`` are two of the ranker's six terms and both are read
    with a zero default, so an arm that stopped setting them would go on
    returning results — ranked as though every hit matched the query equally
    badly."""
    hits = ta.search("retry backoff")
    assert hits
    for h in hits:
        assert "_lex" in h, "the lexical arm's normalized rank is missing"
        assert "_bm25" in h, "the lexical arm's own bm25 score is missing"
        assert 0.0 <= h["_lex"] <= 1.0, h["_lex"]
        assert 0.0 <= h["_bm25"] <= 1.0, "bm25 is peak-normalized over the pool"
    # Peak-normalized: the best hit in the pool anchors the scale, so a scale
    # that drifted off 1.0 would silently rescale the whole term.
    assert max(h["_bm25"] for h in hits) == pytest.approx(1.0)


def test_no_hit_claims_a_score_no_arm_computed(seeded) -> None:
    """With the vector arm sitting out (this suite runs model-free), a hit
    carrying ``_semantic`` would be scored on a similarity nothing measured —
    and ``_rrf`` is a *fusion* score, so with one arm there is nothing to fuse
    and its absence is the honest answer rather than a dropped annotation."""
    hits = ta.search("retry backoff")
    assert hits
    for h in hits:
        assert "_semantic" not in h, "a lexical-only result claims a vector score"
        assert "_rrf" not in h, "a single-arm result claims a fusion score"


def test_fusion_annotates_every_hit_it_returns(seeded) -> None:
    """The stage a lexical-only archive never reaches, pinned through its own
    function. ``_rrf`` is the ranker's ``fusion_weight`` term; a hit that came
    out of fusion without one would be weighted as though the two arms
    disagreed about it completely."""
    lexical = ta.search("retry backoff")
    assert lexical
    # A second ranked list standing in for the vector arm: same events, reversed,
    # which is the case fusion exists for — two arms that agree on the set and
    # disagree on the order.
    vector = [dict(h, _semantic=0.5) for h in reversed(lexical)]

    fused = _rrf_merge([list(lexical), vector], limit=10)

    assert fused
    for h in fused:
        assert "_rrf" in h, "fusion returned a hit with no fusion score"
        assert 0.0 <= h["_rrf"] <= 1.0, "normalized to [0,1] — the weight is calibrated to that"
        assert BASE_KEYS <= set(h), "fusion dropped a field the renderer indexes"
    assert max(h["_rrf"] for h in fused) == pytest.approx(1.0), "peak-normalized"
    # The arm provenance survives fusion even when the other arm's copy is the
    # one kept, or the ranker loses the vector magnitude term for that hit.
    assert all("_semantic" in h for h in fused)


def test_enrichment_fills_the_display_fields_for_every_hit(seeded) -> None:
    """The renderer reads these with ``or`` fallbacks, so a hit enrichment missed
    renders as ``thread <id>`` with no provider — a plausible-looking row that
    quietly lost its provenance."""
    hits = ta.search("retry backoff")
    assert hits
    for h in hits:
        assert h["thread_title"], "a hit reached the renderer with no thread title"
        assert h["thread_source"] == "claude-code"


def test_a_browse_row_carries_what_the_list_renderer_branches_on(seeded) -> None:
    """``format_results`` picks the browse layout off ``_browse`` and prints the
    two fields only a browse row has. A row missing them renders through the hit
    layout instead — the same data, described as something else."""
    rows = ta.search("")
    assert rows
    for r in rows:
        assert r["_browse"] is True
        assert r["thread_source"] and r["n_events"] > 0
        assert BASE_KEYS <= set(r)


def test_a_code_axis_browse_carries_the_columns_that_make_it_one(seeded) -> None:
    """The renderer decides a result *is* the code axis by ``_path_ops`` being
    present on the first row, then reads the rest of the ``_path_*`` set off
    every row. Half a set renders a file answer with no file in it."""
    ta.code_index()
    rows = ta.search("", path="rank.py")
    assert rows, "the seeded session edited rank.py"
    for r in rows:
        assert r["_path_ops"], "the op tally is what marks this as the code axis"
        assert set(r["_path_ops"]) <= {"edit", "write", "delete", "read", "search", "run"}
        assert r["_path_first"] and r["_path_last"]
        assert r["_path_files"] >= 1 and r["_path_sample"]
        # Re-pointed at the touch rather than the thread's tail: the whole reason
        # a code-axis row opens where the work happened.
        assert r["event_id"] > 0


def test_a_page_of_results_can_describe_itself(seeded) -> None:
    """``Results`` is what makes a cut readable as a page rather than an answer.
    Every field here is read by the header, and a missing one reads as a search
    that found exactly what it returned."""
    hits = ta.search("retry backoff")
    assert hits.total is not None and hits.total >= len(hits)
    assert hits.page == 1 and hits.pages is not None
    assert hits.exhaustive is True, "a pool that came back short reaches every match"
