"""Ratchet the public API boundary.

The public API is exactly four things: the retrieval tools
(``thread_search`` / ``thread_read`` — served to agents by ``archive-mcp`` and
to a person by the ``thread-archive search`` / ``thread-archive read`` verbs),
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
import inspect
from pathlib import Path

import thread_archive
from thread_archive import _tools, cli
from thread_archive._web import route
from thread_archive.cli import (
    _LEGACY_VERBS,
    _SECTIONS,
    _normalize,
    _subcommands_of,
    build_parser,
)

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

# The command tree: each listed root command mapped to the actions under it
# (empty for a leaf). Most of the CLI is private tooling, but its verbs are wired
# into the LaunchAgent plists, lab's cron script, the /ci skill, and the
# monitor's heartbeat contract — this pin makes renaming one a deliberate act
# that updates those in the same change, not a compatibility promise to anyone
# external.
# `search` and `read` are the exception: they are the retrieval tools with a
# terminal in front of them (one implementation in thread_archive/_tools.py,
# served over MCP and here), so they carry the same public promise the tools do
# and there is nothing to keep out. What stays out is a *second implementation* —
# `web` is an opener, not a read surface: it hands the cohosted viewer's URL to a
# browser and returns nothing itself.
CLI_TREE = {
    "search": set(),
    "read": set(),
    "web": set(),
    "watch": set(),
    "source": {"list", "import", "import-account", "mirror", "coverage", "loads", "fix"},
    "index": {"rebuild", "migrate", "embed", "verify", "repair"},
    "backup": {"run", "nightly", "drill", "restore"},
    "status": set(),
    "setup": set(),
    "service": {"install", "uninstall", "restart", "status"},
    "self-update": set(),
    "uninstall": set(),
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


def _root() -> argparse._SubParsersAction:
    return next(
        a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    )


def test_cli_tree_is_exactly_the_pinned_shape() -> None:
    sub = _root()
    # Iterating the root map yields only the listed commands — the legacy
    # spellings resolve but are deliberately not part of the surface.
    assert set(sub.choices) == set(CLI_TREE)
    for name, actions in CLI_TREE.items():
        assert set(_subcommands_of(sub.choices[name])) == actions, name


def test_every_command_appears_in_exactly_one_help_section() -> None:
    """`--help` is the discovery surface: a command missing from a section is a
    command nobody finds. Pins both directions, so adding one to the tree
    without placing it reds here rather than going quietly unlisted."""
    listed = [name for _, names in _SECTIONS for name in names]
    assert len(listed) == len(set(listed))
    assert set(listed) == set(CLI_TREE)


def test_pre_group_spellings_still_resolve() -> None:
    """The renames must not strand a machine already running the old verbs — the
    installed service manifests, operator scripts, shell history. Each legacy
    spelling must reach the *same parser object* its grouped path reaches."""
    parser, sub = build_parser(), _root()
    for old, path in _LEGACY_VERBS.items():
        target = sub.choices[path[0]]
        for step in path[1:]:
            nested = next(
                a for a in target._actions if isinstance(a, argparse._SubParsersAction)
            )
            target = nested.choices[step]
        assert sub.choices[old] is target, old
        assert old in sub.choices  # resolves…
        assert old not in set(sub.choices)  # …but is listed nowhere

    # `backup <dest>` is the one that can't be an alias — `backup` is a group
    # name now — so the rewrite that keeps it working is exercised end to end.
    assert parser.parse_args(_normalize(["backup", "/d"])).func is cli.cmd_backup
    assert parser.parse_args(_normalize(["backup", "--home", "/h", "/d"])).dest == "/d"
    assert parser.parse_args(_normalize(["backup", "nightly", "/d"])).func is cli.cmd_nightly


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


def test_retrieval_tools_expose_no_extension_region_surface() -> None:
    """The extension region (docs/format.md) is storage, not product.

    ``archive-mcp`` generates its tool schema from these signatures and
    docstrings, so a parameter or a paragraph here is shipped to every install
    — including the overwhelming majority that have no knowledge layer writing
    the region and so can never make it return anything. Reaching the graph is
    the writing layer's own tools' job; keeping the seam private is what lets
    that layer own its vocabulary without a format bump.
    """
    for tool in (_tools.thread_search, _tools.thread_read):
        params = inspect.signature(tool).parameters
        assert "topic_id" not in params, tool.__name__
        assert "topic" not in (tool.__doc__ or "").lower(), tool.__name__

    verbs = next(
        a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    ).choices
    for verb in ("search", "read"):
        flags = {o for a in verbs[verb]._actions for o in a.option_strings}
        assert "--topic-id" not in flags, verb


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
