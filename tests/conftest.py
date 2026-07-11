"""Shared test fixtures + global-state isolation.

The store keeps a module-global engine and the truth-log keeps cached file handles;
both must be reset around every test so state never leaks between tests. Critically,
every test is defaulted to a *throwaway* home so nothing can ever touch the real
``~/.thread/archive`` (the global engine lazily resolves its DSN from ``$THREAD_ARCHIVE_HOME``,
so an un-homed test would otherwise create the real default).

The suite is also pinned **model-free**: the embed/rerank availability gates are
forced off so no test can cold-load the torch models just because the venv happens
to have the ``[embeddings]`` extra installed. Without the pin, any ``search()``
whose query trips the rerank gate loads the real cross-encoder in-process — a
100-second stall — and search-path tests exercise different code depending on
which extras are installed. Tests that cover the vector/rerank machinery opt back
in per-test with their own monkeypatches (see ``test_vectors.py``,
``test_rerank.py``), which override this default.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_archive(tmp_path, monkeypatch):
    from thread_archive import _config as config
    from thread_archive._retrieval import embed, rerank
    from thread_archive._store import _base
    from thread_archive._truth import jsonl_log

    # Default home for any test that doesn't set its own (archive_home / --home).
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path / "_home"))
    monkeypatch.delenv(config.ENV_TRUTH, raising=False)
    monkeypatch.delenv(config.ENV_INDEX, raising=False)
    # The nightly pipeline stamps a family-monitor heartbeat in ~/.thread/logs
    # when that dir exists; a test run must never touch the real box's beat.
    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(tmp_path / "_family_logs"))
    # The librarian backfill worker id activates claim-on-read; never let an ambient one
    # leak into a test that expects a plain queue read.
    monkeypatch.delenv("THREAD_ARCHIVE_LIBRARIAN_WORKER", raising=False)
    # The MCP server cohosts lazy catch-up ingest around its tools — in a test
    # that would background-import the machine's REAL AI-tool stores and repoint
    # the engine mid-suite. Off; test_lazy_ingest.py exercises it with stubs.
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "0")
    # Model-free suite: no real torch model may load, regardless of installed extras.
    monkeypatch.setattr(embed, "is_available", lambda: False)
    monkeypatch.setattr(rerank, "is_available", lambda: False)
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
    from thread_archive import _config as config

    home = tmp_path / "arc"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(config.ENV_HOME, str(home))
    return home
