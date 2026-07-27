"""BEIR retrieval calibration — the external yardstick, run as an opt-in test.

Answers the question the in-house harness can't: *are the retrieval components
embarrassing* against a public IR benchmark with human relevance judgments? Runs
``search_lab/beir_eval.py`` (download a BEIR dataset → ingest the corpus into a
throwaway archive → score the real ``api.search`` pipeline with nDCG@10 /
Recall@k / MRR@10) and asserts floors that only a genuine collapse trips.

This is the ``beir`` lane — deselected from the default run (see pyproject),
**no ci.toml row**: it downloads a corpus, ingests thousands of docs, and loads
torch, so it costs tens of minutes and needs the network. It is external
calibration you invoke when you touch ranking, not a per-commit gate — the
fast per-commit ``retrieval-gate`` row only probes that the model arms are
alive.

    .venv/bin/pytest -m beir                                    # both checks
    .venv/bin/pytest tests/test_beir_calibration.py::test_scifact_lexical_finds_golds

The floors sit well below values measured at calibration (scifact full stack:
nDCG@10 0.62, Recall@100 0.87; lexical Recall@100 0.71), so a breach means a
component genuinely broke — a dead embeddings arm, a lexical regression that
drops golds from the pool — not ordinary drift. A breach is investigate, not
revert; recalibrate the floors on a deliberate ranking redesign.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.beir

REPO = Path(__file__).resolve().parent.parent
EVAL = REPO / "search_lab" / "beir_eval.py"
# Persistent, HOME-independent cache (gitignored): the download AND the built
# archive/embeddings survive across runs, so a re-run of this lane — and the
# full-stack test reusing the lexical test's ingest — skips the expensive rebuild.
CACHE = REPO / ".beir-cache"


def _run_eval(tmp_path: Path, *args: str, timeout: int) -> dict:
    """Invoke the harness in a fresh process and return its JSON report.

    A subprocess, not an in-process call: the eval needs the real embedder /
    cross-encoder and a live archive home, which the suite's model-free,
    throwaway-home pins (conftest) deliberately forbid in-process. The script
    sets its own arm switches and home, so a clean child is the honest
    environment. ``--data-dir`` is the persistent cache, so a second run reuses
    the built index and vectors."""
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(EVAL), *args,
         "--data-dir", str(CACHE),
         "--json-out", str(out)],
        cwd=str(REPO), timeout=timeout,
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"beir_eval failed:\n{proc.stderr[-3000:]}"
    return json.loads(out.read_text())


def test_scifact_lexical_finds_golds(tmp_path) -> None:
    """The lexical arm alone must pull the gold docs into the pool (Recall@100),
    even where it orders the head worse than BM25 — the plumbing check. No torch,
    so this is the fast lane (~a few minutes, dominated by corpus ingest)."""
    r = _run_eval(tmp_path, "--dataset", "scifact", timeout=900)
    assert r["corpus_docs"] == 5183, "the full scifact corpus must ingest as distinct docs"
    assert r["recall"]["100"] >= 0.60, (
        f"lexical Recall@100 {r['recall']['100']:.3f} collapsed — golds are no "
        "longer reaching the candidate pool (a lexical/indexing regression)"
    )


def test_scifact_full_stack_is_bm25_competitive(tmp_path) -> None:
    """The full stack (lexical + vectors) must land nDCG@10 in the BM25 ballpark —
    the semantic arm recovering the head ordering the lexical floor gets wrong.
    Loads torch and embeds the whole corpus: the slow lane (~20 min on CPU)."""
    r = _run_eval(tmp_path, "--dataset", "scifact", "--vectors", timeout=3600)
    assert r["arms"] == ["lexical", "vectors"]
    assert r["ndcg10"] >= 0.50, (
        f"full-stack nDCG@10 {r['ndcg10']:.3f} fell far below the BM25 reference "
        f"({r['reference'].get('bm25')}) — the vector arm is "
        "likely dead, since the lexical floor alone scores ~0.31 here"
    )
    assert r["recall"]["100"] >= 0.78, (
        f"full-stack Recall@100 {r['recall']['100']:.3f} collapsed"
    )
