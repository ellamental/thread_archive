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

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# The throwaway machine. $HOME is redirected at import time, not in a fixture: a
# module that resolves its store location at import (``DEFAULT_HOME``) bakes
# whatever $HOME says at that moment, and a fixture runs long after. conftest is
# imported before the test modules that import thread_archive, so this is the home
# it bakes against. tests/meta/test_isolation.py enforces the result.
_SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="thread-archive-test-home-"))
os.environ["HOME"] = str(_SANDBOX_HOME)
atexit.register(shutil.rmtree, _SANDBOX_HOME, ignore_errors=True)

# XDG base dirs must follow the redirect: an inherited XDG_CONFIG_HOME (GitHub's
# runners export one) still names the real ~/.config, and an explicit XDG var
# outranks $HOME for anything XDG-aware. Dropped, they re-derive from the sandbox.
for _xdg in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    os.environ.pop(_xdg, None)


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch):
    """Re-pin ``$HOME`` per test, so one that rewrites it can't leak into the next."""
    monkeypatch.setenv("HOME", str(_SANDBOX_HOME))


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
    # The MCP server cohosts lazy catch-up ingest around its tools — in a test
    # that would background-import the machine's REAL AI-tool stores and repoint
    # the engine mid-suite. Off; test_lazy_ingest.py exercises it with stubs.
    monkeypatch.setenv("THREAD_ARCHIVE_MCP_INGEST", "0")
    # Same discipline for the nightly's capture-coverage stage: it enumerates
    # the machine's REAL AI-tool stores (enabled_watchers + discover), which a
    # test must never do. Green no-op here; test_capture_coverage.py exercises
    # the real check with stub watchers, and test_nightly.py re-patches its own
    # stub to assert the stage wiring.
    from thread_archive._ops import nightly as _nightly

    monkeypatch.setattr(_nightly, "check_coverage", lambda **kw: {"ok": True})
    # And for the nightly's source-mirror stage, which sweeps those same real
    # stores' files into the home. Green no-op here; test_source_mirror.py
    # exercises the real sweep with stub watchers, and test_nightly.py
    # re-patches its own stub to assert the stage wiring.
    monkeypatch.setattr(_nightly, "mirror_sources", lambda **kw: {"ok": True})
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
