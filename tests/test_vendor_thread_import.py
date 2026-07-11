"""Lock the vendored _thread_import: it imports as a clean island and its parsers
instantiate. The single-path ratchet (test_import_ratchet) also scans this tree,
so a server/postgres import sneaking in via the vendor would fail there.
"""

from __future__ import annotations

import pytest


def test_thread_import_is_vendored_in_repo() -> None:
    import thread_archive._thread_import as thread_import

    # The vendored island under the archive package's own tree, never a
    # pip-installed external package.
    path = thread_import.__file__.replace("\\", "/")
    assert "/thread_archive/_thread_import/" in path


def test_thread_import_is_not_a_top_level_package() -> None:
    # The island is private to thread_archive: a pip install must not plant a
    # public top-level `thread_import` in site-packages.
    with pytest.raises(ModuleNotFoundError):
        import thread_import  # noqa: F401


@pytest.mark.parametrize("provider", ["chatgpt", "claude", "claude-code", "cursor"])
def test_every_provider_parser_instantiates(provider: str) -> None:
    from thread_archive._thread_import import get_parser

    parser = get_parser(provider)
    assert parser is not None


def test_normalized_message_surface_present() -> None:
    # The types the importer will build events from.
    from thread_archive._thread_import import DefaultEventBuilder, ThreadEvent  # noqa: F401
    from thread_archive._thread_import.parsers import NormalizedMessage  # noqa: F401
