"""The curation seam — what the archive reports with and without thread-librarian.

The graph is the archive's own (``_knowledge.graph``); the one surface that
belongs to the plugin is curation stats, and ``api.curation_stats`` must say
"not installed" rather than guess when the plugin is absent. Both sides are
pinned here (the dev venv carries the plugin; ``importorskip`` guards the
delegation half).
"""

from __future__ import annotations

import sys

import pytest

from thread_archive import _api as ta


@pytest.fixture
def no_plugin(monkeypatch):
    """Make ``import thread_librarian`` (and its submodules) raise ImportError."""
    for name in list(sys.modules):
        if name == "thread_librarian" or name.startswith("thread_librarian."):
            monkeypatch.delitem(sys.modules, name)
    # A None entry makes the import system raise ImportError for the name and
    # anything below it — the plugin-not-installed condition, without touching
    # the venv.
    monkeypatch.setitem(sys.modules, "thread_librarian", None)


def test_api_curation_stats_degrades(archive_home, no_plugin) -> None:
    out = ta.curation_stats()
    assert out["available"] is False and "thread-librarian" in out["error"]


def test_api_curation_stats_delegates(archive_home) -> None:
    pytest.importorskip("thread_librarian")
    from thread_archive._store import init_db

    init_db()
    out = ta.curation_stats(days=1)
    # The plugin's stats shape: drains + coverage over the (empty) store.
    assert "drains" in out and "coverage" in out


def test_graph_api_needs_no_plugin(archive_home, no_plugin) -> None:
    """The graph surfaces are the archive's own — fully functional without the
    curation plugin, empty only because nothing has curated yet."""
    from thread_archive._store import init_db

    init_db()
    from thread_archive import _knowledge as knowledge

    knowledge.reset_cache()
    status = ta.knowledge_status()
    assert status["available"] is True and status["nodes"] == 0
    assert ta.bridge_topics() == []
    assert ta.topic_peers(999_999_999) == []
    assert knowledge.get_topic_graph_metadata([]) == {}
    assert knowledge.get_topic_graph_meta("nope") is None
