"""Ratchet the public API boundary.

The package's public surface is exactly: ``thread_archive.__all__`` (the api.py
re-exports), the ``archive`` CLI, the two MCP server commands, the manifest
hook, and the on-disk truth format (docs/format.md). Everything else is a
private, underscore-prefixed module. These tests make widening the surface a
deliberate act (edit the pinned list here) instead of a naming accident.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import thread_archive
from thread_archive import api

# The full advertised Python surface. Adding a name here is an API commitment —
# it must survive until a deliberate deprecation, even at 0.0.x.
PUBLIC_API = [
    "__version__",
    "open_archive",
    "close",
    "search",
    "read_thread",
    "read_thread_structured",
    "import_path",
    "reindex",
    "embed",
    "checkpoint",
    "watch",
    "status",
    "knowledge_status",
    "bridge_topics",
    "topic_peers",
    "backup",
    "verify",
    "repair",
    "restore_drill",
    "nightly",
]

# The only modules allowed to live at a public (non-underscore) name: the
# library surface, the CLI entry point, and the family-manifest hook
# (advertised as `python -m thread_archive.manifest`).
PUBLIC_MODULES = {"api", "cli", "manifest"}


def test_all_is_exactly_the_pinned_surface() -> None:
    assert sorted(thread_archive.__all__) == sorted(PUBLIC_API)


def test_every_advertised_name_resolves_and_is_callable() -> None:
    for name in PUBLIC_API:
        obj = getattr(thread_archive, name)
        if name != "__version__":
            assert callable(obj), name


def test_every_public_def_in_api_is_exported() -> None:
    # api.py is the surface module: a public-named function there that isn't
    # re-exported is a boundary leak (it looks supported but isn't advertised).
    public_defs = {
        name
        for name, obj in vars(api).items()
        if inspect.isfunction(obj) and obj.__module__ == api.__name__ and not name.startswith("_")
    }
    assert public_defs == set(PUBLIC_API) - {"__version__"}


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
