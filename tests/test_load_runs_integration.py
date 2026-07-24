"""The load telemetry wired into the real paths: the embed drain reports its
select/encode/write split into a phase, and a tracked import advances per file."""

from __future__ import annotations

import json

from thread_archive import _api as api
from thread_archive._ops import load_runs
from thread_archive._retrieval import vectors
from thread_archive._store import init_db


class _FixedEmbedder:
    """Front-door stand-in for the torch model — one fixed 768-d vector, no load."""

    def __init__(self) -> None:
        self.batches: list[int] = []

    def is_available(self) -> bool:
        return True

    def space_key(self) -> str:
        return "local:test"

    def embed_documents(self, texts):
        self.batches.append(len(texts))
        return [[1.0] + [0.0] * 767 for _ in texts]


def _seed(home):
    """Import a couple of real events so the FTS shadow the drain reads is populated."""
    init_db()
    lines = [
        {"type": "user", "uuid": "u0", "timestamp": "2026-01-01T10:00:00Z", "cwd": "/p",
         "message": {"role": "user", "content": "how do I fix the failing test"}},
        {"type": "assistant", "uuid": "a0", "timestamp": "2026-01-01T10:00:30Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "run the suite and read the traceback"}]}},
    ]
    f = home / "corpus.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    api.import_path(f)


def test_drain_reports_the_select_encode_write_split(archive_home) -> None:
    _seed(archive_home)
    run = load_runs.LoadRun("embed", archive_home)
    with run.phase("embed") as ph:
        n = vectors.index_events_local(embedder=_FixedEmbedder(), phase=ph)
    assert n > 0
    snap = ph.snapshot()
    # The three sub-timings the split exists to expose are all present…
    assert set(snap["detail_s"]) == {"select", "encode", "write"}
    # …the pending total was set up front, and progress reached it…
    assert snap["total"] == n and snap["done"] == n
    # …and the chunk accounting rode along.
    assert snap["counts"]["chunks"] >= n


def test_embed_api_writes_a_ledger_row(archive_home) -> None:
    _seed(archive_home)
    res = api.embed(embedder=_FixedEmbedder())
    assert res["embedded"] > 0
    row = load_runs.read_runs(home=archive_home)[0]
    assert row["kind"] == "embed" and row["status"] == "ok"
    assert row["phases"][0]["detail_s"].keys() >= {"select", "encode", "write"}


def test_untracked_drain_still_runs_identically(archive_home) -> None:
    # No phase passed → a NullPhase → the drain does the same work, records nothing.
    _seed(archive_home)
    n = vectors.index_events_local(embedder=_FixedEmbedder())  # no phase=
    assert n > 0
    assert load_runs.read_runs(home=archive_home) == []  # nothing tracked


def test_chunk_count_matches_the_chunker():
    # The Python chunk-count that seeds the phase total must equal len(_chunk()).
    for text in ["", "x", "a" * 2049, "b" * (2048 * 9)]:
        if not text:
            continue
        assert vectors._chunk_count(text) == len(vectors._chunk(text))
