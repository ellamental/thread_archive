"""Ratchet the public API boundary.

The package deliberately exposes **no public Python API**. Its public surface
is exactly: the ``archive`` CLI, the two MCP server commands, and the on-disk
truth format (docs/format.md). Everything else — the ``_api`` coordination
layer included — is a private, underscore-prefixed module. These tests make
widening the surface a deliberate act (edit the pinned sets here) instead of a
naming accident.
"""

from __future__ import annotations

from pathlib import Path

import thread_archive

# The full advertised Python surface: the version, nothing else. Adding a name
# here is an API commitment — it must survive until a deliberate deprecation.
PUBLIC_API = ["__version__"]

# The only module allowed to live at a public (non-underscore) name: the CLI
# entry point. Installer machinery (the family manifest writer) lives in host/,
# outside the package — it needs a repo checkout and is never shipped.
PUBLIC_MODULES = {"cli"}


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
