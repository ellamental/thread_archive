"""The `thread_archive` setup/status flow: config persistence, the stat-only
discover pass, consent → import wiring, and the non-TTY safety stance (no
terminal + no --yes must never ingest as a side effect).

Fakes stand in for provider watchers throughout — the suite must never scan
or import this machine's real AI-tool stores — and the watcher/MCP offers are
exercised through their skip paths or monkeypatches, never real launchctl
installs or `claude mcp add` runs.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from thread_archive import _config as config
from thread_archive import _launchd
from thread_archive._setup import clients, wizard
from thread_archive._watcher import enabled_watchers, provider_watchers
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult
from thread_archive._watcher.sources import ClaudeCodeWatcher, cursor_watcher, grok_watcher

# ── config.json ──────────────────────────────────────────────────────────────


def test_load_config_absent_and_corrupt_mean_defaults(archive_home, caplog) -> None:
    # Absent is the normal pre-setup state: silent defaults.
    with caplog.at_level("ERROR", logger="thread_archive._config"):
        assert config.load_config() == {}
    assert not caplog.records
    # A corrupt file silently re-enabling every opted-out source would be a
    # privacy hazard — it must degrade to defaults BUT log at error.
    config.config_path().write_text("{not json")
    with caplog.at_level("ERROR", logger="thread_archive._config"):
        assert config.load_config() == {}
    assert any("ALL defaults" in r.message for r in caplog.records)
    caplog.clear()
    # A non-dict document also degrades to defaults rather than exploding.
    config.config_path().write_text('["list"]')
    with caplog.at_level("ERROR", logger="thread_archive._config"):
        assert config.load_config() == {}
    assert any("ALL defaults" in r.message for r in caplog.records)


def test_save_config_roundtrip(archive_home) -> None:
    cfg = {"sources": {"cursor": {"enabled": False}}, "setup": {"watcher": "skipped"}}
    path = config.save_config(cfg)
    assert path == archive_home / "config.json"
    assert config.load_config() == cfg
    assert not path.with_name(path.name + ".tmp").exists()  # atomic write cleaned up


def test_source_enabled_defaults_true() -> None:
    assert config.source_enabled({}, "cursor")
    assert config.source_enabled({"sources": {"cursor": {}}}, "cursor")
    assert not config.source_enabled({"sources": {"cursor": {"enabled": False}}}, "cursor")
    # Malformed entries degrade to enabled, never to a crash.
    assert config.source_enabled({"sources": {"cursor": "nope"}}, "cursor")


def test_enabled_watchers_respects_config(archive_home) -> None:
    all_names = {w.source_name for w in enabled_watchers()}
    assert {"claude-code", "cursor", "export-drop", "cc-exthost"} <= all_names
    config.save_config({"sources": {"cursor": {"enabled": False}, "cc-exthost": {"enabled": False}}})
    filtered = {w.source_name for w in enabled_watchers()}
    assert "cursor" not in filtered and "cc-exthost" not in filtered
    assert "claude-code" in filtered


def test_provider_watchers_excludes_mechanisms() -> None:
    names = {w.source_name for w in provider_watchers()}
    assert "export-drop" not in names and "cc-exthost" not in names
    assert "claude-code" in names


# ── discover ─────────────────────────────────────────────────────────────────


def test_file_watcher_discover_counts_and_ranges(tmp_path) -> None:
    projects = tmp_path / "projects"
    (projects / "proj1").mkdir(parents=True)
    old = projects / "proj1" / "a.jsonl"
    new = projects / "proj1" / "b.jsonl"
    old.write_text('{"x":1}\n' * 10)
    new.write_text('{"x":2}\n')
    (projects / "proj1" / "empty.jsonl").touch()  # zero bytes: not a session yet
    import os

    os.utime(old, (1_600_000_000, 1_600_000_000))
    report = ClaudeCodeWatcher(projects_dirs=[projects]).discover()
    assert report.name == "claude-code" and report.available
    assert report.items == 2
    assert report.bytes == old.stat().st_size + new.stat().st_size
    assert report.earliest == pytest.approx(1_600_000_000)
    assert report.latest >= report.earliest


def test_discover_absent_store(tmp_path) -> None:
    report = grok_watcher(sessions_dir=tmp_path / "nope").discover()
    assert not report.available and report.items == 0 and report.bytes == 0


def test_db_watcher_discover_size_no_count(tmp_path) -> None:
    db = tmp_path / "state.vscdb"
    db.write_bytes(b"\x00" * 2048)
    report = cursor_watcher(db_path=db).discover()
    assert report.available and report.items is None
    assert report.bytes == 2048 and report.latest is not None
    assert not cursor_watcher(db_path=tmp_path / "gone.db").discover().available


# ── the wizard ───────────────────────────────────────────────────────────────


class FakeWatcher(SourceWatcher):
    def __init__(self, name: str, *, available: bool = True, items: int = 3, events: int = 5):
        self._name, self._available = name, available
        self._items, self._events = items, events
        self.polled = 0

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def discover(self) -> SourceDiscovery:
        now = time.time()
        return SourceDiscovery(
            name=self._name, available=self._available, items=self._items,
            bytes=4096, earliest=now - 86400, latest=now,
        )

    def poll(self) -> WatchResult:
        self.polled += 1
        return WatchResult(sources_checked=1, items_imported=self._items, events_created=self._events)


def _args(*argv: str):
    return wizard.build_parser().parse_args(list(argv))


def test_non_tty_without_yes_does_no_work(archive_home, capsys) -> None:
    # Under pytest stdin/stdout are not TTYs, so the bare fresh-home invocation
    # must land on guidance, import nothing, and create no index.
    assert wizard.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing was imported" in out
    assert not (archive_home / "index.db").exists()


def test_yes_setup_imports_and_records(archive_home, capsys) -> None:
    fakes = [FakeWatcher("claude-code"), FakeWatcher("codex", available=False)]
    rc = wizard.run_setup(_args("setup", "--yes", "--skip-watcher", "--skip-backup", "--skip-mcp"), watchers=fakes)
    assert rc == 0
    assert fakes[0].polled == 1 and fakes[1].polled == 0  # only the found store imports
    out = capsys.readouterr().out
    assert "Scanning for conversation stores" in out
    assert "Importing" in out and "5 events" in out
    assert "not found: Codex" in out
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg["setup"]["completed_at"]
    assert cfg["setup"]["watcher"] == "skipped" and cfg["setup"]["clients"]["claude"] == "skipped"
    assert "claude-code" not in cfg.get("sources", {})  # enabled = unlisted


def test_edit_selection_persists_opt_outs(archive_home, monkeypatch, capsys) -> None:
    answers = iter(["e", "n", "y"])  # edit; drop claude-code; keep cursor
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    rc = wizard.run_setup(
        _args("setup", "--skip-watcher", "--skip-backup", "--skip-mcp"), watchers=fakes,
        interactive=True,
        ask=lambda prompt, *, default, interactive: next(answers, default),
    )
    assert rc == 0
    assert fakes[0].polled == 0 and fakes[1].polled == 1
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg["sources"]["claude-code"] == {"enabled": False}
    # cc-exthost recovers claude-code steering: it follows that source's choice.
    assert cfg["sources"]["cc-exthost"] == {"enabled": False}
    assert "cursor" not in cfg["sources"]
    # And the ingest paths see the choice.
    assert "claude-code" not in {w.source_name for w in enabled_watchers()}


def test_edit_to_zero_disables_every_source(archive_home, monkeypatch, capsys) -> None:
    # Deselecting each source in the edit pass is an explicit opt-out for all of
    # them — unlike a wholesale "skip import", which leaves sources enabled.
    answers = iter(["e", "n", "n"])
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    rc = wizard.run_setup(
        _args("setup", "--skip-watcher", "--skip-backup", "--skip-mcp"), watchers=fakes,
        interactive=True,
        ask=lambda prompt, *, default, interactive: next(answers, default),
    )
    assert rc == 0
    assert fakes[0].polled == 0 and fakes[1].polled == 0
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg["sources"]["claude-code"] == {"enabled": False}
    assert cfg["sources"]["cursor"] == {"enabled": False}
    assert cfg["sources"]["cc-exthost"] == {"enabled": False}


def test_skip_import_keeps_sources_enabled(archive_home, capsys) -> None:
    fakes = [FakeWatcher("claude-code")]
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-backup", "--skip-mcp"), watchers=fakes
    )
    assert rc == 0 and fakes[0].polled == 0
    cfg = json.loads((archive_home / "config.json").read_text())
    assert "claude-code" not in cfg.get("sources", {})  # skipping import ≠ disabling


def test_bare_rerun_lands_on_status(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: False)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: False)
    wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-backup", "--skip-mcp"), watchers=[]
    )
    capsys.readouterr()
    assert wizard.main([]) == 0
    out = capsys.readouterr().out
    assert "archive status" in out
    assert "thread_archive setup" in out  # the re-entry hint


def test_status_command(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: False)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: False)
    assert wizard.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "0 conversations" in out and "all enabled" in out
    if sys.platform == "darwin":
        assert "no nightly backup" in out  # the promoted CTA, not buried in passing


# ── the nightly-backup offer ──────────────────────────────────────────────────
#
# Never a real launchctl install: install_backup is monkeypatched to record its
# args, and _backup_running / sys.platform are forced so the flow is exercised on
# any host (the offer is macOS-only in production).


def _force_darwin(monkeypatch) -> None:
    monkeypatch.setattr(wizard.sys, "platform", "darwin")


def test_offer_backup_skipped_flag(archive_home) -> None:
    out = wizard._offer_backup(_args("setup", "--skip-backup"), interactive=False)
    assert out == {"status": "skipped"}


def test_offer_backup_installs_from_dest_flag(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: False)
    recorded = {}
    monkeypatch.setattr(
        _launchd, "install_backup",
        lambda dest, home=None, **kw: recorded.update(dest=dest, home=home),
    )
    out = wizard._offer_backup(
        _args("setup", "--yes", "--backup-dest", "/Volumes/Backup/arc"), interactive=False
    )
    assert out == {"status": "launchd", "dest": "/Volumes/Backup/arc"}
    assert recorded["dest"] == "/Volumes/Backup/arc"
    assert "Scheduled" in capsys.readouterr().out


def test_offer_backup_already_installed_is_left_alone(archive_home, monkeypatch, capsys) -> None:
    # The guard that stops a wizard re-run from clobbering an operator-installed
    # pipeline (e.g. the host/ NAS backup): a loaded agent → no install call.
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: True)
    monkeypatch.setattr(_launchd, "backup_agent_dest", lambda: "/Volumes/NAS/arc")
    called = []
    monkeypatch.setattr(
        _launchd, "install_backup", lambda *a, **k: called.append(a)
    )
    out = wizard._offer_backup(_args("setup", "--yes"), interactive=False)
    assert out == {"status": "already-installed", "dest": "/Volumes/NAS/arc"}
    assert not called  # never re-installed over the existing agent
    assert "already installed" in capsys.readouterr().out


def test_offer_backup_yes_without_dest_skips(archive_home, monkeypatch) -> None:
    # --yes has no destination to invent: it must skip, never install to a guess.
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: False)
    monkeypatch.setattr(
        _launchd, "install_backup",
        lambda *a, **k: pytest.fail("must not install without a dest"),
    )
    out = wizard._offer_backup(_args("setup", "--yes"), interactive=False)
    assert out == {"status": "skipped"}


def test_offer_backup_non_darwin_is_unavailable(archive_home, monkeypatch) -> None:
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    out = wizard._offer_backup(_args("setup"), interactive=False)
    assert out == {"status": "unavailable"}


def test_setup_records_backup_outcome(archive_home, monkeypatch, capsys) -> None:
    # End to end through run_setup: the offer's verdict lands in config under
    # setup.backup, the sibling of setup.watcher.
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-mcp"),
        watchers=[],
        offer_backup=lambda args, interactive: {"status": "skipped"},
    )
    assert rc == 0
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg["setup"]["backup"] == {"status": "skipped"}


# ── client wiring ────────────────────────────────────────────────────────────


def test_mcp_config_block_names_read_server_only() -> None:
    # The librarian write server is the archive-librarian plugin's to wire —
    # the core wizard must not hand curation power to every client it touches.
    block = json.loads(clients.mcp_config_block())
    servers = block["mcpServers"]
    assert set(servers) == {"thread-archive"}
    assert servers["thread-archive"]["command"].endswith("archive-mcp")


def test_setup_prints_config_when_no_client_found(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(clients, "claude_cli", lambda: None)
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-backup"), watchers=[]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcpServers" in out and "thread-archive" in out
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg["setup"]["clients"]["claude"] == "printed"


# ── formatting ───────────────────────────────────────────────────────────────


def test_fmt_helpers() -> None:
    assert wizard._fmt_bytes(0) == "0 B"
    assert wizard._fmt_bytes(2048) == "2.0 KB"
    assert wizard._fmt_bytes(3 * 1024**3) == "3.0 GB"
    assert wizard._fmt_when(None) == "?"
    assert wizard._fmt_when(time.time() - 60) == "today"
    assert wizard._fmt_range(None, None) == ""


def test_wholesale_import_skip_writes_no_opt_outs(archive_home, monkeypatch) -> None:
    """Skipping the import step entirely is not an opt-out: every discovered
    source must stay enabled (unlisted) for later ingest paths. Only an
    explicit per-source "no" in the edit pass may write enabled=False —
    pinned here so a wizard regression can't silently mass-disable capture."""
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    rc = wizard.run_setup(
        _args("setup", "--skip-watcher", "--skip-backup", "--skip-mcp"), watchers=fakes,
        interactive=True,
        ask=lambda prompt, *, default, interactive: "s",
    )
    assert rc == 0
    assert all(f.polled == 0 for f in fakes)  # nothing imported
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg.get("sources", {}) == {}  # no opt-outs written; absence = enabled
