"""Ratchet the public API boundary.

The package deliberately exposes **no public Python API**. Its public surface
is exactly: the ``archive`` CLI's promised verbs, the two MCP server commands,
and the on-disk truth format (docs/format.md). Everything else — the ``_api``
coordination layer included — is a private, underscore-prefixed module, and
the remaining CLI verbs are conveniences with no stability promise. These
tests make widening the surface a deliberate act (edit the pinned sets here)
instead of a naming accident.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import thread_archive
from thread_archive.cli import UNSTABLE_MARKER, build_parser

# The full advertised Python surface: the version, nothing else. Adding a name
# here is an API commitment — it must survive until a deliberate deprecation.
PUBLIC_API = ["__version__"]

# The only module allowed to live at a public (non-underscore) name: the CLI
# entry point. Installer machinery (the family manifest writer) lives in host/,
# outside the package — it needs a repo checkout and is never shipped.
PUBLIC_MODULES = {"cli"}

# The CLI's promised verbs: the service commands the LaunchAgents run, plus the
# durability kit that enforces the truth-format promise. Renaming or removing
# one is a breaking change — daemons, cron, and the monitor's heartbeat
# contract stand on these.
PROMISED_CLI_VERBS = {
    "watch",
    "nightly",
    "backup",
    "verify",
    "restore-drill",
    "reindex",
    "repair",
    "status",
}

# Conveniences with no stability promise (retrieval's promised surface is the
# MCP tools). A new subcommand must be classified into one set or the other —
# that classification, not the parser, is the API decision.
CONVENIENCE_CLI_VERBS = {
    "import",
    "import-export",
    "search",
    "read",
    "web",
    "embed",
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


def test_cli_verbs_are_exactly_the_pinned_tiers() -> None:
    sub = next(
        a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert not PROMISED_CLI_VERBS & CONVENIENCE_CLI_VERBS
    assert set(sub.choices) == PROMISED_CLI_VERBS | CONVENIENCE_CLI_VERBS


def test_cli_help_marks_exactly_the_convenience_verbs() -> None:
    # The [unstable] marker in `archive --help` is the user-facing face of the
    # tiering; it must agree with the pinned sets verb-for-verb.
    sub = next(
        a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    )
    marked = {
        a.dest for a in sub._choices_actions if a.help and UNSTABLE_MARKER in a.help
    }
    assert marked == CONVENIENCE_CLI_VERBS


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
