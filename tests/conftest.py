"""Shared test fixtures + global-state isolation.

The open archive (`_store._instance.Archive`) owns the engine and every cache above
it, and the truth-log keeps cached file handles; both must be dropped around every
test so state never leaks between tests. Closing the archive is one call and covers
every cache in it — only genuinely process-scoped state needs its own line. Critically,
every test is defaulted to a *throwaway* home so nothing can ever touch the real
``~/.thread/archive`` (the global engine lazily resolves its DSN from ``$THREAD_ARCHIVE_HOME``,
so an un-homed test would otherwise create the real default).

The suite is also pinned **model-free** through the product's own switches
(``$THREAD_ARCHIVE_EMBED`` set to ``off``): no test can cold-load the torch model
just because the venv happens to have the ``[embeddings]`` extra installed.
Without the pin, a search-path test would exercise different code depending on
which extras are installed. Tests that cover the vector machinery opt back in
per-test, either by clearing the switch and constructing an ``Embedder`` around a
scripted model, or by passing their own through the ``embedder`` argument (see
``test_vectors.py``).
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
for _xdg in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
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
    from thread_archive import _tools
    from thread_archive._ops import ingest_errors
    from thread_archive._retrieval import embed_graph, model_slot, vectors
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
    # The product's own off switch, so the pin runs through the same code an
    # operator's `--lexical-only` does (read per call — set here, honored from here on).
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "off")
    # Coherence re-rank off suite-wide: its background graph-refresh thread
    # holds a pooled sqlite connection past the test that spawned it, and the
    # late GC fails an unrelated victim test. Its logic has dedicated
    # deterministic coverage (test_embed_graph.py builds inline and injects
    # gamma explicitly; tests that need the env set their own).
    monkeypatch.setenv("THREAD_ARCHIVE_COHERENCE", "off")
    # Runtime telemetry records only on a dev install (_ops/telemetry.py), and a
    # tmp home has no config.json — so every ledger assertion in this suite would
    # otherwise be asserting over a file nothing writes, and pass for the wrong
    # reason. Pinned on through the product's own per-ledger switches, which is
    # exactly what a maintainer running the bench sets. test_telemetry_gate.py
    # clears them to cover the default and the config path.
    for _switch in ("THREAD_ARCHIVE_USAGE_LOG", "THREAD_ARCHIVE_WEB_METRICS",
                    "THREAD_ARCHIVE_INGEST_LOG", "THREAD_ARCHIVE_LOAD_LOG"):
        monkeypatch.setenv(_switch, "1")
    # The load policy is a process global a server sets at startup (see
    # model_slot): a test that starts one would otherwise leave every later test in
    # the worker serving lexical-only, which looks like a ranking bug rather than a
    # leaked flag.
    model_slot.set_defer_construction(False)
    # Same shape for the default retrieval surface: a daemon declares it once at
    # startup (`set_default_surface`), so a test that boots the web viewer would
    # relabel every ledger row the worker's later tests write as served-by-web.
    # Reset through the product's own knob, so this cannot drift from where the
    # state actually lives.
    _tools.set_default_surface(_tools.UNATTRIBUTED)
    # Closing the archive is the whole cache teardown: the matrix, the corpus graph
    # and the exact-set memo live inside the `Archive` (`_store._instance`) and go
    # with it. A cache added above the store needs no line here and cannot be
    # forgotten by a fixture that does not know about it.
    _base.close_engine()
    jsonl_log.reset_handles()
    # The ingest-error tally is deliberately *process*-scoped, not archive-scoped —
    # it counts this process's sightings — so it is reset separately. The ledger only
    # writes a signature's first sighting and then powers of ten, so a count left
    # behind by an earlier test silences the *next* test's identical fault, which
    # reads as an empty ledger rather than as leaked state, and only in whatever run
    # puts the two tests on the same worker.
    ingest_errors.reset_tally()
    yield
    # Let any in-flight single-flight refresh finish before the engine closes: a daemon
    # refresh thread that outlives its test would touch a torn-down engine and leak a
    # connection the late GC blames on an unrelated later test.
    _deadline = time.monotonic() + 5.0
    while (vectors._MATRIX_REFRESHING or embed_graph._REFRESHING) and time.monotonic() < _deadline:
        time.sleep(0.01)
    jsonl_log.reset_handles()
    _base.close_engine()
    ingest_errors.reset_tally()
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


def pytest_collection_modifyitems(config, items):
    """Stand the ``viewer`` marker down where there is no viewer.

    The viewer is dev-only: ``thread_archive._web`` and its built bundle are
    excluded from the wheel (docs/public/web-viewer.md), so `web`, `watch --web`, and
    the setup wizard's browser step exist in a checkout and not in an install.
    This suite runs both ways — from the source tree, and against the installed
    wheel in the Docker install lane — and the marked tests describe behaviour
    only the first one has.

    Skipping is the honest outcome rather than a gap: what an install does
    instead (no verb, no flag, no offer) is asserted directly by
    tests/test_viewer_probe.py and the package lane's installed-CLI checks.
    """
    from thread_archive._viewer import viewer_available

    if viewer_available():
        return
    skip = pytest.mark.skip(reason="the viewer is dev-only (no wheel carries it)")
    for item in items:
        if "viewer" in item.keywords:
            item.add_marker(skip)
