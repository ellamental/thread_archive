"""Branch-coverage tests for the ``thread_archive`` CLI (:mod:`thread_archive.cli`).

Companion to ``test_cli_smoke.py``, which drives the verbs end-to-end over a
seeded archive. This file covers the branches a real run cannot reach, two ways:

* the ``report_*`` functions — each verb's operator report is a pure function of
  the result dict and its exit code. One test per report drives the richest
  failure shape that report prints, holding the exit-code contract and the main
  reporting surface; individual warning/sample lines are not enumerated;
* the verbs whose boundary is the operating system — the ``daemon`` LaunchAgent
  lifecycle and the watcher's long-running loop — which run for real against a
  redirected ``$HOME``: ``$PATH`` is pinned to a directory holding only the
  ``launchctl`` stand-in the test wrote, and the loop is stopped by a
  ``KeyboardInterrupt`` — the operator's ^C — injected once it is observably
  running.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import plistlib
import pty
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pytest

from thread_archive import _update, cli
from thread_archive._service import launchd as _launchd
from thread_archive._service.launchd import BACKUP_LABEL, MCP_LABEL, WATCHER_LABEL
from thread_archive.cli import main

from .helpers import (
    cc_assistant,
    cc_user,
    event_count,
    import_cc_session,
    one_thread_file,
    write_jsonl,
)
from .test_truth_migrate_v2 import make_legacy_home

# coverage tag: cli


# ── stand-in executables ─────────────────────────────────────────────────────


@pytest.fixture
def stub_bin(tmp_path, monkeypatch) -> Path:
    """``$PATH``, pinned to a directory that starts out empty.

    A ``launchctl`` the ``daemon`` verbs spawn by name then resolves to the
    stand-in the test wrote — or to nothing at all — so the real ``_launchd``
    bodies run over real argv, real exit codes and real stdout parsing while the
    operator's live agents stay out of reach.
    """
    b = tmp_path / "stub-bin"
    b.mkdir()
    monkeypatch.setenv("PATH", str(b))
    return b


def _launchctl_stub(
    stub_bin: Path, results: Optional[dict[str, tuple[int, str, str]]] = None
) -> Path:
    """A ``launchctl`` stand-in on PATH, scripted per subcommand
    (``{subcommand: (returncode, stdout, stderr)}``; anything unlisted exits 0).

    Returns the log it appends one line of arguments to per invocation.
    """
    log = stub_bin.parent / "launchctl.log"
    arms = []
    for pattern, (rc, out, err) in (results or {}).items():
        body = []
        if out:
            body.append(f'printf "%s\\n" "{out}"')
        if err:
            body.append(f'printf "%s\\n" "{err}" >&2')
        body.append(f"exit {rc}")
        arms.append(f'  "{pattern}") {"; ".join(body)} ;;')
    script = "\n".join([
        "#!/bin/sh",
        f'echo "$*" >> "{log}"',
        'case "$1" in',
        *arms,
        "  *) exit 0 ;;",
        "esac",
    ]) + "\n"
    exe = stub_bin / "launchctl"
    exe.write_text(script, encoding="utf-8")
    exe.chmod(0o755)
    return log


def _calls(log: Path) -> list[list[str]]:
    """Every invocation the stand-in recorded, as its argument list."""
    if not log.exists():
        return []
    return [ln.split() for ln in log.read_text(encoding="utf-8").splitlines() if ln]


def _subcommands(log: Path) -> list[str]:
    return [c[0] for c in _calls(log)]


def _darwin(monkeypatch) -> None:
    """The one seam this file cannot inject through: ``_launchd``'s macOS gate is
    a module-level ``sys.platform`` read, and the LaunchAgent lifecycle has to be
    provable on either kind of CI host."""
    monkeypatch.setattr(_launchd.sys, "platform", "darwin")


def _written_plist(label: str) -> dict:
    return plistlib.loads(_launchd._plist_path(label).read_bytes())


# ── real stores + a real interrupt, for the watch loop ───────────────────────


def _seed_claude_code_store(home: Path, project: str, name: str) -> Path:
    """A real claude-code session under ``$HOME/.claude/projects`` — the store the
    default watcher set discovers on its own, with no watcher list injected."""
    d = home / ".claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    write_jsonl(f, [cc_user(name), cc_assistant(name)])
    return f


def _loop_is_running(archive_home) -> Callable[[], bool]:
    """True once the watch loop has completed a pass: ``watch_pass_last`` only
    reaches ``health.json`` from inside ``Watcher._run_loop``, so it is proof the
    process is in the blocking loop and an interrupt will land there."""

    def running() -> bool:
        try:
            return "watch_pass_last" in json.loads(
                (archive_home / "health.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False

    return running


def _raise_in_thread(thread_id: int, exc: type[BaseException]) -> None:
    """Asynchronously raise ``exc`` in the thread with ``thread_id`` — the
    operator's ^C landing in the blocked verb.

    Injecting the exception, rather than sending a process ``SIGINT``, is what
    makes this work under ``pytest-xdist``: an xdist worker does not deliver a
    ``SIGINT`` as a ``KeyboardInterrupt`` to the running test, so a signal-based
    interrupt hangs the watch loop until the timeout there while passing serially.
    ``PyThreadState_SetAsyncExc`` is signal-disposition-independent and delivers
    exactly the ``KeyboardInterrupt`` the loop's own bytecode sees from a real ^C.

    The exception is pending until the target next runs Python bytecode; the loop
    wakes out of its short ``time.sleep`` slice within one slice and raises there."""
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id), ctypes.py_object(exc)
    )
    if n != 1:  # 0 = the thread is already gone; >1 = the id matched too broadly
        if n > 1:  # undo the over-broad set rather than corrupt an unrelated thread
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)
        raise RuntimeError(f"could not deliver interrupt to thread {thread_id} (n={n})")


def _interrupt_once(ready: Callable[[], bool], *, timeout: float = 120.0) -> threading.Thread:
    """Raise ``KeyboardInterrupt`` in the caller's thread — the operator's ^C —
    once ``ready()`` holds, so the CLI's own ``KeyboardInterrupt`` handler runs
    the real shutdown. The exception is injected into the *calling* thread (the
    one about to block in the verb under test); see :func:`_raise_in_thread` for
    why a process ``SIGINT`` won't do under xdist.

    The interrupt is unconditional: it fires when ``ready()`` holds, when the
    deadline lapses, and — via the ``finally`` — even if ``ready()`` raises.
    The caller's thread is *blocked* in the verb under test; a poll thread
    that dies without firing leaves it blocked forever (a raising predicate,
    e.g. a probe hitting a not-yet-listening socket, once hung whole CI sweeps).
    A predicate that raises therefore counts as "not ready yet" and is retried
    until the deadline."""
    target = threading.get_ident()

    def wait_then_interrupt() -> None:
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                try:
                    if ready():
                        break
                except Exception:  # noqa: BLE001 — not-ready, retry until deadline
                    pass
                time.sleep(0.02)
        finally:
            _raise_in_thread(target, KeyboardInterrupt)

    t = threading.Thread(target=wait_then_interrupt, name="watch-interrupt", daemon=True)
    t.start()
    return t


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ── _parse_hhmm edge cases ────────────────────────────────────────────────────


def test_parse_hhmm_valid() -> None:
    assert cli._parse_hhmm("04:30") == (4, 30)
    assert cli._parse_hhmm("00:00") == (0, 0)
    assert cli._parse_hhmm("23:59") == (23, 59)


def test_parse_hhmm_non_numeric_raises() -> None:
    # int() raises ValueError inside the try → the guarded SystemExit.
    with pytest.raises(SystemExit) as exc:
        cli._parse_hhmm("aa:bb")
    assert "HH:MM" in str(exc.value)


def test_parse_hhmm_missing_colon_raises() -> None:
    # split(":") yields one element → unpack ValueError.
    with pytest.raises(SystemExit):
        cli._parse_hhmm("930")


def test_parse_hhmm_hour_out_of_range_raises() -> None:
    with pytest.raises(SystemExit) as exc:
        cli._parse_hhmm("25:00")
    assert "25:00" in str(exc.value)


def test_parse_hhmm_minute_out_of_range_raises() -> None:
    with pytest.raises(SystemExit):
        cli._parse_hhmm("04:99")


# ── _self_throttle ────────────────────────────────────────────────────────────


def test_self_throttle_really_renices_the_process() -> None:
    """``nice()`` is one-way, so this runs in a child: the real throttle (CPU
    nice + on macOS the io-policy syscall) lands on a process we can throw away,
    and the env opt-out really skips it."""
    probe = (
        "import os, sys;"
        "from thread_archive.cli import _self_throttle;"
        "_self_throttle();"
        "print(os.nice(0))"
    )

    def run(**env):
        return subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, env={**os.environ, **env},
        )

    # Relative to this process's own niceness, not a fixed number: nice is
    # inherited, so the launcher sets the floor — the CI sweeper runs its suites
    # at nice 10, and 19 is the kernel's ceiling.
    base = os.nice(0)

    throttled = run(THREAD_ARCHIVE_NO_THROTTLE="")
    assert throttled.returncode == 0, throttled.stderr
    assert int(throttled.stdout.strip()) == min(base + 10, 19)

    opted_out = run(THREAD_ARCHIVE_NO_THROTTLE="1")
    assert opted_out.returncode == 0, opted_out.stderr
    assert int(opted_out.stdout.strip()) == base


def test_self_throttle_non_darwin(monkeypatch) -> None:
    """Off macOS the CPU renice still happens and the io-policy syscall is skipped.

    The opt-out has to come off first or the body never runs (the suite sets it —
    see conftest). ``nice`` is the one thing here that cannot run for real: it is
    one-way, so a real call would deprioritize the test runner and every later
    test with it. ``sys.platform`` is the other: both sides of the gate have to be
    provable from whichever host runs the suite.
    """
    import os

    monkeypatch.delenv("THREAD_ARCHIVE_NO_THROTTLE")
    niced = []
    monkeypatch.setattr(os, "nice", niced.append)
    monkeypatch.setattr(cli.sys, "platform", "linux")

    cli._self_throttle()

    assert niced == [10]  # the renice ran; the darwin-only syscall did not


# ── _age ──────────────────────────────────────────────────────────────────────


def test_age_invalid_returns_question_mark() -> None:
    assert cli._age("not-a-date") == "?"
    assert cli._age(None) == "?"  # TypeError path


def test_age_naive_and_ranges() -> None:
    # tz-naive input is coerced to UTC; a 3-day-old stamp reads in days.
    assert cli._age("2020-01-01T00:00:00").endswith("d ago")
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert cli._age(recent).endswith("h ago")
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    assert cli._age(old).endswith("d ago")


# ── import ────────────────────────────────────────────────────────────────────


def test_import_line_stream_summary(archive_home, tmp_path, capsys) -> None:
    """A line-stream provider's summary names the thread it landed in and the
    events it created — read back off the real import, not a stub's numbers."""
    session = tmp_path / "session.jsonl"
    write_jsonl(session, [cc_user("cli"), cc_assistant("cli")])

    rc = main(["import", str(session), "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"imported {session} (claude-code): thread=" in out
    assert "new=True" in out
    events = int(out.split("events=")[1].split()[0])
    assert events == event_count() > 0

    # a second import of the same file is the same thread, no longer new
    assert main(["import", str(session), "--home", str(archive_home)]) == 0
    assert "new=False" in capsys.readouterr().out


def _cursor_store(path) -> None:
    """A minimal cursor ``state.vscdb``: one composer, one user + one agent bubble."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        ("composerData:comp1", json.dumps({
            "name": "Cursor Chat", "lastUpdatedAt": 1700000000000,
            "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1},
                                            {"bubbleId": "b2", "type": 2}]})),
        ("bubbleId:comp1:b1", json.dumps(
            {"type": 1, "text": "hello cursor", "createdAt": 1700000000000})),
        ("bubbleId:comp1:b2", json.dumps(
            {"type": 2, "text": "hi from cursor", "createdAt": 1700000001000})),
    ])
    conn.commit()
    conn.close()


def test_import_db_scanner_summary(archive_home, tmp_path, capsys) -> None:
    """The cursor/opencode DB scanners return a result whose ``vars()`` is the
    printed summary (many sessions per file)."""
    db = tmp_path / "state.vscdb"
    _cursor_store(db)

    rc = main(["import", str(db), "--provider", "cursor", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"imported {db} (cursor):" in out
    assert "processed=1 imported=1" in out
    assert f"events_created={event_count()}" in out and event_count() > 0


def test_import_unknown_provider_direct_call() -> None:
    """The registry guard in cmd_import (argparse ``choices`` normally blocks an
    unknown provider before the handler, so exercise the guard directly)."""
    ns = argparse.Namespace(provider="bogus-provider", path="/x", home=None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_import(ns)
    assert "unknown provider 'bogus-provider'" in str(exc.value)


def test_setup_verb_dispatches_to_wizard(tmp_path, capsys) -> None:
    """`thread-archive setup` routes cli.cmd_setup → wizard.run_setup. Under
    capsys stdout is not a TTY, so the no-``--yes`` run takes the guidance-only
    branch: no host scan, no home scaffolded, exit 0 — enough to cover the
    dispatch seam the packaged front door depends on."""
    rc = cli.main(["setup", "--home", str(tmp_path / "arc")])
    assert rc == 0
    assert "thread-archive setup" in capsys.readouterr().out


# ── providers ─────────────────────────────────────────────────────────────────
# Driven against the real registry: the verb's whole job is reporting what the
# registry holds, so stubbing it would assert only the format string.


def _providers_rows(out: str) -> dict[str, str]:
    """The printed table as ``{provider name: rest of its line}``."""
    return {
        line.split()[0]: line.split(maxsplit=1)[1]
        for line in out.splitlines()
        if line and not line.startswith("(")
    }


def test_providers_lists_sources_not_mechanisms(archive_home, capsys) -> None:
    rc = main(["providers", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    rows = _providers_rows(out)
    # Archive's own machinery is held back unless asked for.
    assert "export-drop" not in rows and "cc-exthost" not in rows
    assert "on" in rows["claude-code"] and "Claude Code" in rows["claude-code"]
    assert "line-stream" in rows["claude-code"]
    assert "db-scan" in rows["cursor"]
    # An export-only provider has no live store to poll; both traits are named.
    assert "export:" in rows["chatgpt"] and "no live store" in rows["chatgpt"]
    # A watched provider with no importer kind and no export carries no traits,
    # so its line ends at the label — no empty parenthetical.
    assert rows["cowork"].rstrip().endswith("Cowork")
    assert "(--all also lists archive's own machinery)" in out


def test_providers_all_includes_mechanisms(archive_home, capsys) -> None:
    rc = main(["providers", "--all", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    rows = _providers_rows(out)
    assert "mechanism" in rows["export-drop"]
    assert "follows claude-code" in rows["cc-exthost"]
    # The footer only advertises --all when it wasn't given.
    assert "--all also lists" not in out


def test_providers_marks_disabled_and_followers_off(archive_home, capsys) -> None:
    """A disabled source reads ``off``, and so does the recovery pass that follows
    it — the follower has no independent meaning once its primary is off."""
    (archive_home / "config.json").write_text(
        '{"sources": {"claude-code": {"enabled": false}}}', encoding="utf-8"
    )
    rc = main(["providers", "--all", "--home", str(archive_home)])
    assert rc == 0
    rows = _providers_rows(capsys.readouterr().out)
    assert rows["claude-code"].startswith("off")
    assert rows["cc-exthost"].startswith("off")
    assert rows["codex"].startswith("on")


# ── import-export ─────────────────────────────────────────────────────────────


def test_import_export_imports_a_real_export(archive_home, tmp_path, capsys) -> None:
    """`thread-archive source import-account` unpacks a real claude.ai export ZIP into the
    archive, and --force reaches the importer: a second pass skips what is
    already there unless it is told to reimport."""
    conv = {
        "uuid": "conv-1", "name": "Exported", "created_at": "2026-01-01T10:00:00Z",
        "updated_at": "2026-01-01T10:00:10Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "hello export",
             "created_at": "2026-01-01T10:00:00Z"},
            {"uuid": "m2", "sender": "assistant", "text": "hi from the export",
             "created_at": "2026-01-01T10:00:05Z"},
        ],
    }
    def write_export(path, conversation) -> None:
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("conversations.json", json.dumps([conversation]))
            zf.writestr("users.json", json.dumps([{"uuid": "user-1"}]))

    export = tmp_path / "claude-export.zip"
    write_export(export, conv)

    rc = main(["import-export", str(export), "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"imported export {export}: processed=1 imported=1 skipped=0 events=" in out
    assert event_count() > 0
    # checkpointed: the imported thread's metadata really reached the truth log
    assert any("Exported" in f.read_text(encoding="utf-8")
               for f in (archive_home / "truth" / "threads").rglob("*.jsonl"))

    # a later export of the same conversation, one turn longer
    grown = {**conv, "chat_messages": [*conv["chat_messages"],
             {"uuid": "m3", "sender": "human", "text": "one more turn",
              "created_at": "2026-01-01T10:01:00Z"}]}
    write_export(export, grown)

    assert main(["import-export", str(export), "--home", str(archive_home)]) == 0
    assert "processed=1 imported=0 skipped=1 events=0" in capsys.readouterr().out

    before = event_count()
    assert main(["import-export", str(export), "--force",
                 "--home", str(archive_home)]) == 0
    assert "processed=1 imported=1 skipped=0" in capsys.readouterr().out
    assert event_count() > before  # --force really re-read the export


# ── watch ─────────────────────────────────────────────────────────────────────


def test_watch_once_imports_and_maintains(archive_home, tmp_path, monkeypatch, capsys) -> None:
    """``watch --once`` over a real machine: the default source set finds the
    seeded claude-code store on its own, imports it, reports the pass, and — because
    events landed — runs the one-shot upkeep. A second project holding an
    unreadable "session" rides out as a poll error."""
    machine = tmp_path / "machine"
    _seed_claude_code_store(machine, "myproj", "sess")
    # A directory where a transcript should be: the importer really raises OSError
    # on it, so the source's per-item error path runs for real.
    (machine / ".claude" / "projects" / "broken" / "notes.jsonl").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(machine))

    rc = main(["watch", "--once", "--home", str(archive_home)])
    assert rc == 0

    events = event_count()
    assert events > 0  # the session really imported
    out = capsys.readouterr().out
    assert f"watch: checked 2 sources, imported 1 items ({events} events)" in out
    assert "  ! claude-code import error for broken:notes:" in out
    # events_created > 0 → the one-shot ran its upkeep pass, which stamps the
    # truth manifest watermark.
    assert (archive_home / "truth" / "manifest.json").exists()


def test_watch_once_no_events_skips_maintain(archive_home, tmp_path, monkeypatch, capsys) -> None:
    """A machine with no AI-tool stores: the pass is a clean no-op and the upkeep
    pass is skipped, so no manifest watermark is written."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-machine"))

    rc = main(["watch", "--once", "--no-embed", "--home", str(archive_home)])
    assert rc == 0
    assert "watch: checked 0 sources, imported 0 items (0 events)" in capsys.readouterr().out
    assert not (archive_home / "truth" / "manifest.json").exists()


def test_watch_loop_runs_until_interrupted(archive_home, tmp_path, monkeypatch, capsys) -> None:
    """The bare loop: it blocks in ``Watcher.run`` until a real ^C, then reports the
    stop. Nothing was cohosted, so the shutdown has no server to close."""
    monkeypatch.setenv("HOME", str(tmp_path / "machine"))
    _seed_claude_code_store(tmp_path / "machine", "myproj", "sess")

    interrupt = _interrupt_once(_loop_is_running(archive_home))
    rc = main(["watch", "--interval", "0.05", "--home", str(archive_home)])
    interrupt.join(5)

    assert rc == 0
    assert "stopped." in capsys.readouterr().out
    assert event_count() > 0  # the loop really polled and imported


@pytest.mark.integration
@pytest.mark.viewer
def test_watch_web_cohosts_the_viewer_and_closes_it_on_interrupt(
    archive_home, tmp_path, monkeypatch, capsys
) -> None:
    """``--web`` cohosts the read viewer in the watcher's own process: it answers on
    the requested bind while the loop runs, and the shutdown's finally-clause closes
    it — the port is free again once the verb returns."""
    monkeypatch.setenv("HOME", str(tmp_path / "machine"))
    port = _free_port()
    served: dict[str, object] = {}
    running = _loop_is_running(archive_home)

    def viewer_answered() -> bool:
        if not running():
            return False
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=10
        ) as resp:
            served["status"] = resp.status
            served["home"] = json.loads(resp.read())["home"]
        return True

    interrupt = _interrupt_once(viewer_answered)
    rc = main(["watch", "--web", "--web-host", "127.0.0.1", "--web-port", str(port),
               "--interval", "0.05", "--home", str(archive_home)])
    interrupt.join(5)

    assert rc == 0
    assert "stopped." in capsys.readouterr().out
    # The cohosted viewer served this archive on the requested bind…
    assert served == {"status": 200, "home": str(archive_home)}
    # …and the finally-clause really closed it: the bind refuses connections now.
    with socket.socket() as s:
        s.settimeout(5)
        assert s.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.integration
@pytest.mark.viewer
def test_watch_web_warms_the_retrieval_models_on_start(archive_home, tmp_path, monkeypatch) -> None:
    """The cohosting daemon warms retrieval before anyone can search it. The cold load
    is tens of seconds while the viewer's search box says only "searching…", so a first
    query that pays it reads as a broken product — and every query after is sub-second,
    which puts the whole cost on the one search that forms someone's impression.

    Read off the ledger row the warm pass writes as its last act, and off the load
    policy the daemon switches on for the window before it lands. This suite is
    model-free, so the pass here is arms that stand down and a lexical priming search:
    what is under test is that the daemon runs it at all, on its own thread, without
    holding up the loop it is about to enter."""
    monkeypatch.setenv("HOME", str(tmp_path / "machine"))
    from thread_archive._retrieval.model_slot import defer_construction

    ledger = archive_home / "retrieval-usage.jsonl"

    def warmed() -> bool:
        return any(
            json.loads(ln).get("kind") == "warm"
            for ln in ledger.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        )

    interrupt = _interrupt_once(warmed)
    rc = main(["watch", "--web", "--web-host", "127.0.0.1", "--web-port", str(_free_port()),
               "--interval", "0.05", "--home", str(archive_home)])
    interrupt.join(5)

    assert rc == 0
    assert warmed()  # the pass ran to completion while the loop was serving
    assert defer_construction() is True


@pytest.mark.viewer
def test_watch_web_refuses_a_non_loopback_bind(archive_home, tmp_path, monkeypatch) -> None:
    """The cohosted viewer is unauthenticated full read, so a typo'd ``--web-host``
    must fail loudly before the loop starts rather than exposing the archive."""
    monkeypatch.setenv("HOME", str(tmp_path / "machine"))
    monkeypatch.delenv("THREAD_ARCHIVE_WEB_NONLOCAL", raising=False)

    # The refusal lands before any bind, so the port never has to be free.
    with pytest.raises(ValueError, match="refusing non-loopback bind"):
        main(["watch", "--web", "--web-host", "0.0.0.0", "--web-port", "8787",
              "--home", str(archive_home)])


# ── daemon: mcp / backup / watcher lifecycle ─────────────────────────────────
# The verbs run for real against a redirected $HOME (plists land in tmp_path) and
# a `launchctl` stand-in that is the only executable on $PATH, so the assertions
# are the agent that would actually be installed and the argv launchctl actually
# received.


def test_daemon_mcp_lifecycle(tmp_path, monkeypatch, stub_bin, capsys) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    status_out = f"{MCP_LABEL} = {{\\nstate = running\\npid = 4242\\n}}"
    log = _launchctl_stub(stub_bin, {
        # bootout nonzero = nothing was loaded → install skips the settle sleep.
        "bootout": (1, "", ""), "bootstrap": (0, "", ""),
        "kickstart": (0, "", ""), "print": (0, status_out, ""),
    })
    arc = str(tmp_path / "arc")

    assert main(["daemon", "install", "--mcp", "--mcp-ingest", "--http-host", "1.2.3.4",
                 "--http-port", "9", "--home", arc]) == 0
    out = capsys.readouterr().out
    assert "shared MCP server: http://1.2.3.4:9/mcp" in out
    assert "catch-up ingest: enabled" in out
    plist = _written_plist(MCP_LABEL)
    assert plist["ProgramArguments"][1:] == ["--http", "--host", "1.2.3.4", "--port", "9"]
    assert plist["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == arc
    assert plist["EnvironmentVariables"]["THREAD_ARCHIVE_MCP_INGEST"] == "1"
    assert _calls(log) == [
        ["bootout", f"gui/{_launchd._uid()}/{MCP_LABEL}"],
        ["bootstrap", f"gui/{_launchd._uid()}", str(_launchd._plist_path(MCP_LABEL))],
    ]

    assert main(["daemon", "uninstall", "--mcp"]) == 0
    assert "uninstalled" in capsys.readouterr().out
    assert not _launchd._plist_path(MCP_LABEL).exists()

    assert main(["daemon", "restart", "--mcp"]) == 0
    assert "restarted" in capsys.readouterr().out

    assert main(["daemon", "status", "--mcp"]) == 0
    status = capsys.readouterr().out
    assert MCP_LABEL in status and "state = running" in status and "pid = 4242" in status

    assert _subcommands(log) == ["bootout", "bootstrap", "bootout", "kickstart", "print"]


def test_daemon_backup_lifecycle(tmp_path, monkeypatch, stub_bin, capsys) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    log = _launchctl_stub(stub_bin, {"kickstart": (0, "", ""), "print": (1, "", "")})

    assert main(["daemon", "uninstall", "--backup"]) == 0
    assert "uninstalled" in capsys.readouterr().out

    assert main(["daemon", "restart", "--backup"]) == 0
    assert "restarted" in capsys.readouterr().out

    # launchctl print exits nonzero for an agent that isn't loaded.
    assert main(["daemon", "status", "--backup"]) == 0
    assert f"{BACKUP_LABEL}: not loaded" in capsys.readouterr().out
    assert _subcommands(log) == ["bootout", "kickstart", "print"]


def test_daemon_backup_install_passes_notify_url(tmp_path, monkeypatch, stub_bin, capsys) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _launchctl_stub(stub_bin, {"bootout": (1, "", ""), "bootstrap": (0, "", "")})

    assert main(["daemon", "install", "--backup", "--dest", "/vol/bak",
                 "--at", "04:00", "--notify-url", "http://n"]) == 0
    assert "nightly at 04:00" in capsys.readouterr().out
    plist = _written_plist(BACKUP_LABEL)
    assert plist["ProgramArguments"][1:] == [
        "backup", "nightly", "/vol/bak", "--notify-url", "http://n"
    ]
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}


@pytest.mark.viewer
def test_daemon_watcher_install_with_web(tmp_path, monkeypatch, stub_bin, capsys) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _launchctl_stub(stub_bin, {"bootout": (1, "", ""), "bootstrap": (0, "", "")})
    arc = str(tmp_path / "arc")

    assert main(["daemon", "install", "--home", arc]) == 0  # web defaults on
    assert "web viewer: http://127.0.0.1:8787" in capsys.readouterr().out
    plist = _written_plist(WATCHER_LABEL)
    assert plist["ProgramArguments"][1:] == ["watch", "--web", "--web-port", "8787"]
    assert plist["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == arc


@pytest.mark.viewer
def test_daemon_watcher_install_no_web(tmp_path, monkeypatch, stub_bin, capsys) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _launchctl_stub(stub_bin, {"bootout": (1, "", ""), "bootstrap": (0, "", "")})

    assert main(["daemon", "install", "--no-web"]) == 0
    assert "web viewer" not in capsys.readouterr().out
    assert _written_plist(WATCHER_LABEL)["ProgramArguments"][1:] == ["watch"]


def test_daemon_install_reports_a_launchctl_failure(tmp_path, monkeypatch, stub_bin) -> None:
    """A bootstrap launchd refuses is operator guidance carrying launchctl's own
    stderr, not a traceback."""
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _launchctl_stub(stub_bin, {
        "bootout": (1, "", ""), "bootstrap": (5, "", "Bootstrap failed: 5"),
    })

    with pytest.raises(SystemExit, match="bootstrap failed: Bootstrap failed: 5"):
        main(["daemon", "install", "--home", str(tmp_path / "arc")])


def test_daemon_watcher_uninstall_restart_status(tmp_path, monkeypatch, stub_bin, capsys) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    status_out = f"{WATCHER_LABEL} = {{\\nstate = running\\n}}"
    log = _launchctl_stub(stub_bin, {"kickstart": (0, "", ""), "print": (0, status_out, "")})
    plist = _launchd._plist_path(WATCHER_LABEL)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"stale")

    assert main(["daemon", "uninstall"]) == 0
    assert "uninstalled" in capsys.readouterr().out
    assert not plist.exists()  # the plist is really gone, not just booted out

    assert main(["daemon", "restart"]) == 0
    assert "restarted" in capsys.readouterr().out

    assert main(["daemon", "status"]) == 0
    assert "state = running" in capsys.readouterr().out
    assert _subcommands(log) == ["bootout", "kickstart", "print"]


# ── reindex error branch ──────────────────────────────────────────────────────


def _gut_the_truth(archive_home) -> None:
    """Empty the archive's one thread file — a truth that lost committed records
    the index still holds, the state the publication guard exists for."""
    from thread_archive._truth import jsonl_log

    one_thread_file(archive_home).write_text("", encoding="utf-8")
    jsonl_log.reset_handles()


def test_reindex_refuses_a_lossy_rebuild(archive_home, tmp_path, monkeypatch, capsys) -> None:
    """A rebuild that would lose committed records is refused and the old index
    kept — the operator gets guidance on stderr, not a stack trace."""
    monkeypatch.setenv("THREAD_ARCHIVE_NO_THROTTLE", "1")
    import_cc_session(tmp_path)
    before = event_count()
    _gut_the_truth(archive_home)

    rc = main(["reindex", "--home", str(archive_home)])
    assert rc == 1
    assert "reindex refused:" in capsys.readouterr().err
    assert event_count() == before  # the old index is intact


def test_reindex_salvage_publishes_the_lossy_rebuild(
    archive_home, tmp_path, monkeypatch, capsys
) -> None:
    """--salvage reaches api.reindex: the same rebuild the default refuses is
    published, and the counts it printed are the ones it wrote."""
    monkeypatch.setenv("THREAD_ARCHIVE_NO_THROTTLE", "1")
    import_cc_session(tmp_path)
    _gut_the_truth(archive_home)

    rc = main(["reindex", "--vectors", "--salvage", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "vectors=True" in out and "done" in out
    assert "events" in out and "threads" in out
    assert event_count() == 0  # the lossy rebuild really replaced the index


# ── backup: full warning surface ──────────────────────────────────────────────


def test_backup_all_warnings_returns_1(capsys) -> None:
    res = {
        "truth_dir": "t", "dest": "d", "files_copied": 3, "bytes_copied": 5 * 1024 * 1024,
        "verify_ok": False, "deletions_skipped": 2, "shrinks_skipped": 1,
        "shrink_sample": ["a.jsonl"], "mirror_complete": True,
        "generation_created": "2026-07-15T00-00-00", "generations_kept": 7,
        "generations_pruned": 1, "generation_error": "snap failed",
        "rehomed_twins_deleted": 4, "renamed_twins_deleted": 2,
    }
    rc = cli.report_backup(res)
    assert rc == 1
    out = capsys.readouterr().out
    assert "generation:" in out
    assert "WARNING: generation snapshot failed" in out
    assert "pre-backup verify FAILED" in out
    assert "rebalance twins: 4" in out
    assert "migration twins: 2" in out
    assert "stale destination files kept" in out
    assert "SHRINK GUARD: 1" in out


# ── verify: parse errors, fts, deep samples, hashes, backup error ─────────────


def _verify_rich_failure() -> dict:
    return {
        "ok": False,
        "failed_components": ["drift_events", "fts_orphans"],
        "failure_log": "/home/verify-failures.jsonl",
        "truth": {
            "threads": 3, "events": 10, "events_effective": 8,
            "duplicate_id_lines": 1, "duplicate_content_lines": 1, "parse_errors": 2,
            "parse_errors_torn_tail": 1, "parse_errors_interior": 1,
            "parse_error_sample": ["truth/threads/x.jsonl:5"],
        },
        "index": {"threads": 3, "events": 9, "kg_events": 4,
                  "quick_check": "ok", "check": "quick_check"},
        "drift": {"threads": 0, "events": -1, "kg_events": 0},
        "fts": {"shadow_rows": 9, "fts5_rows": 8, "orphan_rows": 2},
        "deep": {
            "watermark": 9, "events_index_only": 1, "index_only_sample": [7],
            "events_missing_from_index": 1, "missing_sample": [3],
            "events_key_mismatch": 1, "key_mismatch_sample": [4],
            "events_superseded_twins": 1,
            "thread_meta_mismatch": 2, "thread_meta_sample": [11, 12],
            "kg": {"index_only": 1, "truth_only": 0, "content_mismatch": 1},
            "dangling": {"link_endpoints": 1, "citation_events": 0,
                         "citation_thread_mismatch": 0, "event_threads": 1},
            "duplicate_content_pairs_index": 1,
            "fts": {"orphan_rows": 2, "shadow_rows": 9, "fts5_rows": 8,
                    "unindexed_events": 1, "unindexed_sample": [6],
                    "empty_extract_events": 1},
        },
        "hashes": {
            "truth": {"checked": 8, "mismatched": 1, "unhashed_keys": 0, "no_key": 0,
                      "mismatch_sample": [1]},
            "index": {"checked": 8, "mismatched": 1, "unhashed_keys": 0, "no_key": 0,
                      "mismatch_sample": [2]},
            "cross": {"compared": 8, "mismatched": 1, "mismatch_sample": [3]},
            "previous": {"at": "2026-07-14T00:00:00Z"},
            "delta": {"truth_mismatched": 1, "index_mismatched": 1, "cross_mismatched": 1},
        },
        "backup": {"dest": "/mirror", "error": "mirror unreadable"},
    }


def test_verify_rich_failure_all_branches(capsys) -> None:
    rc = cli.report_verify(
        _verify_rich_failure(), deep=True, hashes=True, backup="/mirror"
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "torn tails=1 interior=1" in out
    assert "parse error sample" in out
    assert "fts:   shadow=9 fts5=8 orphans=2" in out
    assert "thread metadata drift" in out
    assert "unindexed sample" in out
    assert "missing sample" in out
    assert "index-only sample" in out
    assert "key-mismatch sample" in out
    assert "hashes[truth]" in out and "mismatch sample" in out
    assert "hashes[cross]: compared=8 mismatched=1" in out
    assert "hashes delta vs 2026-07-14T00:00:00Z" in out
    assert "backup[/mirror]: mirror unreadable" in out
    assert "FAILED: drift_events, fts_orphans" in out
    assert "full result appended to /home/verify-failures.jsonl" in out


# ── restore-drill: report + failure branches ─────────────────────────────────


def test_restore_drill_failure_with_smoke(capsys) -> None:
    res = {
        "ok": False, "seconds": 1.0, "error": "rebuild aborted",
        "mirror": {"threads": 1, "events_effective": 1, "parse_errors": 3},
        "rebuilt": {"threads": 1, "events": 1},
        "smoke": {"read_ok": True, "search_ok": False, "token": "hello", "error": "no hit"},
        "drill_home": "/tmp/drill-abc",
    }
    rc = cli.report_restore_drill(res)
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED: rebuild aborted" in out
    assert "smoke:  read=ok search=FAILED" in out
    assert "token 'hello'" in out and "error: no hit" in out
    assert "drill home kept: /tmp/drill-abc" in out
    assert "RESTORE DRILL FAILED (1.0s)" in out


# ── restore: the failure branch a happy restore can't produce ─────────────────
# The real restore path (mirror + rebuilt + working smoke + OK line) is driven
# end-to-end in test_restore.py.


def test_restore_failed_no_mirror_no_smoke_error(capsys) -> None:
    """A failed restore with neither mirror nor rebuilt scanned, no smoke, and an
    error — the pure failure surface, from a named generation."""
    res = {"ok": False, "seconds": 1.0, "error": "rebuild aborted"}
    rc = cli.report_restore(res, to="/h/x")
    assert rc == 1
    out = capsys.readouterr().out
    assert "mirror:" not in out and "rebuilt:" not in out
    assert "FAILED: rebuild aborted" in out
    assert "RESTORE FAILED (1.0s)" in out


# ── nightly: stage errors + the failed-stages verdict ─────────────────────────


def test_nightly_backup_error_and_drill_error(capsys) -> None:
    res = {
        "backup": {"error": "dest not mounted"},
        "escalations": {"deep": False, "hashes": False},
        "verify": {"ok": True, "drift": {"events": 0}, "truth": {"parse_errors": 0}},
        "drill": {"error": "throwaway home failed"},
        "ok": False, "failed_stages": ["backup", "drill"],
    }
    rc = cli.report_nightly(res)
    assert rc == 1
    out = capsys.readouterr().out
    assert "backup: ERROR dest not mounted" in out
    assert "verify [shallow]: ok" in out
    assert "restore drill: ERROR throwaway home failed" in out
    assert "NIGHTLY FAILED: backup, drill" in out


# ── repair: applied (non-dry-run) branches ───────────────────────────────────


def test_repair_applied_with_samples(capsys) -> None:
    res = {
        "dry_run": False, "fragments_quarantined": 3, "files_damaged": 2,
        "damaged_sample": ["truth/threads/x.jsonl:9"], "quarantine_file": "/home/quarantine.jsonl",
        "events_restored_from_index": 4, "kg_events_restored": 1, "thread_records_restored": 2,
    }
    rc = cli.report_repair(res)
    assert rc == 0
    out = capsys.readouterr().out
    assert "quarantined 3 unparseable line(s) across 2 file(s)" in out
    assert "sample: ['truth/threads/x.jsonl:9']" in out
    assert "ledger: /home/quarantine.jsonl" in out
    assert "restored from index: 4 event(s)" in out
    assert "the repaired files shrank" in out
    assert "run `thread-archive index verify`" in out


# ── status: fully-populated ok + failed variants ─────────────────────────────


def _status_base(**over) -> dict:
    base = {
        "home": "/h", "truth_dir": "/h/truth", "index_path": "/h/index.db",
        "threads": 5, "events": 50, "fts_indexed": 50,
    }
    base.update(over)
    return base


def test_status_all_ok(capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_verify={"ok": True, "at": old},
        last_backup={"ok": True, "dest": "/vol/bak", "at": old},
        last_restore_drill={"ok": True, "coverage": 0.99, "at": old},
        last_nightly={"ok": True, "dest": "/vol/bak", "at": old},
        last_coverage={"ok": True, "sources_checked": 6, "at": old},
        last_watch_pass={"at": old, "sources": {"cc": {"events": 3, "parse_errors": 2}}},
        last_watch_errors={"at": old, "count_since_start": 4,
                           "errors": ["e1", "e2", "e3", "e4"]},
    )
    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "verify:  ok" in out
    assert "backup:  ok → /vol/bak" in out
    assert "nightly: ok → /vol/bak" in out
    assert "drill:   ok coverage=0.99" in out
    assert "coverage: ok (6 sources)" in out
    assert "3 events since pass-owner start" in out
    assert "2 PARSE ERRORS" in out
    assert "poll errors seen" in out
    # only the first 3 watch errors are echoed
    assert "e3" in out and "e4" not in out


def test_status_all_failed(capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_verify={"ok": False, "failed": ["drift"], "at": old},
        last_backup={"ok": False, "dest": "/vol/bak", "at": old},
        last_restore_drill={"ok": False, "coverage": 0.1, "at": old},
        last_nightly={"ok": False, "dest": "/vol/off",
                      "failed_stages": ["backup", "restore-drill"], "at": old},
        last_coverage={"ok": False, "at": old, "failed": ["m1", "m2", "m3", "m4"]},
        last_watch_pass={"at": old, "sources": {}},
    )
    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "verify:  FAILED (drift)" in out
    assert "backup:  FAILED → /vol/bak" in out
    assert "nightly: FAILED (backup, restore-drill) → /vol/off" in out
    assert "drill:   FAILED" in out
    assert "coverage: FAILED" in out
    assert "m1" in out and "m3" in out and "m4" not in out  # capped at 3
    assert "last pass" in out  # watch pass with no parse errors
    assert "no pass recorded" not in out


# ── coverage: source states, disabled/unwatched, skips, failure ──────────────


def test_coverage_full_surface_failed(capsys) -> None:
    result = {
        "ok": False,
        "failed": ["cursor stale > 48h"],
        "warnings": ["opencode newest event is 30h old"],
        "sources": {
            "claude-code": {"failed": None, "warning": None,
                            "store_latest": "2026-07-15", "newest_event_at": "2026-07-15",
                            "history": 100},
            "codex": {"failed": "stale", "warning": None,
                      "store_latest": None, "newest_event_at": None, "history": 5},
            "grok": {"failed": None, "warning": "lagging",
                     "store_latest": "2026-07-14", "newest_event_at": "2026-07-14",
                     "history": 20},
        },
        "disabled": {"antigravity": {"history": 0}},
        "unwatched": {"demo-harness": {"newest_event_at": None}},
        "skips": {"total": 7, "recent": 2, "recent_lines": 3, "days": 7.0},
        "drift": {"total": 0, "recent": 0, "recent_findings": 0, "days": 7.0},
    }
    rc = cli.report_coverage(result)
    assert rc == 1
    out = capsys.readouterr().out
    assert "claude-code" in out and "ok" in out
    assert "codex" in out and "stale" in out
    assert "grok" in out and "lagging" in out
    assert "antigravity" in out and "disabled" in out
    assert "demo-harness" in out and "unwatched" in out
    assert "skips: 7 ledger records" in out
    assert "warning: opencode newest event is 30h old" in out
    assert "FAILED:" in out
    assert "cursor stale > 48h" in out


# ── self-update: flag mapping + the blocked exit code ────────────────────────


def test_self_update_applied(monkeypatch, capsys) -> None:
    seen = {}

    def fake_update(*, home=None, check_only=False, allow_format_bump=False):
        seen.update(home=home, check_only=check_only, allow_format_bump=allow_format_bump)
        return {"ok": True, "action": "updated", "current": "0.9.0", "target": "0.9.1",
                "reason": "updated 0.9.0 → 0.9.1"}

    monkeypatch.setattr(_update, "self_update", fake_update)
    rc = main(["self-update", "--home", "/h", "--allow-format-bump"])
    assert rc == 0
    assert seen == {"home": "/h", "check_only": False, "allow_format_bump": True}
    assert "self-update: updated 0.9.0 → 0.9.1" in capsys.readouterr().out


def test_self_update_blocked_returns_1(capsys) -> None:
    rc = cli.report_self_update(
        {"ok": False, "action": "blocked", "current": "0.9.0",
         "reason": "truth format 4 > this install reads 3"},
    )
    assert rc == 1
    assert "self-update: BLOCKED: truth format 4 > this install reads 3" in capsys.readouterr().out


# ── mirror: per-provider rows, extras, unsupported, exit code ────────────────


def _mirror_provider(**over) -> dict:
    p = {"ok": True, "files": 10, "copied": 2, "unchanged": 8,
         "bytes_in": 1000, "bytes_out": 400}
    p.update(over)
    return p


def test_mirror_cli_sweeps_the_real_sources(archive_home, capsys) -> None:
    """`thread-archive source mirror` runs the real raw-store sweep into <home>/source-mirror.
    Nothing is on disk to mirror in a throwaway home, so the run is green and
    the root it names is the one it created."""
    rc = main(["mirror", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"OK → {archive_home / 'source-mirror'}" in out


def test_mirror_failed_provider_reports_extras_and_errors(capsys) -> None:
    result = {
        "ok": False, "root": "/h/source-mirror", "duration_s": 4.0,
        "providers": {
            "codex": _mirror_provider(generations=3, sidecars_capped=7),
            "cursor": _mirror_provider(ok=False, error_count=2,
                                       errors=["sweep: OSError: disk full",
                                               "db: locked"]),
        },
        "unsupported": ["demo-harness"],
    }
    rc = cli.report_mirror(result)
    assert rc == 1
    out = capsys.readouterr().out
    assert "generations=3" in out and "capped=7" in out
    assert "cursor" in out and "FAILED" in out and "errors=2" in out
    assert "    sweep: OSError: disk full" in out and "    db: locked" in out
    assert "demo-harness     unsupported (watcher shape has no mirror path)" in out
    assert "FAILED → /h/source-mirror (4.0s)" in out


# ── loads: the wait, stated in the units a person waits in ───────────────────


def test_progress_line_reports_phase_percent_rate_and_eta(capsys) -> None:
    cli._progress_line({"phases": [
        {"name": "embed", "done": 2500, "total": 10000,
         "rate_per_s": 27.7, "eta_s": 271},
    ]})
    out = capsys.readouterr().out
    assert "embed: 2,500/10,000" in out
    assert "25.0%" in out and "27.7/s" in out and "ETA 4m31s" in out
    assert out.startswith("\r")  # rewrites its own line, never scrolls


def test_loads_reports_no_load_and_no_history(archive_home, capsys) -> None:
    rc = main(["loads", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "current: no load in flight" in out
    assert "no recorded runs" in out


def test_loads_reports_a_stalled_load_and_the_run_history(archive_home, capsys) -> None:
    """A load whose process is gone reads as stalled — the case the record exists
    for — and each finished run shows its phases with their internal split."""
    from thread_archive._ops import load_runs

    (archive_home / load_runs.STATE_FILE).write_text(json.dumps({
        "kind": "embed", "status": "running", "pid": 0, "elapsed_s": 3700,
        "phases": [{"name": "embed", "elapsed_s": 3700, "done": 900, "total": 5000,
                    "rate_per_s": 14.2}],
    }), encoding="utf-8")
    load_runs.ledger_path(archive_home).write_text("\n".join([
        json.dumps({"at": "2026-07-20T04:00:00+00:00", "kind": "reindex",
                    "status": "ok", "duration_s": 92,
                    "phases": [{"name": "reindex", "elapsed_s": 92, "done": 40,
                                "counts": {"threads": 12}}]}),
        json.dumps({"at": "2026-07-21T04:00:00+00:00", "kind": "embed",
                    "status": "failed", "duration_s": 5400,
                    "phases": [{"name": "embed", "elapsed_s": 5400, "done": 300,
                                "total": 900, "rate_per_s": 3.1,
                                "detail_s": {"encode": 5100, "write": 120}}]}),
    ]) + "\n", encoding="utf-8")

    assert main(["loads", "--home", str(archive_home)]) == 0
    out = capsys.readouterr().out
    assert "current: embed — stalled" in out
    assert "the load died mid-phase" in out
    assert "embed" in out and "900/5,000 done" in out and "14.2/s" in out
    assert "recent runs (2):" in out
    assert "2026-07-21T04:00:00  embed    failed" in out
    assert "[encode 1h25m write 2m00s]" in out   # where the time actually went
    assert "(threads=12)" in out


# ── progress on a real terminal: the tty-only arm of the long verbs ──────────


def _run_on_a_terminal(argv: list[str], archive_home: Path, home: Path) -> str:
    """Run the CLI with its stdout attached to a real pty, and return what a
    terminal would have shown.

    The live progress display is gated on ``sys.stdout.isatty()`` — the arm a
    captured or redirected stream never takes — so the only honest way to see it
    is to give the process an actual terminal."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "thread_archive", *argv],
        stdout=slave, stderr=subprocess.DEVNULL,
        env={**os.environ, "HOME": str(home), "THREAD_ARCHIVE_HOME": str(archive_home),
             "THREAD_ARCHIVE_EMBED": "off", "THREAD_ARCHIVE_RERANK": "off"},
    )
    os.close(slave)  # the child now holds the only writer; read drains to EOF
    chunks: list[bytes] = []
    try:
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:  # the pty raises rather than EOFs when the child goes
                break
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(master)
    assert proc.wait(timeout=180) == 0
    return b"".join(chunks).decode()


@pytest.mark.integration
def test_the_long_verbs_draw_progress_on_a_terminal_and_wipe_it(
    archive_home, tmp_path
) -> None:
    """``watch --once`` and ``embed`` rewrite one line while they work, then clear
    it — so the summary lands on a clean line instead of a half-drawn bar."""
    machine = tmp_path / "machine"
    machine.mkdir()

    watched = _run_on_a_terminal(["watch", "--once", "--no-embed"], archive_home, machine)
    assert "\r  import: 0" in watched                    # the live phase line…
    assert "\r" + " " * 80 + "\r" in watched              # …wiped before the summary
    assert watched.rstrip().endswith("watch: checked 0 sources, imported 0 items (0 events)")

    embedded = _run_on_a_terminal(["embed"], archive_home, machine)
    assert "\r  embed: 0" in embedded
    assert "\r" + " " * 80 + "\r" in embedded
    assert embedded.rstrip().endswith("embedded 0")


# ── migrate: the failure arms ────────────────────────────────────────────────


def test_migrate_reports_what_it_moved(tmp_path, capsys) -> None:
    """The v1→v2 truth migration over a real legacy home: it swaps truth, rebuilds
    the index, verifies it, and reports what moved."""
    home = tmp_path / "legacy"
    home.mkdir()
    make_legacy_home(home)

    assert main(["migrate", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "migration complete: truth format v2, threads=3 events=4" in out


def test_migrate_dry_run_says_truth_was_untouched(tmp_path, capsys) -> None:
    home = tmp_path / "legacy-dry"
    home.mkdir()
    make_legacy_home(home)

    assert main(["migrate", "--dry-run", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "migration dry run complete; truth was not changed" in out
    # The proof it was a dry run: truth is still v1 on disk.
    assert '"version": 1' in (home / "truth" / "manifest.json").read_text(encoding="utf-8")


def test_migrate_reports_a_failure(tmp_path, capsys) -> None:
    # Unreadable truth metadata: the migration cannot start, and says why.
    home = tmp_path / "legacy-broken"
    home.mkdir()
    make_legacy_home(home)
    (home / "truth" / "manifest.json").write_text("{not json", encoding="utf-8")

    assert main(["migrate", "--home", str(home)]) == 1
    assert "migration failed: Expecting property name" in capsys.readouterr().err


# ── main() with no subcommand prints help ────────────────────────────────────


def test_main_no_command_prints_help(capsys) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower() or "archive" in out


def test_status_names_what_the_health_page_is_holding_back(archive_home, capsys) -> None:
    """A silence made in the viewer is a UI choice, and the terminal must not
    inherit it silently — a report that omits a warning someone put aside is the
    half-truth this whole surface exists to avoid."""
    from thread_archive._ops.notices import silence

    st = _status_base(backup_same_device=True)
    silence("same-disk", st)

    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "silenced: 1 notice(s) held aside on the health page" in out
    assert "Backup is on the same filesystem as the archive" in out


def test_status_names_the_ingest_faults_on_record(archive_home, capsys) -> None:
    """The question a green daemon cannot answer about itself: was ingest ever
    broken. The ``watch:`` line clears the moment a poll comes back green, while
    the conversations a two-day fault dropped stay dropped — so the ledger's own
    record is reported beside it, worst first and only the worst three."""
    from thread_archive._ops import ingest_errors

    ingest_errors.reset_tally()
    for _ in range(10):  # rows land on powers of ten: this one records itself at 10
        ingest_errors.record(["cursor: scan failed: database is locked"], home=archive_home)
    ingest_errors.record(["codex: could not parse /tmp/session-1.jsonl"], home=archive_home)
    ingest_errors.record(["grok: unreadable export /tmp/export-2.zip"], home=archive_home)
    ingest_errors.record(["opencode: missing store /tmp/store-3"], home=archive_home)

    assert cli.report_status(_status_base()) == 0
    out = capsys.readouterr().out
    # "at least", because the count is a floor — the 200 sightings after a fault
    # last recorded itself at 1,000 are real and unwritten.
    assert "faults:  at least 13 ingest error(s) on record across 4 distinct fault(s)" in out
    assert "10x [cursor]" in out
    assert out.count("1x [") == 2  # the other three tie at one; three rows printed in all


def test_status_is_quiet_about_faults_on_an_install_that_had_none(archive_home, capsys) -> None:
    assert cli.report_status(_status_base()) == 0
    assert "faults:" not in capsys.readouterr().out


# ── `web`: the opener ────────────────────────────────────────────────────────
# Driven with $BROWSER pointed at a no-op command, so these run the real
# `webbrowser` path — the URL is genuinely handed off — without a window opening
# on whoever is running the suite.

@pytest.mark.viewer
def test_web_opens_the_viewer_url(monkeypatch, capsys, archive_home) -> None:
    monkeypatch.setenv("BROWSER", "true")
    assert cli.main(["web"]) == 0
    # The URL and nothing else: a plain open states no preference, and writes
    # none — the config it would write to is the operator's standing answer.
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:8787"
    assert not (archive_home / "config.json").exists()


@pytest.mark.viewer
def test_web_dev_puts_the_dev_panel_link_in_the_rail(monkeypatch, capsys, archive_home) -> None:
    """The switch is a line in the config, not a URL: the server reads it for
    every shell it serves, so the choice outlives this browser and this tab. It
    does not start the panels' server — it decides whether the rail names it."""
    from thread_archive._config import load_config

    monkeypatch.setenv("BROWSER", "true")
    assert cli.main(["web", "dev", "--port", "9999"]) == 0
    assert load_config(home=archive_home)["dev_panels"] is True
    out = capsys.readouterr().out
    assert "dev-panel link on" in out
    assert out.strip().endswith("http://127.0.0.1:9999")


@pytest.mark.viewer
def test_web_no_dev_takes_the_link_back_out(monkeypatch, capsys, archive_home) -> None:
    from thread_archive._config import load_config, save_config

    monkeypatch.setenv("BROWSER", "true")
    save_config({"dev_panels": True, "sources": {"claude-code": {"enabled": False}}},
                home=archive_home)
    assert cli.main(["web", "--no-dev"]) == 0
    cfg = load_config(home=archive_home)
    assert cfg["dev_panels"] is False
    # The switch edits one line; everything else the operator has decided stays.
    assert cfg["sources"] == {"claude-code": {"enabled": False}}
    assert "dev-panel link off" in capsys.readouterr().out


@pytest.mark.viewer
def test_web_refuses_to_write_over_a_config_it_could_not_read(
    monkeypatch, capsys, archive_home
) -> None:
    """A config too broken to parse may still hold a source policy someone is
    relying on. Rewriting it from an empty dict to flip one flag would drop
    that, so the switch declines and says which file to repair."""
    monkeypatch.setenv("BROWSER", "true")
    (archive_home / "config.json").write_text("{not json", encoding="utf-8")
    assert cli.main(["web", "dev"]) == 1
    assert (archive_home / "config.json").read_text(encoding="utf-8") == "{not json"
    assert "could not be read" in capsys.readouterr().err


# ── `source ingest`: the ingest-cost report ──────────────────────────────────


def test_source_ingest_on_an_archive_that_recorded_nothing(archive_home, capsys) -> None:
    assert cli.main(["source", "ingest"]) == 0
    assert "no ingest recorded in the last 24h" in capsys.readouterr().out


def test_source_ingest_says_when_the_ledger_is_the_reason_it_is_empty(
    archive_home, monkeypatch, capsys
) -> None:
    """A quiet window and an install that writes no ledger render identically, and
    only one of them is fixed by asking for a longer window."""
    monkeypatch.setenv("THREAD_ARCHIVE_INGEST_LOG", "0")
    assert cli.main(["source", "ingest"]) == 0
    out = capsys.readouterr().out
    assert "no ingest recorded in the last 24h" in out
    assert '"dev_mode": true' in out


def test_source_ingest_reports_sources_stages_and_upkeep(archive_home, capsys) -> None:
    """The report over a seeded ledger: a source table with percentiles, the
    stage ranking, the maintenance/embed lines beside (not inside) the source
    totals, and the retained-bytes footer."""
    from thread_archive._importers import _probe
    from thread_archive._watcher import ingest_log
    from thread_archive._watcher.base import WatchResult

    for pass_ms in (40.0, 90_000.0):  # one quick pass, one that formats as minutes
        with _probe.install() as probe:
            _probe.count("items", 2)
            _probe.count("events", 24)
            _probe.count("bytes", 4_000_000)
            with _probe.timed("parse_ms"):
                pass
        ingest_log.record_pass(
            "claude-code", home=archive_home, probe=probe, pass_ms=pass_ms,
            result=WatchResult(events_created=24, errors=["boom"]))
    ingest_log.record_maintenance(
        home=archive_home,
        timings={"ms": 700.0, "lock_ms": 300.0, "snapshot_ms": 90.0},
        counts={"threads": 3})
    ingest_log.record_embed(home=archive_home, embedded=16, elapsed_ms=500.0,
                            detail_ms={"encode_ms": 400.0})

    assert cli.main(["source", "ingest", "--hours", "24"]) == 0
    out = capsys.readouterr().out
    assert "ingest, last 24h" in out
    assert "claude-code" in out
    assert "2 errors" in out
    assert "where the time went:" in out
    assert "lock_ms" in out
    assert "maintenance" in out and "embed" in out
    assert "16 vectors" in out
    assert "ledger retains" in out


# ── report formatters: crash tolerance over an unreadable store ──────────────


def test_report_silenced_stays_quiet_over_an_unreadable_silence_store(capsys) -> None:
    """The silence store is a convenience over the records printed above it, so
    a status report must survive one it cannot read."""
    cli._report_silenced(None)  # notice_board(None) cannot even .get
    assert capsys.readouterr().out == ""
