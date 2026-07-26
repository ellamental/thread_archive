"""The gold-gate run ledger: ``<home>/gold-runs.jsonl``.

The retrieval-quality gold gate (``scripts/retrieval_gold_gate.py``) scores the
snapshot-bound gold files on every commit and checks each against a floor. The
floor answers *did search break*; it throws the actual numbers away once printed.
This ledger keeps them: every gate run appends one record — per gold file's
MRR / success@10 / recall@10 / nDCG@10 and p50 latency, the active
:class:`~thread_archive._retrieval.params.SearchParams`, the model-arm switches,
the corpus ``snapshot_id``, and the code commit — so the baseline is a recorded
**timeseries**, not a value that existed only in the moment the gate printed it.

That is what makes a defaults change auditable: "baseline was 0.46 MRR on commit
X, 0.44 on Y, and the config changed here" is a lookup against this file, not a
re-run of the old configuration. When a change moves the numbers, the before is
already on disk under the params that produced it.

Append-only JSONL, advisory, fail-soft — a ledger write must never break the gate
it records. ``THREAD_ARCHIVE_GOLD_RUNS_LOG=0`` disables it. Lives at the archive
home root beside ``retrieval-usage.jsonl``, outside ``truth/`` — operational
telemetry, not archive data; it records ids and metrics, never case content.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LEDGER_FILE = "gold-runs.jsonl"
BASELINE_FILE = "gold-baseline.json"


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_GOLD_RUNS_LOG", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _repo_root() -> Path:
    """The archive repo root (src/thread_archive/_ops/gold_runs.py → up 3)."""
    return Path(__file__).resolve().parents[3]


def git_commit() -> Optional[str]:
    """The short SHA of the code being gated, or ``None`` outside a git checkout.
    Best-effort: a detached/absent repo records no commit rather than raising."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_repo_root()), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = out.stdout.strip()
        return sha or None
    except (OSError, subprocess.SubprocessError):
        return None


def active_config(params: Any = None) -> dict[str, Any]:
    """The retrieval configuration a gate run scores under: the effective
    ``SearchParams`` fields plus the model-arm / coherence env switches that
    materially move the numbers. This is the "which config produced these scores"
    half of a recorded baseline — the field that turns a metric drift into an
    explained one. ``params`` defaults to the shipped values; a tuning run passes
    the configuration it actually scored, so the ledger row and its numbers can
    never describe different rankings."""
    from thread_archive._retrieval import SearchParams

    params = asdict(params if params is not None else SearchParams())
    # content_type_weights is a mapping-or-None; asdict keeps it JSON-safe already.
    env = os.environ.get
    config: dict[str, Any] = {"params": params}
    config["rerank"] = "off" if env("THREAD_ARCHIVE_RERANK", "").strip().lower() in (
        "0", "false", "no", "off") else "on"
    config["embed"] = "off" if env("THREAD_ARCHIVE_EMBED", "").strip().lower() in (
        "0", "false", "no", "off") else "on"
    coherence = env("THREAD_ARCHIVE_COHERENCE")
    if coherence is not None:
        config["coherence"] = coherence
    return config


def record_run(
    home: Path,
    *,
    snapshot_id: Optional[str],
    files: dict[str, dict[str, Any]],
    passed: bool,
    config: Optional[dict[str, Any]] = None,
    commit: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
    pool_cache: bool = False,
) -> None:
    """Append one gold-gate run to ``<home>/gold-runs.jsonl``. ``files`` maps each
    scored gold file to its metrics (``mrr``/``success10``/``recall10``/``ndcg10``/
    ``p50_ms``/``n``/``status``); ``passed`` is whether every floor held;
    ``overrides`` names the ``SearchParams`` fields a tuning run changed, marking
    the row as an experiment rather than a baseline. ``home`` is passed explicitly
    — the gate repoints ``THREAD_ARCHIVE_HOME`` at the frozen snapshot to score,
    so the ledger location can't be read back off the env. Fail-soft: any write
    error is logged and swallowed.

    ``pool_cache`` marks a run that scored from persisted candidate pools. Its
    scores are the point of such a run and are unaffected; its ``p50_ms`` is not a
    retrieval latency at all, because the arms never ran — measured on this corpus,
    a cached run's median lands near 60 ms against ~900 ms for the same files
    uncached. Unflagged, the two sit in one column and the timeseries reads as a
    tenfold speedup no code change caused."""
    if not _enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "gold-run",
        "snapshot_id": snapshot_id,
        "commit": commit if commit is not None else git_commit(),
        "config": config if config is not None else active_config(),
        "passed": passed,
        "files": files,
    }
    if overrides:
        # A tuning run: the numbers describe a candidate configuration, not the
        # shipped one. Flagged so reading the timeseries can't mistake an
        # experiment for a baseline movement.
        record["overrides"] = overrides
    if pool_cache:
        # Same reason, for the latency column: a pooled run's p50 measures the
        # cache, not the pipeline.
        record["pool_cache"] = True
    try:
        path = home / LEDGER_FILE
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record gold-gate run", exc_info=True)


def write_baseline(
    home: Path, *, snapshot_id: Optional[str], files: dict[str, dict[str, float]],
) -> None:
    """Overwrite ``<home>/gold-baseline.json`` with the shipped configuration's
    **per-case** reciprocal ranks: ``{gold file: {query: rr}}``.

    The ledger is history and grows; this is a single current reference and does
    not. Its job is to make a gate run fail fast and explain itself: knowing what
    each case scored at the baseline lets the gate order cases best-first (so a
    regression shows in the first few searches instead of the last few) and name
    the cases that changed instead of only reporting that a mean moved.

    Keyed by query text, not file position — a re-mine that reorders or replaces
    cases leaves the surviving queries comparable and simply has no baseline for
    the new ones. Written only by a full, unmodified run: a run under overridden
    params, a case subset, or an early abort describes a different ranking or a
    prefix of the cases, and either would poison the reference for every run after.
    """
    if not _enabled():
        return
    blob = {
        "at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id,
        "commit": git_commit(),
        "files": files,
    }
    try:
        path = home / BASELINE_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("could not write gold baseline", exc_info=True)


def read_baseline(
    home: Path, *, snapshot_id: Optional[str] = None,
) -> dict[str, dict[str, float]]:
    """The recorded per-case baseline as ``{gold file: {query: rr}}``, or empty.

    ``snapshot_id`` (when given) must match the one the baseline was written
    under: per-case scores from a different corpus describe different documents,
    so a mismatch reads as no baseline rather than as stale guidance. Missing or
    unreadable reads as empty — the gate then simply runs in file order without
    the fast-fail ordering, never fails."""
    path = home / BASELINE_FILE
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if snapshot_id is not None and blob.get("snapshot_id") != snapshot_id:
        return {}
    files = blob.get("files")
    return files if isinstance(files, dict) else {}


def read_runs(home: Path, *, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """The recorded gold-gate runs, newest first. ``limit`` caps the count. A
    missing or unreadable ledger reads as empty (no history yet), never an error."""
    path = home / LEDGER_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    runs: list[dict[str, Any]] = []
    for line in lines:
        if line.strip():
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    runs.reverse()
    return runs[:limit] if limit is not None else runs
