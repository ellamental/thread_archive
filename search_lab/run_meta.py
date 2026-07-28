"""What a recorded measurement was taken *of* — the code and the configuration.

Every ledger in the lab (``bench-runs.jsonl``, ``latency-runs.jsonl``,
``bench-runs.jsonl``) stamps its rows with the same two facts, and they belong in
one place: a number whose commit and ``SearchParams`` are recorded is auditable,
and one whose aren't is a value that existed only in the moment it printed.

Both are best-effort. A ledger stamp must never be the thing that breaks the run
it is recording, so an absent git checkout records no commit rather than raising.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional


def _repo_root() -> Path:
    """The archive repo root — this file's directory (``search_lab/``) is its
    child, so one level up.

    Derived from ``__file__`` rather than the cwd because a run is launched from
    anywhere, and it must name *this* checkout: archive is a nested repo, so
    walking up from a wrong starting point finds a different repository's HEAD and
    stamps the ledger with a commit that has nothing to do with the code that was
    measured."""
    return Path(__file__).resolve().parents[1]


def git_commit() -> Optional[str]:
    """The short SHA of the code being measured, or ``None`` outside a git
    checkout. Best-effort: a detached/absent repo records no commit rather than
    raising.

    ``rev-parse --show-toplevel`` first: ``git -C <dir>`` walks *up* until it finds
    a repository, so a checkout root that is somehow not one would silently answer
    with an ancestor repo's HEAD. Confirming the toplevel is the checkout makes
    that read as "no commit" instead of a wrong one — the ledger's whole value is
    that a recorded number names the code that produced it."""
    root = _repo_root()
    try:
        def _git(*argv: str) -> Optional[str]:
            out = subprocess.run(
                ["git", "-C", str(root), *argv],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return out.stdout.strip() or None

        toplevel = _git("rev-parse", "--show-toplevel")
        if toplevel is None or Path(toplevel).resolve() != root:
            return None
        return _git("rev-parse", "--short", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None


def active_config(params: Any = None) -> dict[str, Any]:
    """The retrieval configuration a run measures under: the effective
    ``SearchParams`` fields plus the model-arm / coherence env switches that
    materially move the numbers. This is the "which config produced these numbers"
    half of a recorded measurement — the field that turns a drift into an
    explained one. ``params`` defaults to the shipped values; a tuning run passes
    the configuration it actually measured, so the ledger row and its numbers can
    never describe different rankings."""
    from thread_archive._retrieval import SearchParams

    params = asdict(params if params is not None else SearchParams())
    # content_type_weights is a mapping-or-None; asdict keeps it JSON-safe already.
    env = os.environ.get
    config: dict[str, Any] = {"params": params}
    config["embed"] = "off" if env("THREAD_ARCHIVE_EMBED", "").strip().lower() in (
        "0", "false", "no", "off") else "on"
    coherence = env("THREAD_ARCHIVE_COHERENCE")
    if coherence is not None:
        config["coherence"] = coherence
    return config
