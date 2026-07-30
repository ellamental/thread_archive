"""Whether this installation carries the web viewer.

The viewer is a dev-only surface: :mod:`thread_archive._web` and its built
bundle are excluded from the wheel, so the package exists in a checkout and not
in an install (the same shape as :mod:`thread_archive._dev`). A wheel therefore
carries preservation, retrieval, and the MCP server — and no browser UI, no
646K of JavaScript.

Everything that would offer the viewer asks here first. The CLI registers the
``web`` verb and ``watch --web`` only when the answer is yes, so an install
never advertises a command it cannot run (``tests/test_package_artifact.py``
holds that line), and the service layer leaves ``--web`` out of the unit it
writes for the same reason.

The probe resolves a spec rather than importing: a missing viewer is the normal
state of an install, not an exception to swallow, and importing would drag in
the server module on every CLI start just to learn it exists.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any


def viewer_available(
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
) -> bool:
    """True when ``thread_archive._web`` is importable in this installation.

    ``find_spec`` is a seam, not a knob: this tree always has the viewer, so the
    install-shaped answer is unreachable here without one, and the callers that
    branch on it (the CLI parser, the service spec) would go untested against
    the shape that actually ships.
    """
    try:
        return find_spec("thread_archive._web") is not None
    except (ImportError, ValueError):
        # A parent package that won't load, or a spec-less namespace entry —
        # either way there is no viewer to offer.
        return False
