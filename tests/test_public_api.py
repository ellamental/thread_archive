"""Ratchet the public API boundary.

The public API is exactly two things: the retrieval MCP tools
(``thread_search`` / ``thread_read``, served by ``archive-mcp``) and the
on-disk truth format (docs/format.md). Everything else — the ``archive`` CLI,
the librarian MCP server, the ``_api`` coordination layer, every
underscore-prefixed module — is private support machinery. These tests make
widening the surface a deliberate act (edit the pinned sets here) instead of
a naming accident.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import thread_archive
from thread_archive.cli import build_parser

# The full advertised Python surface: the version, nothing else. Adding a name
# here is an API commitment — it must survive until a deliberate deprecation.
PUBLIC_API = ["__version__"]

# The only module allowed to live at a public (non-underscore) name: the CLI
# entry point. Installer machinery (the family manifest writer) lives in host/,
# outside the package — it needs a repo checkout and is never shipped.
PUBLIC_MODULES = {"cli"}

# The CLI is private tooling, but its verbs are wired into the LaunchAgent
# plists, lab's cron script, the /ci skill, and the monitor's heartbeat
# contract — this pin makes renaming one a deliberate act that updates those
# in the same change, not a compatibility promise to anyone external.
# Retrieval verbs (search/read/web) are deliberately absent and must stay
# absent: the public MCP tools are the one retrieval surface, and the watcher
# cohosts the viewer.
CLI_VERBS = {
    "import",
    "import-export",
    "watch",
    "embed",
    "reindex",
    "status",
    "backup",
    "verify",
    "restore-drill",
    "restore",
    "nightly",
    "coverage",
    "repair",
    "redact",
    "unredact",
    "daemon",
}


def test_all_is_exactly_the_pinned_surface() -> None:
    assert sorted(thread_archive.__all__) == sorted(PUBLIC_API)


def test_no_function_reexports_on_the_package() -> None:
    # The package namespace must not quietly re-grow a function surface: every
    # callable a consumer could find on `thread_archive` must be pinned above.
    leaked = {
        name
        for name in vars(thread_archive)
        if not name.startswith("_") and callable(getattr(thread_archive, name))
    }
    assert not leaked, leaked


def test_cli_verbs_are_exactly_the_pinned_set() -> None:
    sub = next(
        a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert set(sub.choices) == CLI_VERBS


def test_no_unsanctioned_public_modules() -> None:
    pkg_dir = Path(thread_archive.__file__).parent
    public = set()
    for child in pkg_dir.iterdir():
        name = child.name
        if name.startswith(("_", ".")):
            continue
        if child.is_dir() and (child / "__init__.py").exists():
            public.add(name)
        elif child.suffix == ".py":
            public.add(child.stem)
    assert public == PUBLIC_MODULES
