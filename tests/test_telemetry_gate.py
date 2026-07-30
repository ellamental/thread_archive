"""Runtime telemetry records only on an install that is being developed on.

The archive keeps ledgers of *how it ran* — served requests, retrieval calls,
ingest passes, load runs. They are a maintainer's instruments: nothing in the
product reads them, their consumers are the dev panels and the bench, and
rotation retains every segment forever by design. On someone's laptop, where the
archive is infrastructure they read conversations through, that is a permanently
growing file of what they searched for, paid for a question nobody there will
ask.

So the default is off, and ``"dev_mode": true`` in ``config.json`` is what turns
it on (:mod:`thread_archive._ops.telemetry`). The per-ledger environment switches
still outrank the config in both directions, which is how a maintainer silences
one ledger on a dev box and how an operator asked for a trace of a slow install
produces one without becoming a developer.

The other half of this file is the line: **fault records are not telemetry.** A
capture skip, an ingest error, format drift, a verify failure — each says
conversations may not have been preserved, which is exactly the news an archive
owes an operator who has never heard of ``dev_mode``. Those keep recording, and a
regression that folded them into the same switch would be silent by construction.
"""

from __future__ import annotations

import json

import pytest

from thread_archive._config import save_config
from thread_archive._ops import ingest_errors, load_runs, telemetry
from thread_archive._retrieval import usage
from thread_archive._watcher import ingest_log
from thread_archive._web import metrics

SWITCHES = ("THREAD_ARCHIVE_USAGE_LOG", "THREAD_ARCHIVE_WEB_METRICS",
            "THREAD_ARCHIVE_INGEST_LOG", "THREAD_ARCHIVE_LOAD_LOG")


@pytest.fixture(autouse=True)
def _unpinned(monkeypatch):
    """Drop the suite-wide pin (see conftest) so these tests see real defaults."""
    for switch in SWITCHES:
        monkeypatch.delenv(switch, raising=False)


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── the switch ───────────────────────────────────────────────────────────────


def test_a_plain_install_records_nothing(archive_home) -> None:
    """No config at all — the state every install starts in."""
    assert telemetry.recording("THREAD_ARCHIVE_USAGE_LOG") is False
    assert usage.enabled() is False
    assert metrics.enabled() is False
    assert ingest_log.enabled(archive_home) is False


def test_dev_mode_turns_every_gated_ledger_on(archive_home) -> None:
    save_config({"dev_mode": True}, home=archive_home)
    assert usage.enabled() is True
    assert metrics.enabled() is True
    assert ingest_log.enabled(archive_home) is True


def test_dev_mode_is_strict_true(archive_home) -> None:
    """Same discipline as every other switch in config.json: a key holding the
    string ``"true"`` is not a switch, it is a config someone got wrong — and the
    failure here is silent accumulation rather than a visible error."""
    for value in ("true", "false", 1, None):
        save_config({"dev_mode": value}, home=archive_home)
        assert usage.enabled() is False, value


def test_an_unreadable_config_records_nothing(archive_home) -> None:
    """A config that cannot be parsed already fails closed for ingest. Telemetry
    follows it rather than falling back to the pre-config default, which would
    have a corrupt file quietly turn recording on."""
    (archive_home / "config.json").write_text("{ not json", encoding="utf-8")
    assert usage.enabled() is False


def test_the_env_switch_outranks_the_config_both_ways(archive_home, monkeypatch) -> None:
    # On, without a config: the operator who was asked for a trace of a slow
    # install and should not have to become a developer to produce one.
    monkeypatch.setenv("THREAD_ARCHIVE_USAGE_LOG", "1")
    assert usage.enabled() is True
    # Off, on a dev install: one ledger silenced without giving up the rest.
    save_config({"dev_mode": True}, home=archive_home)
    monkeypatch.setenv("THREAD_ARCHIVE_USAGE_LOG", "0")
    assert usage.enabled() is False
    assert metrics.enabled() is True  # the others are untouched


def test_the_switch_is_read_per_call_not_frozen(archive_home) -> None:
    """The daemons that write these ledgers run for weeks. If the answer were
    resolved once at import, turning recording on would mean restarting the
    watcher, the viewer and every MCP server before the slowness under
    investigation could be measured."""
    assert usage.enabled() is False
    save_config({"dev_mode": True}, home=archive_home)
    assert usage.enabled() is True


# ── what stops being written ─────────────────────────────────────────────────


def test_retrieval_calls_are_not_ledgered(archive_home) -> None:
    usage.record_search("what did we decide", params={"limit": 10}, hits=[])
    usage.record_read(42)
    usage.record_warm(duration_ms=1.0, stages={"embed_ms": 1.0})
    usage.record_refresh("graph", duration_ms=1.0)
    usage.record_serve({"kind": "serve"})
    assert _rows(archive_home / usage.LEDGER_FILE) == []


def test_a_real_search_leaves_no_row(archive_home) -> None:
    """Through the MCP tool, not the writer: the gate has to hold on the path the
    rows actually come from, including the contention sample it assembles."""
    from thread_archive import _api as ta
    from thread_archive._mcp.server import thread_search

    session = archive_home / "sess.jsonl"
    session.write_text("\n".join(json.dumps(line) for line in (
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": "hello gate"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "hi from the gate"}]}},
    )) + "\n", encoding="utf-8")
    ta.import_path(session)

    assert "hello gate" in thread_search("hello gate", limit=5)
    assert _rows(archive_home / usage.LEDGER_FILE) == []


def test_served_requests_are_not_ledgered(archive_home) -> None:
    metrics.record_request("/api/search", status=200, duration_ms=12.0, size=99)
    assert _rows(archive_home / metrics.LEDGER_FILE) == []


def test_ingest_passes_are_not_ledgered(archive_home) -> None:
    class _Probe:
        ran = True

        def as_record(self):
            return {"total_ms": 5.0}

    ingest_log.record_pass("claude_code", home=archive_home, probe=_Probe(), pass_ms=7.0)
    ingest_log.record_idle(home=archive_home, passes=60, total_ms=900.0, max_ms=45.0,
                           window_s=300.0, checked=1200)
    ingest_log.record_maintenance(home=archive_home, timings={"rebalance_ms": 3.0}, counts={})
    ingest_log.record_embed(home=archive_home, embedded=10, elapsed_ms=5.0, detail_ms={})
    assert _rows(archive_home / ingest_log.LEDGER_FILE) == []


# ── what keeps being written ─────────────────────────────────────────────────


def test_loads_still_record_on_a_plain_install(archive_home) -> None:
    """Deliberately outside the switch, both halves. The live state is the
    progress bar for a load that runs for hours on a cold archive, and the history
    is one row per load someone started — a handful over an install's life, which
    the viewer's own health page renders as what building this archive cost."""
    with load_runs.load_run("reindex", home=archive_home) as run:
        with run.phase("truth", total=10) as ph:
            ph.advance(10)

    (row,) = _rows(load_runs.ledger_path(archive_home))
    assert row["kind"] == "reindex" and row["status"] == "ok"
    state = json.loads(load_runs.state_path(archive_home).read_text(encoding="utf-8"))
    assert state["kind"] == "reindex" and state["status"] == "ok"


def test_ingest_faults_still_record(archive_home) -> None:
    """An ingest error means conversations are not being captured, and the
    harness prunes its transcripts on its own schedule regardless."""
    ingest_errors.reset_tally()
    ingest_errors.record(["claude_code: could not read /a/b.jsonl"], home=archive_home)
    assert _rows(archive_home / ingest_errors.LEDGER_FILE)


def test_capture_skips_still_record(archive_home) -> None:
    from thread_archive._importers import _skip_ledger

    _skip_ledger.record_skip("claude_code", "s1", lines_skipped=40, lines_total=40,
                             reason="empty_import_discarded")
    assert _rows(archive_home / _skip_ledger.LEDGER_FILE)


def test_format_drift_still_records(archive_home) -> None:
    """Drift is the one case where ``dev_mode`` already had a say — over whether
    a *warning* fires now or after a grace window. It never gated the record."""
    from thread_archive._importers import _validation_ledger

    _validation_ledger.record_drift(
        "claude_code", "s1", findings=["unmodeled field message.newField"],
        batch_safe=True, additive=True,
    )
    assert _rows(archive_home / _validation_ledger.LEDGER_FILE)
