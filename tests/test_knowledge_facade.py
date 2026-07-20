"""The librarian seam — what the archive reports with and without thread-librarian.

The archive keeps only the knowledge layer's data plane; everything analytic
(curation stats, graph metadata, the subjects lens) belongs to the optional
``thread_librarian`` package and must degrade cleanly when it is absent. Both
sides are pinned here (the dev venv carries the plugin; ``importorskip``
guards the delegation half).
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


def test_topic_read_degrades_without_plugin(archive_home, no_plugin) -> None:
    """The compatibility topic reader works plugin-free: existing KG records
    still read, just without graph metadata or community peers."""
    from thread_archive._knowledge import topic_get
    from thread_archive._store import Thread, get_session, init_db

    init_db()
    tid = "01T0PIC0000000000000000001"
    with get_session() as s:
        s.add(Thread(id=tid, name="t", title="A Topic", thread_type="topic"))
        s.commit()
    detail = topic_get(tid)
    assert detail["title"] == "A Topic"
    assert detail["graph"] is None and detail["peers"] == []


def test_search_render_degrades_without_plugin(archive_home, no_plugin) -> None:
    """The subjects seam is a strict no-op when the lens's package is absent."""
    from thread_archive._retrieval.format import subjects_line

    assert subjects_line([{"thread_id": "x", "event_id": 1}]) is None
