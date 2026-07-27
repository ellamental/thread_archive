"""MCP client detection + wiring for the setup flow.

Finds agent clients on this machine (currently: the ``claude`` CLI) and wires
the archive's read server into them — or back out again, when the archive is
being removed from the machine — or produces the JSON config block for
any other client. Commands are wired by **absolute path** to this
environment's console scripts — the client's runtime PATH may not include the
env that installed the package (a venv, a pipx/uv tool dir), and a bare name
that resolves today can silently stop resolving after a shell change.

Wiring is home-aware: a server entry with no ``THREAD_ARCHIVE_HOME`` in its
environment serves the *default* home, so when setup targets any other home
the generated config and the ``claude mcp add`` call both carry the env —
otherwise the "connected" agent silently searches the wrong archive.
Detection is more than a name match for the same reason: a server that is
pending approval, or whose environment points at a different home, is not
"already wired" for this setup.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .._config import ENV_HOME, ENV_MCP_INGEST, default_home, resolve_paths

SEARCH_SERVER = "thread-archive"

_SUBPROCESS_TIMEOUT = 30.0


def console_script(name: str) -> str:
    """Absolute path to one of this environment's console scripts, falling back
    to PATH, then to the bare name (last resort — still a valid MCP command for
    a client whose PATH has it)."""
    candidate = Path(sys.executable).with_name(name)
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    return found if found else name


def _home_env(home: Optional[str]) -> dict:
    """The explicit environment for a setup-generated stdio server.

    Setup's consent/source-selection flow opts this entry into zero-daemon
    catch-up. A custom archive home is pinned too; the default needs no home
    override, but still carries the ingest opt-in rather than relying on a
    hidden process default.
    """
    target = resolve_paths(home).home
    env = {ENV_MCP_INGEST: "1"}
    if target != default_home():
        env[ENV_HOME] = str(target)
    return env


def mcp_config_block(home: Optional[str] = None) -> str:
    """The ``mcpServers`` JSON any MCP client accepts, absolute commands.
    Catch-up ingest is explicit; a custom home is carried when needed."""
    entry: dict = {"command": console_script("archive-mcp"), "env": _home_env(home)}
    return json.dumps({"mcpServers": {SEARCH_SERVER: entry}}, indent=2)


def claude_cli() -> Optional[str]:
    return shutil.which("claude")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
    )


def claude_server_report(
    cli: str, home: Optional[str] = None, server: str = SEARCH_SERVER
) -> tuple[bool, Optional[str]]:
    """Whether the claude CLI already has a server entry that actually serves
    ``home`` — ``(wired, problem)``.

    ``(True, None)`` means a usable entry exists. ``(False, reason)`` names
    what disqualified the entry it found (pending approval, wrong archive
    home), or is ``(False, None)`` when there is no entry at all. A probe that
    itself fails (old CLI, weird install) reads as unwired with no problem —
    callers offer wiring, which is the safe direction.

    The name alone is not enough: ``claude mcp get`` returns 0 for a
    project-scoped server still pending approval, and for an entry whose
    environment pins a different archive than the one just set up.
    """
    text = _describe(cli, server)
    if text is None:
        return False, None
    if re.search(r"pending approval", text, re.IGNORECASE):
        return False, "the existing entry is still pending approval in claude"
    target = resolve_paths(home).home
    entry_home = _entry_home(text)
    if entry_home != target:
        return False, (
            f"the existing entry serves {entry_home}, not this archive ({target})"
        )
    return True, None


def _describe(cli: str, server: str) -> Optional[str]:
    """``claude mcp get``'s description of ``server``, or ``None`` when there is
    no such entry (or the probe itself failed)."""
    try:
        proc = _run([cli, "mcp", "get", server])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout or "" if proc.returncode == 0 else None


def _entry_home(text: str) -> Path:
    """The archive an entry serves: the home its environment pins, else the
    default (an entry with no ``THREAD_ARCHIVE_HOME`` serves that one)."""
    m = re.search(rf"{ENV_HOME}=(\S+)", text)
    return Path(m.group(1)).expanduser() if m else default_home()


def _entry_scope(text: str) -> Optional[str]:
    """The scope holding an entry, lowercased (``user`` / ``project`` /
    ``local``), or ``None`` when the description doesn't name one."""
    m = re.search(r"^\s*Scope:\s*(\S+)", text, re.MULTILINE)
    return m.group(1).lower() if m else None


def claude_removable(
    cli: str, home: Optional[str] = None, server: str = SEARCH_SERVER
) -> tuple[bool, Optional[str]]:
    """Whether claude holds an entry for this archive, and whether unwiring may
    take it — ``(present, reason_to_leave_it)``.

    ``(True, None)`` is setup's own wiring, there to be removed. ``(True,
    reason)`` is an entry that exists but is not this archive's to touch, and
    ``(False, None)`` is no entry at all.

    Two things disqualify one. An entry serving a *different* archive home is
    another install's. And an entry outside **user scope** is somebody else's
    wiring — a project's lives in a checkout's ``.mcp.json``, a file the archive
    does not own — where :func:`wire_claude` only ever writes user scope. A
    description that names no scope is left alone for the same reason: an
    uninstall that guesses wrong edits a repository.
    """
    text = _describe(cli, server)
    if text is None:
        return False, None
    scope = _entry_scope(text)
    if scope != "user":
        named = f"{scope}-scope" if scope else "an unrecognized scope's"
        return True, f"it is {named} wiring, not the user-scope entry setup writes"
    target = resolve_paths(home).home
    entry_home = _entry_home(text)
    if entry_home != target:
        return True, f"it serves {entry_home}, not this archive ({target})"
    return True, None


def unwire_claude(cli: str, server: str = SEARCH_SERVER) -> list[str]:
    """Remove the archive's user-scope server entry from the claude CLI — the
    exact inverse of :func:`wire_claude`.

    Scoped to ``user`` for the same reason :func:`claude_removable` requires it:
    that is the only scope setup writes, and a scopeless removal would reach into
    whatever project config happened to hold an entry of the same name.

    Returns error strings, empty on success.
    """
    try:
        proc = _run([cli, "mcp", "remove", "--scope", "user", server])
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"{server}: {e}"]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return [f"{server}: {detail[-1] if detail else 'claude mcp remove failed'}"]
    return []


def wire_claude(cli: str, home: Optional[str] = None) -> list[str]:
    """Add the read server to the claude CLI at user scope (all projects),
    carrying ``THREAD_ARCHIVE_HOME`` when ``home`` is not the default.

    Returns error strings, empty on full success.
    """
    errors: list[str] = []
    env = _home_env(home)
    for server, script in ((SEARCH_SERVER, "archive-mcp"),):
        argv = [cli, "mcp", "add", "--scope", "user", server]
        for key, value in env.items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--", console_script(script)]
        try:
            proc = _run(argv)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"{server}: {e}")
            continue
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            errors.append(f"{server}: {detail[-1] if detail else 'claude mcp add failed'}")
    return errors
