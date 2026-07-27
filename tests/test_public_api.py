"""Ratchet the public API boundary.

The public API is exactly four things: the retrieval tools
(``thread_search`` / ``thread_read`` — served to agents by ``archive-mcp`` and
to a person by the ``thread_archive search`` / ``thread_archive read`` verbs),
the on-disk truth format (docs/format.md), the provider plugin API
(``thread_archive.provider``, docs/providers.md), and the web viewer's URLs
(README → "Web viewer"). Everything else — the rest of the
``thread_archive`` CLI, the ``_api`` coordination layer, every underscore-prefixed
module — is private support machinery. These tests make
widening the surface a deliberate act (edit the pinned sets here) instead of
a naming accident.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import thread_archive
from thread_archive._web import route
from thread_archive.cli import build_parser

# The full advertised Python surface: the version, nothing else. Adding a name
# here is an API commitment — it must survive until a deliberate deprecation.
PUBLIC_API = ["__version__"]

# The modules allowed to live at a public (non-underscore) name. `cli` is the
# entry point; `provider` is the plugin API — the one surface archive commits to
# keeping stable, because a provider defined outside the package is written
# against it and cannot follow the private tree's churn. Installer machinery (the
# family manifest writer) lives in host/, outside the package — it needs a repo
# checkout and is never shipped.
PUBLIC_MODULES = {"cli", "provider"}

# Most of the CLI is private tooling, but its verbs are wired into the LaunchAgent
# plists, lab's cron script, the /ci skill, and the monitor's heartbeat
# contract — this pin makes renaming one a deliberate act that updates those
# in the same change, not a compatibility promise to anyone external.
# `search` and `read` are the exception: they are the retrieval tools with a
# terminal in front of them (one implementation in thread_archive/_tools.py,
# served over MCP and here), so they carry the same public promise the tools do
# and there is nothing to keep out. What stays out is a *second implementation* —
# `web` is an opener, not a read surface: it hands the cohosted viewer's URL to a
# browser and returns nothing itself.
CLI_VERBS = {
    "setup",
    "uninstall",
    "search",
    "read",
    "import",
    "import-export",
    "providers",
    "watch",
    "web",
    "embed",
    "reindex",
    "migrate",
    "status",
    "loads",
    "backup",
    "verify",
    "restore-drill",
    "restore",
    "nightly",
    "coverage",
    "mirror",
    "repair",
    "daemon",
    "fix-import",
    "self-update",
}


# The viewer's JSON endpoints that programs outside this repo call directly: the
# family manifest's health probe, and the editor buttons resolving a session uuid
# to its thread. Every other /api/* path backs the viewer's own bundle and moves
# with the frontend. The viewer's committed *page* routes are pinned in the
# frontend, against the route table itself (frontend/e2e/routes.ts →
# PUBLIC_ROUTES): the server answers every path with the SPA shell, so only that
# table knows whether a URL resolves to a page.
PUBLIC_WEB_ENDPOINTS = {"/api/health", "/api/archive-link"}


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


def test_committed_web_endpoints_are_served(archive_home) -> None:
    """Each committed endpoint resolves to a handler of its own.

    An endpoint that got renamed or deleted falls through to the router's
    unknown-``/api/`` 404 — so this reds, and restoring the URL (or deciding to
    break it, here) is the deliberate act. The responses themselves are
    behaviour, tested in test_web.py; this only pins that the URLs exist.
    """
    assert route("GET", "/api/not-a-real-endpoint", {})[0] == 404  # the fallthrough
    for path in PUBLIC_WEB_ENDPOINTS:
        assert route("GET", path, {})[0] != 404, path


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
