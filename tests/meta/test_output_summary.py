"""The run-result ratchet: a finished run must always report its counts.

pytest's verbosity is cumulative — every ``-q`` on the command line stacks with
every ``-q`` the config supplies. At -qq pytest stops printing the
``N passed in Xs`` counts line, and a green run's entire output becomes a field
of dots. That is the worst possible failure shape for the readers this suite
actually has: agents and CI rows, which verify a run by piping it through
``grep -E 'passed|failed'``. On green that grep returns *nothing*, which is
byte-identical to a command that failed to launch — so the reader can't tell a
passing suite from a broken invocation, and re-runs it, harder, several times.

Callers own their own quiet: ``-q`` belongs on the command line (docs/install.md's
run line, the ci.toml rows), never in ``addopts``, so a caller who asks for
quiet gets quiet *with* its counts line.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"


def _addopts() -> str:
    cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return cfg["tool"]["pytest"]["ini_options"].get("addopts", "")


def test_addopts_carries_no_quiet_flag() -> None:
    """No config-supplied quiet: it stacks with the caller's and hides the counts."""
    opts = _addopts().split()
    offenders = [o for o in opts if o == "--quiet" or re.fullmatch(r"-q+", o)]
    assert not offenders, (
        f"addopts sets {offenders} — quiet is the caller's to pass. Config quiet "
        "plus the caller's -q lands at -qq and drops the 'N passed' counts line."
    )


@pytest.mark.integration
def test_quiet_run_still_prints_the_counts_line(tmp_path: Path) -> None:
    """The property itself: a caller passing -q still sees `N passed`.

    Spawned against this repo's config (``-c pyproject.toml``) over a throwaway
    test file, so it measures the shipped configuration rather than a
    reconstruction of it.
    """
    probe = tmp_path / "test_probe.py"
    probe.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(PYPROJECT), "-q",
         "-p", "no:cacheprovider", str(probe)],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert re.search(r"\b1 passed\b", run.stdout), (
        "a -q run printed no counts line — the config is quieting on top of the "
        f"caller again:\n{run.stdout}"
    )
