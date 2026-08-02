"""The `thread_archive` setup/status flow: config persistence, the stat-only
discover pass, consent → import wiring, and the non-TTY safety stance (no
terminal + no --yes must never ingest as a side effect).

Fakes stand in for provider watchers throughout — the suite must never scan or
import this machine's real AI-tool stores — and the flow is handed a
:class:`FakeMachine` through its ``machine`` seam, so no step can install a
LaunchAgent or run `claude mcp add` on this box. ``$PATH`` is pinned per test
(autouse) to an empty directory as a second wall: a client CLI the flow looks
for by name cannot resolve to the operator's real one.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Optional

import pytest

from thread_archive import _config as config
from thread_archive._setup import clients, wizard
from thread_archive._watcher import enabled_watchers, provider_watchers
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult
from thread_archive._watcher.sources import ClaudeCodeWatcher, cursor_watcher, grok_watcher


@pytest.fixture(autouse=True)
def _no_real_client_cli(tmp_path, monkeypatch) -> None:
    """``$PATH``, pinned to an empty directory: the MCP-wiring step finds no
    client CLI to run rather than this machine's real one."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

# ── config.json ──────────────────────────────────────────────────────────────


def test_load_config_absent_defaults_but_corruption_fails_closed(archive_home, caplog) -> None:
    # Absent is the normal pre-setup state: silent defaults.
    with caplog.at_level("ERROR", logger="thread_archive._config"):
        cfg = config.load_config()
        assert cfg == {} and cfg.valid
    assert not caplog.records
    # A corrupt file must never silently re-enable an opted-out source.
    config.config_path().write_text("{not json")
    with caplog.at_level("ERROR", logger="thread_archive._config"):
        cfg = config.load_config()
        assert cfg == {} and not cfg.valid
        assert not config.source_enabled(cfg, "cursor")
    assert any("ingestion disabled" in r.message for r in caplog.records)
    caplog.clear()
    # A non-dict document also disables ingest rather than exploding.
    config.config_path().write_text('["list"]')
    with caplog.at_level("ERROR", logger="thread_archive._config"):
        cfg = config.load_config()
        assert cfg == {} and not cfg.valid
        assert not config.source_enabled(cfg, "claude-code")
    assert any("ingestion disabled" in r.message for r in caplog.records)


def test_malformed_source_policy_fails_closed(archive_home, caplog) -> None:
    for sources in (["cursor"], {"cursor": "nope"}, {"cursor": {"enabled": "no"}}):
        config.config_path().write_text(json.dumps({"sources": sources}))
        with caplog.at_level("ERROR", logger="thread_archive._config"):
            cfg = config.load_config()
        assert not cfg.valid
        assert not config.source_enabled(cfg, "cursor")
        assert not config.source_enabled(cfg, "claude-code")
    assert any("malformed sources policy" in r.message for r in caplog.records)


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
    # Malformed privacy policy fails closed, never to a crash.
    assert not config.source_enabled({"sources": {"cursor": "nope"}}, "cursor")
    assert not config.source_enabled({"sources": []}, "cursor")
    assert not config.source_enabled({"sources": {"cursor": {"enabled": "no"}}}, "cursor")


def test_enabled_watchers_respects_config(archive_home) -> None:
    all_names = {w.source_name for w in enabled_watchers()}
    assert {"claude-code", "cursor", "export-drop", "cc-exthost"} <= all_names
    config.save_config({"sources": {"cursor": {"enabled": False}, "cc-exthost": {"enabled": False}}})
    filtered = {w.source_name for w in enabled_watchers()}
    assert "cursor" not in filtered and "cc-exthost" not in filtered
    assert "claude-code" in filtered

    # Existing-but-corrupt config is a hard privacy stop for every ingest path.
    config.config_path().write_text("{not json")
    assert enabled_watchers() == []


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


class FakeMachine:
    """The host the wizard is run against: what is already scheduled on it,
    what this install has, and a record of everything setup asked it to
    install. Hand-written stand-in for
    :class:`~thread_archive._setup.machine.Machine`, passed through the flow's
    ``machine`` parameter."""

    def __init__(
        self, *, can_schedule: bool = True, service_kind: str = "launchd",
        watcher: bool = False, backup: bool = False,
        backup_dest: Optional[str] = None, embeddings: bool = True,
        viewer_ready: bool = True, browser: bool = True,
    ):
        self.can_schedule = can_schedule
        self.service_kind = service_kind if can_schedule else None
        self._watcher, self._backup = watcher, backup
        self._backup_dest = backup_dest
        self._embeddings = embeddings
        self._viewer_ready, self._browser = viewer_ready, browser
        self.installed: list[tuple] = []
        self.opened: list[str] = []

    def watcher_running(self, home=None) -> bool:
        return self._watcher

    def backup_running(self, home=None) -> bool:
        return self._backup

    def backup_dest(self) -> Optional[str]:
        return self._backup_dest

    def embeddings_installed(self) -> bool:
        return self._embeddings

    def viewer_ready(self, port: int) -> bool:
        return self._viewer_ready

    def install_watcher(self, home=None) -> None:
        self.installed.append(("watcher", home))

    def install_backup(self, dest, home=None) -> None:
        self.installed.append(("backup", dest, home))

    def open_browser(self, url: str) -> bool:
        self.opened.append(url)
        return self._browser


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
    """The namespace `thread-archive setup` hands the flow (see cli.build_parser)."""
    p = argparse.ArgumentParser()
    p.add_argument("command", nargs="?", choices=["setup", "status"], default=None)
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--home", default=None)
    p.add_argument("--skip-import", action="store_true")
    p.add_argument("--skip-watcher", action="store_true")
    p.add_argument("--skip-backup", action="store_true")
    p.add_argument("--backup-dest", default=None)
    p.add_argument("--skip-mcp", action="store_true")
    return p.parse_args(list(argv))


def test_non_tty_without_yes_does_no_work(archive_home, capsys) -> None:
    # Under pytest stdin/stdout are not TTYs, so the bare fresh-home invocation
    # must land on guidance, import nothing, and create no index.
    assert wizard.run_setup(_args()) == 0
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


# ── re-running setup over answers already given ───────────────────────────────
#
# An opt-out is the operator's standing answer, and `thread-archive setup` is
# meant to be re-run (it is how every choice is revisited). So a later run may
# only change the source policy where the operator states a new one — the edit
# pass. Anything else leaves it alone: a re-run that silently re-enables a
# source is capture they declined.


def _replies(*typed: str):
    """An ``ask`` seam that replays typed answers, ``""`` meaning Enter — the
    same default-substitution the real prompt does, so a test can press Enter.
    Every prompt it was asked is kept on ``.prompts`` (a fake ask never reaches
    stdout, so that is where the question's own wording is asserted)."""
    it = iter(typed)
    prompts: list[str] = []

    def ask(prompt, *, default, interactive):
        prompts.append(prompt)
        return next(it, "") or default

    ask.prompts = prompts  # type: ignore[attr-defined]
    return ask


def _setup(archive_home, fakes, ask, *, yes: bool = False) -> dict:
    argv = ["setup", "--skip-watcher", "--skip-backup", "--skip-mcp"]
    rc = wizard.run_setup(
        _args(*(argv + ["--yes"] if yes else argv)), watchers=fakes,
        interactive=not yes, ask=ask, machine=FakeMachine(),
    )
    assert rc == 0
    return json.loads((archive_home / "config.json").read_text())


def test_rerun_with_defaults_keeps_an_opt_out(archive_home, capsys) -> None:
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    _setup(archive_home, fakes, _replies("e", "n", "y"))  # edit; drop claude-code
    assert fakes[0].polled == 0 and fakes[1].polled == 1

    # Pressing Enter through a second run states nothing about policy: the
    # opt-out survives, and the disabled store is neither listed as included nor
    # imported.
    ask = _replies("")
    cfg = _setup(archive_home, fakes, ask)
    assert cfg["sources"]["claude-code"] == {"enabled": False}
    assert cfg["sources"]["cc-exthost"] == {"enabled": False}
    assert fakes[0].polled == 0 and fakes[1].polled == 2
    out = capsys.readouterr().out
    assert "[ ] Claude Code" in out and "(off — e to change)" in out
    assert "[x] Cursor" in out
    # And the offer says what Enter will actually do — not "import all".
    assert any("import the checked ones" in p for p in ask.prompts)


def test_rerun_skipping_the_import_keeps_an_opt_out(archive_home, capsys) -> None:
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    _setup(archive_home, fakes, _replies("e", "n", "y"))
    cfg = _setup(archive_home, fakes, _replies("s"))
    assert cfg["sources"]["claude-code"] == {"enabled": False}
    assert fakes[1].polled == 1  # "s" imports nothing at all this run


def test_rerun_with_yes_keeps_an_opt_out(archive_home, capsys) -> None:
    # --yes is how agents and scripts drive this flow; accepting every default
    # must not mean re-enabling a source the operator turned off.
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    _setup(archive_home, fakes, _replies("e", "n", "y"))
    cfg = _setup(archive_home, fakes, _replies(), yes=True)
    assert cfg["sources"]["claude-code"] == {"enabled": False}
    assert fakes[0].polled == 0 and fakes[1].polled == 2
    assert "claude-code" not in {w.source_name for w in enabled_watchers()}


def test_edit_pass_is_seeded_with_what_each_source_is_set_to(archive_home, capsys) -> None:
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    _setup(archive_home, fakes, _replies("e", "n", "y"))

    # Enter through the whole edit pass: each source keeps its current state, so
    # revisiting the selection to look at it changes nothing.
    cfg = _setup(archive_home, fakes, _replies("e", "", ""))
    assert cfg["sources"]["claude-code"] == {"enabled": False}
    assert "cursor" not in cfg["sources"]
    assert fakes[0].polled == 0 and fakes[1].polled == 2

    # An explicit yes is what lifts it — and the source comes back for every
    # ingest path, not just this run's import.
    cfg = _setup(archive_home, fakes, _replies("e", "y", ""))
    assert "claude-code" not in cfg["sources"]
    assert "cc-exthost" not in cfg["sources"]
    assert fakes[0].polled == 1 and fakes[1].polled == 3
    assert "claude-code" in {w.source_name for w in enabled_watchers()}


def test_rerun_with_every_source_off_offers_only_the_edit(archive_home, capsys) -> None:
    fakes = [FakeWatcher("claude-code"), FakeWatcher("cursor")]
    _setup(archive_home, fakes, _replies("e", "n", "n"))
    capsys.readouterr()

    ask = _replies("")
    cfg = _setup(archive_home, fakes, ask)
    assert cfg["sources"]["claude-code"] == {"enabled": False}
    assert cfg["sources"]["cursor"] == {"enabled": False}
    assert all(f.polled == 0 for f in fakes)
    assert "Every store found is switched off in your config" in capsys.readouterr().out
    # Nothing to skip: the only thing on offer is the edit that would turn one on.
    assert ask.prompts == ["  [Enter] leave them off · e = edit selection  > "]


def test_unreadable_policy_is_restated_by_the_run(archive_home, capsys) -> None:
    # A config that load_config can't trust disables every source for ingest —
    # and is exactly the file this run rewrites, so it seeds nothing: the flow
    # behaves like first contact and leaves a valid policy behind.
    config.config_path().write_text("{not json")
    fakes = [FakeWatcher("claude-code")]
    cfg = _setup(archive_home, fakes, _replies(""))
    assert cfg.get("sources", {}) == {}  # all enabled again, and readable
    assert fakes[0].polled == 1
    assert "(off — e to change)" not in capsys.readouterr().out


def test_skip_import_keeps_sources_enabled(archive_home, capsys) -> None:
    fakes = [FakeWatcher("claude-code")]
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-backup", "--skip-mcp"), watchers=fakes
    )
    assert rc == 0 and fakes[0].polled == 0
    cfg = json.loads((archive_home / "config.json").read_text())
    assert "claude-code" not in cfg.get("sources", {})  # skipping import ≠ disabling


def test_a_completed_home_reads_as_set_up_and_renders_status(archive_home, capsys) -> None:
    wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-backup", "--skip-mcp"),
        watchers=[], machine=FakeMachine(),
    )
    capsys.readouterr()
    assert wizard._setup_completed(_args()) is True  # setup stamped the config
    assert wizard.print_status(_args("status"), machine=FakeMachine()) == 0
    out = capsys.readouterr().out
    assert "thread_archive — status" in out  # the status header, not the setup flow
    assert "thread-archive setup" in out  # the re-entry hint


def test_status_command(archive_home, capsys) -> None:
    # A macOS host with nothing scheduled on it: the empty archive's counts plus
    # the promoted call to action for each missing agent.
    assert wizard.print_status(_args("status"), machine=FakeMachine()) == 0
    out = capsys.readouterr().out
    assert "0 conversations" in out and "all enabled" in out
    assert "watcher:  not running" in out
    assert "no nightly backup" in out  # the promoted CTA, not buried in passing


def test_status_builds_its_own_machine(archive_home, capsys) -> None:
    # `thread-archive status` on this host, whatever kind it is: the flow builds
    # its own Machine and the status view renders.
    assert wizard.print_status(_args("status")) == 0
    out = capsys.readouterr().out
    assert "0 conversations" in out and "all enabled" in out


# ── the nightly-backup offer ──────────────────────────────────────────────────
#
# Never a real launchctl install: the offer is given a FakeMachine, which records
# what setup asked it to install and answers the macOS gate, so the flow is
# exercised on any host (the offer is macOS-only in production).


def test_offer_backup_skipped_flag(archive_home) -> None:
    machine = FakeMachine()
    out = wizard._offer_backup(_args("setup", "--skip-backup"), False, machine)
    assert out == {"status": "skipped"}
    assert machine.installed == []


def test_offer_backup_installs_from_dest_flag(archive_home, capsys) -> None:
    machine = FakeMachine()
    out = wizard._offer_backup(
        _args("setup", "--yes", "--backup-dest", "/Volumes/Backup/arc"), False, machine
    )
    assert out == {"status": "launchd", "dest": "/Volumes/Backup/arc"}
    assert machine.installed == [("backup", "/Volumes/Backup/arc", None)]
    assert "Scheduled" in capsys.readouterr().out


def test_offer_backup_already_installed_is_left_alone(archive_home, capsys) -> None:
    # The guard that stops a wizard re-run from clobbering an operator-installed
    # pipeline (e.g. the host/ NAS backup): a loaded agent → no install call.
    machine = FakeMachine(backup=True, backup_dest="/Volumes/NAS/arc")
    out = wizard._offer_backup(_args("setup", "--yes"), False, machine)
    assert out == {"status": "already-installed", "dest": "/Volumes/NAS/arc"}
    assert machine.installed == []  # never re-installed over the existing agent
    assert "already installed" in capsys.readouterr().out


def test_offer_backup_yes_without_dest_skips(archive_home) -> None:
    # --yes has no destination to invent: it must skip, never install to a guess.
    machine = FakeMachine()
    out = wizard._offer_backup(_args("setup", "--yes"), False, machine)
    assert out == {"status": "skipped"}
    assert machine.installed == []


def test_offer_backup_no_service_manager_is_unavailable(archive_home) -> None:
    out = wizard._offer_backup(_args("setup"), False, FakeMachine(can_schedule=False))
    assert out == {"status": "unavailable"}


def test_setup_records_backup_outcome(archive_home, capsys) -> None:
    # End to end through run_setup: the offer's verdict lands in config under
    # setup.backup, the sibling of setup.watcher. Nothing to schedule to (no
    # --backup-dest, nothing to prompt with) → the offer skips.
    machine = FakeMachine()
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-mcp"),
        watchers=[], machine=machine,
    )
    assert rc == 0
    assert machine.installed == []
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg["setup"]["backup"] == {"status": "skipped"}


# ── the viewer offer ──────────────────────────────────────────────────────────
#
# Never a real browser or a real socket: the offer asks its FakeMachine whether
# the viewer answers and hands it the URL to open, so a test can't put a window
# on the operator's screen or reach a watcher actually serving :8787 here.


def test_offer_viewer_opens_the_url(archive_home, capsys) -> None:
    machine = FakeMachine()
    assert wizard._offer_viewer(
        True, machine, ask=lambda prompt, *, default, interactive: "",
    ) == "opened"
    assert machine.opened == ["http://127.0.0.1:8787"]
    assert "Opened http://127.0.0.1:8787" in capsys.readouterr().out


def test_offer_viewer_skip_answer(archive_home, capsys) -> None:
    machine = FakeMachine()
    assert wizard._offer_viewer(
        True, machine, ask=lambda prompt, *, default, interactive: "s",
    ) == "skipped"
    assert machine.opened == []  # a skip opens nothing
    assert "http://127.0.0.1:8787 whenever you want it" in capsys.readouterr().out


def test_offer_viewer_not_offered_without_a_terminal(archive_home, capsys) -> None:
    # --yes drives this flow from agents and scripts: no prompt, and above all no
    # browser window on someone's desktop as a side effect.
    machine = FakeMachine()
    asked: list[str] = []
    assert wizard._offer_viewer(
        False, machine,
        ask=lambda prompt, *, default, interactive: asked.append(prompt) or "",
    ) == "not-offered"
    assert asked == [] and machine.opened == []
    assert capsys.readouterr().out == ""


def test_offer_viewer_waits_for_a_viewer_that_never_answers(archive_home, capsys) -> None:
    # The watcher was installed seconds ago; if its viewer still isn't up, say so
    # rather than opening a browser onto a refused connection.
    machine = FakeMachine(viewer_ready=False)
    assert wizard._offer_viewer(
        True, machine, ask=lambda prompt, *, default, interactive: "",
    ) == "unavailable"
    assert machine.opened == []
    assert "Not answering yet" in capsys.readouterr().out


def test_offer_viewer_headless_host_prints_the_url(archive_home, capsys) -> None:
    machine = FakeMachine(browser=False)
    assert wizard._offer_viewer(
        True, machine, ask=lambda prompt, *, default, interactive: "",
    ) == "failed"
    assert "No browser to open here — visit http://127.0.0.1:8787" in capsys.readouterr().out


@pytest.mark.viewer
def test_setup_ends_in_the_viewer_and_records_it(archive_home, capsys) -> None:
    # End to end: the watcher install serves the viewer, so first contact ends in
    # a browser and the verdict lands in config beside the other setup choices.
    machine = FakeMachine()
    rc = wizard.run_setup(
        _args("setup", "--skip-import", "--skip-backup", "--skip-mcp"),
        watchers=[], machine=machine, interactive=True,
        ask=lambda prompt, *, default, interactive: "",
    )
    assert rc == 0
    assert machine.installed == [("watcher", None)]
    assert machine.opened == ["http://127.0.0.1:8787"]
    cfg = json.loads((archive_home / "config.json").read_text())
    assert cfg["setup"]["viewer"] == "opened"
    assert cfg["setup"]["completed_at"]


def test_setup_without_a_watcher_makes_no_viewer_offer(archive_home, capsys) -> None:
    # Nothing is serving :8787, so there is nothing to open — and no dead prompt.
    machine = FakeMachine()
    rc = wizard.run_setup(
        _args("setup", "--skip-import", "--skip-watcher", "--skip-backup", "--skip-mcp"),
        watchers=[], machine=machine, interactive=True,
        ask=lambda prompt, *, default, interactive: "",
    )
    assert rc == 0
    assert machine.opened == []
    cfg = json.loads((archive_home / "config.json").read_text())
    assert "viewer" not in cfg["setup"]
    assert "See it?" not in capsys.readouterr().out


# ── client wiring ────────────────────────────────────────────────────────────


def test_mcp_config_block_names_read_server_only() -> None:
    # The write server is an external plugin's to wire —
    # the core wizard must not hand write power to every client it touches.
    block = json.loads(clients.mcp_config_block())
    servers = block["mcpServers"]
    assert set(servers) == {"thread-archive"}
    assert servers["thread-archive"]["command"].endswith("archive-mcp")
    assert servers["thread-archive"]["env"]["THREAD_ARCHIVE_MCP_INGEST"] == "1"


def test_setup_prints_config_when_no_client_found(archive_home, capsys) -> None:
    # PATH holds no client CLI, so the MCP step has nothing to wire and must
    # still leave the operator a config block they can paste anywhere.
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-watcher", "--skip-backup"),
        watchers=[], machine=FakeMachine(),
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
