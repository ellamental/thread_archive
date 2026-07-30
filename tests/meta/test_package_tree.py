"""The package-tree ratchet: no operator data dumps in the wheel or in git.

Hatch packages everything under ``src/thread_archive``, so any file that lands
there ships to every installer. Repair undo records (``*_backup_*`` / ``*_plan_*``
dumps) are operator data for one host — real transcript and tool payloads, not
runtime code — so they belong under the archive home, beside the store they
describe: outside the package tree AND outside the checkout. A tracked dump
publishes private conversations to wherever the repo is hosted. This scan fails
the moment one lands inside ``src/`` or gets tracked anywhere.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "thread_archive"

# Filename fragments that mark an operator dump, wherever it sits.
DUMP_MARKERS = ("_backup_", "_plan_")


def _package_files() -> list[Path]:
    return [
        p for p in SRC.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]


def test_no_dump_files_in_package_tree():
    offenders = [
        p.relative_to(SRC)
        for p in _package_files()
        if any(marker in p.name for marker in DUMP_MARKERS)
    ]
    assert not offenders, (
        "operator dump files inside src/thread_archive (they would ship in the "
        f"wheel) — move them under the archive home: {sorted(map(str, offenders))}"
    )


def test_no_dump_files_tracked_in_git():
    """No operator dump is version-controlled anywhere in the repo.

    The wheel scan above catches dumps that would ship to installers; this one
    catches the other publication channel — git itself. No dump-marked filename
    may be tracked at any path: the checkout is never where operator data lands,
    so a dump appearing here at all means a script wrote somewhere it shouldn't.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
            check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout (or git unavailable)")
    tracked = out.splitlines()
    # Marker match applies to data files only: dumps are .json/.jsonl records,
    # while code legitimately names itself after the feature (test_backup_mirror.py).
    offenders = sorted(
        f for f in tracked
        if Path(f).suffix != ".py"
        and any(marker in Path(f).name for marker in DUMP_MARKERS)
    )
    assert not offenders, (
        "operator dump files tracked in git (they publish real conversation "
        f"payloads wherever the repo is pushed) — untrack them: {offenders}"
    )
