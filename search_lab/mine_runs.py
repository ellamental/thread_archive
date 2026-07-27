"""The gold-mining run ledger: ``<gold dir>/mine-runs.jsonl``.

Every ``python -m search_lab.mine <miner>`` run mints gold cases, but what gives
those cases meaning is the *denominator* — how many sampled units yielded no
usable query at all. Score only the cases that *were* minted and the population
is silently conditioned on "the agent succeeded," which inflates the apparent
quality: a unit dropped because no fair query could be written for it is a unit
the benchmark never asks search about.

This ledger keeps the whole denominator: one append per run recording the miner,
the corpus ``snapshot_id``, the code commit, how many units were attempted, how
many cases were written, and the full outcome breakdown (``ok`` / ``no-queries``
/ ``unparseable`` / ``agent-failed`` / …). The drop *rates* are then a lookup
over time, not a number that existed only in the moment the run printed it; a
floor on those rates can be calibrated from this timeseries once it has one.

Append-only JSONL, advisory, fail-soft — a ledger write must never break the
mining run it records. ``THREAD_ARCHIVE_MINE_RUNS_LOG=0`` disables it.

The ledger lands **in the directory the run wrote its cases into**, which is what
keeps a corpus whole: golds mined from the SWE-chat corpus live beside that
download, and their denominators belong there too, not in the archive's private
gold dir where they would describe cases that are not there. Reads default to the
archive gold dir; pass ``home`` to read another corpus's. Either way the ledger
sits outside ``truth/`` — operational telemetry, not archive data; it records ids,
counts, and outcomes, never case content.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LEDGER_FILE = "mine-runs.jsonl"


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_MINE_RUNS_LOG", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _gold_dir() -> Path:
    """The archive's own gold dir — the fallback when a caller names no directory.
    A miner writing elsewhere passes ``home`` so the run record travels beside the
    golds it produced."""
    from .mine._framework import gold_dir

    return gold_dir()


def record_run(
    *,
    miner: str,
    snapshot_id: Optional[str],
    attempted: int,
    written: int,
    failed: int,
    outcomes: dict[str, int],
    home: Optional[Path] = None,
    funnel: Optional[list[dict[str, Any]]] = None,
    cost_usd: Optional[float] = None,
) -> None:
    """Append one mining run to ``<gold dir>/mine-runs.jsonl``. ``attempted`` is the
    number of units the run drew (queries judged, threads sampled); ``written`` the
    cases minted; ``failed`` the units that yielded none; ``outcomes`` the full
    per-unit disposition breakdown (its keys are miner-defined, e.g. ``ok`` /
    ``none-of-pool`` / ``no-grade-2`` / ``agent-failed`` / ``unparseable`` /
    ``empty-pool`` / ``no-queries``). ``home`` is the directory the ledger lands
    in — the miner passes the one it wrote its cases into, so a corpus keeps its
    own denominators. Fail-soft: any write error is logged and swallowed so
    telemetry can't break a mining run."""
    if not _enabled():
        return
    from run_meta import git_commit

    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "mine-run",
        "miner": miner,
        "snapshot_id": snapshot_id,
        "commit": git_commit(),
        "attempted": attempted,
        "written": written,
        "failed": failed,
        "outcomes": outcomes,
    }
    # The funnel is where a unit died, stage by stage; ``outcomes`` above is only
    # its terminal slice. Optional so a run by a miner that declares no stages
    # records exactly what it used to, and a reader must treat its absence as "not
    # recorded" rather than "nothing was dropped".
    if funnel:
        record["funnel"] = funnel
    if cost_usd:
        record["cost_usd"] = round(cost_usd, 4)
    try:
        path = (home or _gold_dir()) / LEDGER_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record mining run", exc_info=True)


def read_runs(home: Optional[Path] = None, *,
              limit: Optional[int] = None) -> list[dict[str, Any]]:
    """The recorded mining runs, newest first. ``limit`` caps the count. A missing
    or unreadable ledger reads as empty (no history yet), never an error."""
    path = (home or _gold_dir()) / LEDGER_FILE
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
