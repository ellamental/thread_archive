"""Dev-only surfaces: the parts of the viewer whose subject is the machinery
rather than the archive.

This package is excluded from the wheel (see ``[tool.hatch.build.targets.wheel]
exclude`` in pyproject.toml), so it exists in a checkout and not in an install —
which is what lets it reach code that ships the same way. The shipped viewer
imports it fail-softly and answers ``404`` when it is absent, so a dev page is a
thing a source tree has rather than a feature an install is missing.

Two pages, both fed from ``search_lab/`` at the repo root: the retrieval report
at ``/retrieval`` (how search is doing) and the bench inventory at ``/lab`` (what
the bench has on hand to measure it with).
"""

from __future__ import annotations

from typing import Any, Optional


def _lab_module(name: str) -> Optional[Any]:
    """A ``search_lab`` module by name, or None if it won't load.

    The lab lives at the repo root, which is on no import path by default, so the
    root is resolved from this file's own location rather than from the process's
    working directory — a daemon's is wherever launchd started it, not the
    checkout. A lab that is absent or broken is simply no dev page; the caller
    turns that into a 404 rather than a traceback.
    """
    import importlib
    import sys
    from pathlib import Path

    # .../<repo>/src/thread_archive/_dev/__init__.py → <repo>
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        return importlib.import_module(f"search_lab.{name}")
    except Exception:  # noqa: BLE001 — an absent or broken lab is "no dev page"
        return None


def retrieval_report() -> Optional[Any]:
    """The search lab's ``retrieval_report`` module, or None."""
    return _lab_module("retrieval_report")


def lab_inventory() -> Optional[Any]:
    """The search lab's ``inventory`` module, or None."""
    return _lab_module("inventory")
