"""Lock the vendored thread_import: it imports as a clean island and its parsers
instantiate. The single-path ratchet (test_import_ratchet) also scans this tree,
so a server/postgres import sneaking in via the vendor would fail there.
"""

from __future__ import annotations

import pytest


def test_thread_import_is_vendored_in_repo() -> None:
    import thread_import

    # The vendored island under the repo's own src/, never a pip-installed external
    # package — asserted without pinning the clone's directory name (it isn't always
    # "thread_archive": a Docker build, CI checkout, or rename would all differ).
    path = thread_import.__file__.replace("\\", "/")
    assert "/src/thread_import/" in path
    assert "site-packages" not in path and "dist-packages" not in path


@pytest.mark.parametrize("provider", ["chatgpt", "claude", "claude-code", "cursor"])
def test_every_provider_parser_instantiates(provider: str) -> None:
    from thread_import import get_parser

    parser = get_parser(provider)
    assert parser is not None


def test_normalized_message_surface_present() -> None:
    # The types the importer will build events from.
    from thread_import import DefaultEventBuilder, ThreadEvent  # noqa: F401
    from thread_import.parsers import NormalizedMessage  # noqa: F401
