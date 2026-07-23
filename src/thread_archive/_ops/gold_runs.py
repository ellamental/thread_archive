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


def active_config() -> dict[str, Any]:
    """The retrieval configuration a gate run scores under: the shipped
    ``SearchParams`` fields plus the model-arm / coherence env switches that
    materially move the numbers. This is the "which config produced these scores"
    half of a recorded baseline — the field that turns a metric drift into an
    explained one."""
    from .._retrieval import SearchParams

    params = asdict(SearchParams())
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
) -> None:
    """Append one gold-gate run to ``<home>/gold-runs.jsonl``. ``files`` maps each
    scored gold file to its metrics (``mrr``/``success10``/``recall10``/``ndcg10``/
    ``p50_ms``/``n``/``status``); ``passed`` is whether every floor held. ``home``
    is passed explicitly — the gate repoints ``THREAD_ARCHIVE_HOME`` at the frozen
    snapshot to score, so the ledger location can't be read back off the env.
    Fail-soft: any write error is logged and swallowed."""
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
    try:
        path = home / LEDGER_FILE
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record gold-gate run", exc_info=True)


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
