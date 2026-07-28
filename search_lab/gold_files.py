"""What counts as a gold case file — the one place that decides.

The gold corpus is *discovered*, never listed: it accumulates a file at a time, and
anything that had to be edited to notice a new file would fall behind the corpus it
describes. That makes the discovery rule load-bearing, and it belongs in exactly one
place — three readers with three slightly different globs is three chances to
disagree about what a fixture is.

A gold file is a ``*cases*.jsonl`` under the gold dir that is not one of the
sidecars that sit beside one: per-case detail dumps, **refusal records**, seed and
candidate pools, accepted-set snapshots, and ``.until-bak`` rewrites. Detail
sidecars exist under two spellings (``X-detail.jsonl`` and ``X.detail.jsonl``), so
the rule matches the marker anywhere in the name rather than as a suffix.

The refusals file is the one whose exclusion is load-bearing rather than tidy: it
sits beside the cases and shares their stem, and a scorer that picked it up would
be asking search to answer questions a gate already ruled unanswerable.
"""

from __future__ import annotations

from pathlib import Path

# Substrings that mark a sidecar rather than a case file. Matched
# anywhere in the basename: detail sidecars sit on disk under both `-detail` and
# `.detail`, and a suffix test silently lets one of them through.
NON_GOLD_MARKERS = ("detail", "rejects", "seed", "candidate", "accepted", "-bak")


def is_gold(path: Path) -> bool:
    """Whether one path is a gold case file rather than a sidecar."""
    name = path.name
    if not name.endswith(".jsonl") or "cases" not in name:
        return False
    return not any(marker in name for marker in NON_GOLD_MARKERS)


def discover(gold_dir: Path) -> list[Path]:
    """Every gold case file in ``gold_dir``, sorted. A missing dir is empty."""
    if not gold_dir.is_dir():
        return []
    return sorted(p for p in gold_dir.glob("*cases*.jsonl") if is_gold(p))
