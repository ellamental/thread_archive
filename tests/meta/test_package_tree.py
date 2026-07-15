"""The package-tree ratchet: no operator data dumps inside the wheel.

Hatch packages everything under ``src/thread_archive``, so any file that lands
there ships to every installer. Live-repair undo records (``*_backup_*`` /
``*_plan_*`` dumps written by the one-shot ``_scripts``) are operator data for
this host, not runtime code — they belong in ``host/repair-dumps/``, outside
the package tree. This scan fails the moment one lands back inside ``src/``.

``_scripts`` gets the stricter form: Python only. Its modules are one-shot
repair/backfill tools that read and write dumps, so it is the directory where
data files accrete when a script defaults its output path to ``Path(__file__)``.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "thread_archive"

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
        f"wheel) — move them to host/repair-dumps/: {sorted(map(str, offenders))}"
    )


def test_scripts_dir_is_python_only():
    scripts = SRC / "_scripts"
    offenders = [
        p.name
        for p in _package_files()
        if scripts in p.parents and p.suffix != ".py"
    ]
    assert not offenders, (
        "non-Python files in _scripts/ (data outputs belong in host/repair-dumps/, "
        f"outside the package): {sorted(offenders)}"
    )
