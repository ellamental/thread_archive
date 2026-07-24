"""Shared test fixtures + global-state isolation.

The store keeps a module-global engine and the truth-log keeps cached file handles;
both must be reset around every test so state never leaks between tests. Critically,
every test is defaulted to a *throwaway* home so nothing can ever touch the real
``~/.thread/archive`` (the global engine lazily resolves its DSN from ``$THREAD_ARCHIVE_HOME``,
so an un-homed test would otherwise create the real default).

The suite is also pinned **model-free** through the product's own switches
(``$THREAD_ARCHIVE_EMBED`` / ``$THREAD_ARCHIVE_RERANK`` set to ``off``): no test
can cold-load the torch models just because the venv happens to have the
``[embeddings]`` extra installed. Without the pin, any ``search()`` whose query
trips the rerank gate loads the real cross-encoder in-process — a 100-second
stall — and search-path tests exercise different code depending on which extras
are installed. Tests that cover the vector/rerank machinery opt back in per-test,
either by clearing the switch and constructing an ``Embedder``/``Reranker`` around
a scripted model, or by passing their own through the ``embedder`` / ``reranker``
arguments (see ``test_vectors.py``, ``test_rerank.py``).
"""

from __future__ import annotations

import atexit
import gc
import os
import shutil
import tempfile
import time
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
# The *_DIRS search paths ride along: a desktop Linux session injects real-home
# entries into them (KDE puts ~/.config/kdedefaults in XDG_CONFIG_DIRS, flatpak
# puts ~/.local/share/flatpak/... in XDG_DATA_DIRS), which trips the isolation
# guard; dropped, they fall back to the spec's system defaults. Unset on macOS.
for _xdg in (
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_DIRS",
    "XDG_DATA_DIRS",
):
    os.environ.pop(_xdg, None)

# THREAD_ARCHIVE_HOME is the store-location override the engine resolves *before*
# $HOME — so on a machine that sets it (the operator's, for the daemons), it wins
# over the redirect above and a test writes into the live archive. The full-suite
# env-var ratchet catches it, but a single-file or single-test run skips that meta
# check, so dropping it here is what actually makes the sandbox home take effect.
os.environ.pop("THREAD_ARCHIVE_HOME", None)

# Any CLI verb run in-process would otherwise renice the test runner itself, and
# nice() is one-way: the drop is permanent for the rest of the session and every
# later test — plus every child they spawn — inherits it. Set here at import, for
# the same reason as $HOME: a fixture runs after the first verb might already have
# fired. A test that wants the real throttle spawns a child with its own env.
os.environ["THREAD_ARCHIVE_NO_THROTTLE"] = "1"


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch):
    """Re-pin ``$HOME`` per test, so one that rewrites it can't leak into the next."""
    monkeypatch.setenv("HOME", str(_SANDBOX_HOME))


@pytest.fixture(autouse=True)
def _isolate_archive(tmp_path, monkeypatch):
    from thread_archive import _config as config
    from thread_archive._retrieval import embed_graph, vectors
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
    # Model-free suite: no real torch model may load, regardless of installed extras.
    # The product's own off switches, so the pin runs through the same code an
    # operator's `--lexical-only` does (read per call — set here, honored from here on).
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "off")
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK", "off")
    # Coherence re-rank off suite-wide: its background graph-refresh thread
    # holds a pooled sqlite connection past the test that spawned it, and the
    # late GC fails an unrelated victim test. Its logic has dedicated
    # deterministic coverage (test_embed_graph.py builds inline and injects
    # gamma explicitly; tests that need the env set their own).
    monkeypatch.setenv("THREAD_ARCHIVE_COHERENCE", "off")
    # The known-archives registry records every home open_archive touches into
    # ~/.thread/archives.json. Off suite-wide so hundreds of tmp homes don't
    # accumulate there and each open stays a pure store op; the registry has its
    # own coverage (test_archives_registry.py opts back in with a tmp path).
    monkeypatch.setenv("THREAD_ARCHIVE_REGISTRY", "0")
    _base.close_engine()
    jsonl_log.reset_handles()
    # The retrieval caches key on id(get_engine()); a closed engine's id can be reused
    # by the next test's engine, so drop them with the engine or a stale matrix/graph
    # from a prior test's DB gets served (serve-stale skips the token check within the
    # cooldown).
    vectors.reset_matrix_cache()
    embed_graph.reset_cache()
    yield
    # Let any in-flight single-flight refresh finish before the engine closes: a daemon
    # refresh thread that outlives its test would touch a torn-down engine and leak a
    # connection the late GC blames on an unrelated later test.
    _deadline = time.monotonic() + 5.0
    while (vectors._MATRIX_REFRESHING or embed_graph._REFRESHING) and time.monotonic() < _deadline:
        time.sleep(0.01)
    jsonl_log.reset_handles()
    _base.close_engine()
    vectors.reset_matrix_cache()
    embed_graph.reset_cache()
    # sqlite3 and subprocess objects can participate in cycles, delaying their
    # ResourceWarning until an unrelated later test. Collect at the isolation
    # boundary so a leaked resource fails the test that created it. Generation 0
    # only: the cycles a just-finished test leaves behind are still young, while a
    # full collection walks the entire heap once per test — at this suite's size
    # that alone is the majority of its runtime.
    gc.collect(0)


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
