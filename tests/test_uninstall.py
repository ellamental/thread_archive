"""``thread-archive uninstall``: what it takes off the machine, and what it must
never touch.

Driven against the **real** removal machinery — the real launchd backend writing
and unlinking real plists, the real ``_setup.clients`` bodies running real argv,
a real ``config.json``, a real archive with real conversations in it. The
operating system is contained rather than faked: ``$HOME`` is redirected per test
so plists land in ``tmp_path``, and ``$PATH`` is pinned (autouse) to a directory
holding only the ``launchctl`` / ``claude`` stand-ins the test wrote, so a
subprocess the product spawns by name can only ever reach a stand-in — the
operator's live agents and MCP config stay out of reach even from a test that
writes none.

The load-bearing assertion in most of these is the negative one: after a full
uninstall the conversations, the index and the source policy are byte-for-byte
what they were. An uninstall that removed data would pass every "is it gone?"
check in this file and still be the one unacceptable outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thread_archive import _service
from thread_archive._config import load_config, save_config
from thread_archive._service import launchd as _launchd
from thread_archive._service.launchd import BACKUP_LABEL, MCP_LABEL, WATCHER_LABEL, _plist_path
from thread_archive._setup import clients, uninstall
from thread_archive._setup.machine import Machine
from thread_archive.cli import main

from .helpers import import_cc_session, one_thread_file
from .test_cov_setup import _calls, _claude_stub, _darwin, _force_platform, _launchctl_stub

# coverage tag: setup


@pytest.fixture(autouse=True)
def stub_bin(tmp_path, monkeypatch) -> Path:
    """``$PATH``, pinned to a directory that starts out empty."""
    b = tmp_path / "stub-bin"
    b.mkdir()
    monkeypatch.setenv("PATH", str(b))
    return b


@pytest.fixture
def host(tmp_path, monkeypatch, stub_bin):
    """A macOS host whose LaunchAgents dir is under ``tmp_path`` and whose
    ``launchctl`` is a stand-in — so installs and removals are the real code
    paths over real files."""
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    _launchctl_stub(stub_bin, {"print": (0, "state = running", "")})
    return tmp_path / "host-home"


# The `claude mcp get` shapes the CLI prints for the entry setup writes, and for
# entries an uninstall must keep its hands off.
def _entry(scope: str = "User", home: str | None = None, pending: bool = False) -> str:
    lines = [
        "thread-archive:",
        f"  Scope: {scope} config",
        f"  Status: {'⏸ Pending approval' if pending else '✔ Connected'}",
        "  Type: stdio",
        "  Command: /venv/bin/archive-mcp",
    ]
    if home:
        lines.append(f"  Environment: THREAD_ARCHIVE_HOME={home}")
    return "\n".join(lines)


def _claude_config(stub_bin, tmp_path, entry: str, *, remove: str = "ok") -> Path:
    """A ``claude`` stand-in over a real one-entry config file: ``mcp get`` reads
    it, ``mcp remove`` deletes it. So the flow's post-removal re-probe asks the
    same question of a store the removal actually changed, rather than of a
    canned answer that can only ever say one thing.

    ``remove`` scripts that verb: ``ok`` deletes, ``error`` refuses, ``silent``
    reports success and removes nothing (a CLI that lies about what it did).
    Returns the argv log."""
    log = tmp_path / "claude.log"
    entry_file = tmp_path / "claude-entry.txt"
    entry_file.write_text(entry, encoding="utf-8")
    arms = {
        "ok": f'rm -f "{entry_file}"; echo Removed; exit 0',
        "error": 'echo "Error: no server thread-archive in user config" >&2; exit 1',
        "silent": "echo Removed; exit 0",
    }[remove]
    script = "\n".join([
        "#!/bin/sh",
        # $PATH is pinned to the stand-in dir for the product's sake; this script
        # still needs the two non-builtins it reads and writes its store with.
        "PATH=/bin:/usr/bin",
        f'echo "$*" >> "{log}"',
        'case "$1 $2" in',
        f'  "mcp get") test -f "{entry_file}" || exit 1; cat "{entry_file}"; exit 0 ;;',
        f'  "mcp remove") {arms} ;;',
        "  *) exit 0 ;;",
        "esac",
    ]) + "\n"
    exe = stub_bin / "claude"
    exe.write_text(script, encoding="utf-8")
    exe.chmod(0o755)
    return log


def _args(home, *, yes: bool = True, dry_run: bool = False):
    import argparse

    return argparse.Namespace(home=str(home), yes=yes, dry_run=dry_run)


def _install_all(home) -> None:
    """The full set of agents this host runs for ``home``."""
    _service.install_watcher(str(home))
    _service.install_mcp(str(home))
    _service.install_backup("/tmp/mirror", str(home))


def _heartbeat(tmp_path, monkeypatch) -> Path:
    """A family-monitor heartbeat, where the archive stamps one."""
    from thread_archive._ops.health import heartbeat_path

    monkeypatch.setenv("THREAD_ARCHIVE_HEARTBEAT_DIR", str(tmp_path / "family-logs"))
    beat = heartbeat_path()
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text('{"ok": true}\n', encoding="utf-8")
    return beat


# ── the survey ───────────────────────────────────────────────────────────────


def test_bare_machine_has_nothing_to_remove(archive_home, host, capsys) -> None:
    items = uninstall.survey(str(archive_home), machine=Machine())
    assert not [i for i in items if i.removable]
    assert {i.name for i in items} == {
        "watcher", "mcp", "backup", "claude", "product", "nightly", "setup"
    }

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0
    assert "Nothing installed" in capsys.readouterr().out


def test_survey_reads_the_manifest_not_the_process(archive_home, host) -> None:
    """An agent whose process is down is still scheduled — and still ours to
    remove. The probe asks the manifest on disk."""
    _service.install_watcher(str(archive_home))
    machine = Machine()
    assert machine.agent_installed("watcher") and not machine.agent_installed("mcp")
    assert machine.agent_home("watcher") == str(archive_home)
    assert machine.agent_covers_home("watcher", str(archive_home))
    assert not machine.agent_covers_home("watcher", str(archive_home) + "-other")

    watcher = next(i for i in uninstall.survey(str(archive_home)) if i.name == "watcher")
    assert watcher.present and watcher.removable and watcher.state == "installed"


# ── the removal ──────────────────────────────────────────────────────────────


def test_uninstall_removes_the_machinery_and_keeps_every_conversation(
    tmp_path, archive_home, host, monkeypatch, stub_bin, capsys
) -> None:
    import_cc_session(tmp_path, "kept")
    truth = one_thread_file(archive_home)
    before = truth.read_bytes()
    index_before = (archive_home / "index.db").read_bytes()

    _install_all(archive_home)
    claude_log = _claude_config(stub_bin, tmp_path, _entry(home=str(archive_home)))
    manifest = archive_home / "product.json"
    manifest.write_text('{"name": "thread-archive"}\n', encoding="utf-8")
    beat = _heartbeat(tmp_path, monkeypatch)
    save_config({"sources": {"cursor": {"enabled": False}},
                 "setup": {"watcher": "launchd", "completed_at": "2026-07-01T00:00:00"}},
                str(archive_home))

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0

    # Gone: every piece of machine footprint.
    for label in (WATCHER_LABEL, MCP_LABEL, BACKUP_LABEL):
        assert not _plist_path(label).exists()
    assert not manifest.exists() and not beat.exists()
    assert ["mcp", "remove", "--scope", "user", "thread-archive"] in _calls(claude_log)

    # Kept: the archive itself, byte for byte — and the source policy with it.
    assert truth.read_bytes() == before
    assert (archive_home / "index.db").read_bytes() == index_before
    cfg = load_config(str(archive_home))
    assert cfg["sources"] == {"cursor": {"enabled": False}}
    assert "setup" not in cfg

    out = capsys.readouterr().out
    assert "Uninstalled." in out
    assert str(archive_home) in out  # the archive is named, never deleted


def test_agents_serving_another_archive_are_left_alone(
    tmp_path, archive_home, host, monkeypatch, capsys
) -> None:
    other = tmp_path / "other-archive"
    _service.install_watcher(str(other))
    _service.install_backup("/tmp/mirror", str(other))
    beat = _heartbeat(tmp_path, monkeypatch)

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0

    # One label per user: removing these would stop a second archive's capture,
    # and the heartbeat is that archive's nightly report, not this one's.
    assert _plist_path(WATCHER_LABEL).exists() and _plist_path(BACKUP_LABEL).exists()
    assert beat.exists()
    out = capsys.readouterr().out
    assert "left alone" in out and str(other) in out
    assert "Nothing installed" in out  # nothing here was this archive's


def test_dry_run_reports_and_changes_nothing(archive_home, host, capsys) -> None:
    _install_all(archive_home)

    assert main(["uninstall", "--home", str(archive_home), "--dry-run"]) == 0

    assert _plist_path(WATCHER_LABEL).exists()
    out = capsys.readouterr().out
    assert "3 item(s) would be removed" in out and "Nothing was changed" in out


def test_no_terminal_and_no_yes_refuses(archive_home, host, capsys) -> None:
    _install_all(archive_home)

    # The wizard's non-TTY greeting exits 0; this is a refusal to act, so a
    # script that meant to uninstall must not read it as success.
    assert uninstall.run_uninstall(_args(archive_home, yes=False), interactive=False) == 2

    assert _plist_path(WATCHER_LABEL).exists()
    assert "No terminal to confirm in" in capsys.readouterr().out


def test_cancelling_at_the_prompt_removes_nothing(archive_home, host, capsys) -> None:
    _install_all(archive_home)

    code = uninstall.run_uninstall(
        _args(archive_home, yes=False), interactive=True,
        ask=lambda prompt, **kw: "s",
    )

    assert code == 0
    assert _plist_path(WATCHER_LABEL).exists()
    assert "Cancelled" in capsys.readouterr().out


def test_a_refused_removal_fails_the_run_and_reports_which(
    tmp_path, archive_home, host, stub_bin, capsys
) -> None:
    _service.install_watcher(str(archive_home))
    _claude_config(stub_bin, tmp_path, _entry(home=str(archive_home)), remove="error")

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 1

    out = capsys.readouterr().out
    assert "FAILED" in out and "the claude MCP wiring" in out
    assert "no server thread-archive in user config" in out
    assert "Uninstall incomplete" in out
    # The rest still went: a failure on one item is not a reason to strand the others.
    assert not _plist_path(WATCHER_LABEL).exists()
    assert "removed the always-on watcher" in out


def test_an_entry_that_survives_its_own_removal_is_not_reported_as_removed(
    tmp_path, archive_home, host, stub_bin, capsys
) -> None:
    """``mcp remove`` exiting 0 while the entry still answers is the one outcome
    that must not print as done."""
    _claude_config(stub_bin, tmp_path, _entry(home=str(archive_home)), remove="silent")

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 1
    assert "claude still has an entry for this archive" in capsys.readouterr().out


def test_a_home_it_cannot_write_reports_what_it_could_not_remove(
    archive_home, host, capsys
) -> None:
    """A removal the filesystem refuses is reported, not raised — and the items
    it could take still go."""
    (archive_home / "product.json").write_text("{}", encoding="utf-8")
    save_config({"setup": {"watcher": "launchd"}}, str(archive_home))
    _service.install_watcher(str(archive_home))
    archive_home.chmod(0o500)  # readable, walkable, not writable
    try:
        assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 1
        out = capsys.readouterr().out
        assert "2 item(s) could not be removed" in out
        assert "the family manifest" in out and "the install record" in out
        assert not _plist_path(WATCHER_LABEL).exists()  # the agent still went
    finally:
        archive_home.chmod(0o700)
    assert (archive_home / "product.json").exists()
    assert load_config(str(archive_home))["setup"] == {"watcher": "launchd"}


def test_a_host_with_no_service_manager_reports_no_agents(
    archive_home, tmp_path, monkeypatch, capsys
) -> None:
    """Nothing schedules here, so nothing is scheduled — the survey says so
    rather than asking a service manager that isn't there."""
    _force_platform(monkeypatch, _launchd, "sunos5")
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))

    items = {i.name: i for i in uninstall.survey(str(archive_home))}
    assert not any(items[a].present for a in ("watcher", "mcp", "backup"))
    assert items["watcher"].detail == "watcher"  # no platform id to name it by
    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0
    assert "Nothing installed" in capsys.readouterr().out


def test_an_unreadable_config_is_never_rewritten(archive_home, host) -> None:
    """A config that can't be parsed states nothing about an install, and an
    uninstall is not the moment to overwrite the one file holding the operator's
    source choices."""
    (archive_home / "config.json").write_text("{not json", encoding="utf-8")
    _service.install_watcher(str(archive_home))

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0
    assert (archive_home / "config.json").read_text(encoding="utf-8") == "{not json"


# ── the claude wiring ────────────────────────────────────────────────────────


def test_claude_removable_takes_only_setups_own_entry(
    archive_home, host, stub_bin
) -> None:
    def probe(stdout: str, rc: int = 0):
        _claude_stub(stub_bin, {"mcp get": (rc, stdout, "")})
        return clients.claude_removable(clients.claude_cli(), home=str(archive_home))

    # Setup's own wiring: user scope, this archive.
    assert probe(_entry(home=str(archive_home))) == (True, None)

    # A project's wiring lives in a checkout's .mcp.json — not the archive's file.
    present, why = probe(_entry(scope="Project"))
    assert present and "project-scope" in why

    # An entry for a different archive belongs to that install.
    present, why = probe(_entry(home=str(archive_home) + "-other"))
    assert present and "not this archive" in why

    # A shape whose scope can't be read is left alone rather than guessed at.
    present, why = probe("thread-archive:\n  Type: stdio\n")
    assert present and "unrecognized scope" in why

    # No entry at all.
    assert probe("", rc=1) == (False, None)


def test_unwire_is_scoped_to_what_wiring_wrote(host, stub_bin) -> None:
    log = _claude_stub(stub_bin, {"mcp remove": (0, "Removed", "")})
    assert clients.unwire_claude(clients.claude_cli()) == []
    assert _calls(log) == [["mcp", "remove", "--scope", "user", "thread-archive"]]


def test_unwire_reports_a_missing_cli(host, tmp_path) -> None:
    errors = clients.unwire_claude(str(tmp_path / "no-such-claude"))
    assert errors and "thread-archive" in errors[0]


# ── the closing report ───────────────────────────────────────────────────────


def test_the_closing_report_names_every_place_the_data_is(
    tmp_path, archive_home, host, capsys
) -> None:
    """Deleting the home is not deleting the conversations, on most installs. The
    report has to name each copy, or "untouched" is the last thing someone reads
    before they believe one directory was all of it."""
    import_cc_session(tmp_path, "kept")
    second = tmp_path / "second-mirror"
    second.mkdir()
    (archive_home / "health.json").write_text(json.dumps({
        # Two destinations, recorded by different stages — the older one is
        # exactly the copy someone has forgotten they have.
        "backup_last": {"dest": "/Volumes/Backup/arc", "ok": True},
        "backup_hashes_last": {"dest": str(second)},
        "verify_last": {"ok": True},  # no dest: not a location
    }), encoding="utf-8")
    aside = archive_home.parent / f"{archive_home.name}.damaged-20260101T000000"
    aside.mkdir()  # what `restore --replace` preserves
    host.mkdir(parents=True, exist_ok=True)
    (host / ".thread_archive").symlink_to(archive_home)
    _service.install_watcher(str(archive_home))

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0

    out = capsys.readouterr().out
    assert "conversations, index, config, logs, exports" in out
    assert str(archive_home) in out
    assert "/Volumes/Backup/arc" in out and "not present right now" in out
    assert str(second) in out
    assert str(aside) in out
    assert f"a symlink to {archive_home}" in out  # a second name, not a second copy
    assert "Deleting any of it is yours to do" in out
    # And how to be rid of the code, which is a different sentence per install
    # shape — a clone is a directory to delete, a wheel is a pip uninstall. The
    # suite runs from both (tests/install/ runs it from a wheel), so assert the
    # one that matches the shape rather than assuming the checkout.
    from thread_archive._update import source_checkout

    if source_checkout() is not None:
        assert "this install runs from the clone" in out
    else:
        assert "pip uninstall thread-archive" in out
    assert "thread-archive setup" in out


def test_a_backup_scheduled_but_never_run_is_still_named(
    archive_home, host, capsys
) -> None:
    """The agent's manifest is the only record of a nightly that has not fired
    yet — and this run removes it, so the dest is read before that happens."""
    _service.install_backup("/Volumes/Nightly/arc", str(archive_home))

    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0

    assert "/Volumes/Nightly/arc" in capsys.readouterr().out
    assert not _plist_path(BACKUP_LABEL).exists()


def test_an_empty_home_reports_as_empty(archive_home, host, capsys) -> None:
    assert main(["uninstall", "--home", str(archive_home), "--yes"]) == 0
    out = capsys.readouterr().out
    assert f"{archive_home}  (empty)" in out
    assert "a backup mirror" not in out
