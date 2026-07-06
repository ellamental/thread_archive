"""Shared test fixtures + global-state isolation.

The store keeps a module-global engine and the truth-log keeps cached file handles;
both must be reset around every test so state never leaks between tests. Critically,
every test is defaulted to a *throwaway* home so nothing can ever touch the real
``~/.thread/archive`` (the global engine lazily resolves its DSN from ``$THREAD_ARCHIVE_HOME``,
so an un-homed test would otherwise create the real default).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_archive(tmp_path, monkeypatch):
    from thread_archive import config
    from thread_archive.store import _base
    from thread_archive.truth import jsonl_log

    # Default home for any test that doesn't set its own (archive_home / --home).
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path / "_home"))
    monkeypatch.delenv(config.ENV_TRUTH, raising=False)
    monkeypatch.delenv(config.ENV_INDEX, raising=False)
    # The librarian backfill worker id activates claim-on-read; never let an ambient one
    # leak into a test that expects a plain queue read.
    monkeypatch.delenv("THREAD_ARCHIVE_LIBRARIAN_WORKER", raising=False)
    _base.close_engine()
    jsonl_log.reset_handles()
    yield
    jsonl_log.reset_handles()
    _base.close_engine()


@pytest.fixture
def archive_home(tmp_path, monkeypatch):
    """Point the archive at a tmp home and return its path.

    Engine/handle isolation is handled by the autouse ``_isolate_archive`` fixture;
    this only overrides the home location and hands the test the directory so it can
    inspect ``truth/`` and ``index.db``.
    """
    from thread_archive import config

    home = tmp_path / "arc"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(config.ENV_HOME, str(home))
    return home
