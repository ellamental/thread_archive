"""MCP client detection + wiring for the setup flow.

Finds agent clients on this machine (currently: the ``claude`` CLI) and wires
the archive's read server into them, or produces the JSON config block for
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

from .._config import ENV_HOME, default_home, resolve_paths

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


def _home_env(home: Optional[str]) -> Optional[dict]:
    """The env block a server entry needs to serve ``home`` (arg, else this
    process's ``$THREAD_ARCHIVE_HOME``, else the default) — ``None`` when the
    target is the default home, where an env-less entry already resolves. Any
    other home must be pinned into the entry: the client launches the server
    with its own environment, not this setup run's."""
    target = resolve_paths(home).home
    if target == default_home():
        return None
    return {ENV_HOME: str(target)}


def mcp_config_block(home: Optional[str] = None) -> str:
    """The ``mcpServers`` JSON any MCP client accepts, absolute commands.
    Carries ``THREAD_ARCHIVE_HOME`` when ``home`` is not the default."""
    entry: dict = {"command": console_script("archive-mcp")}
    env = _home_env(home)
    if env:
        entry["env"] = env
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
    try:
        proc = _run([cli, "mcp", "get", server])
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if proc.returncode != 0:
        return False, None
    text = proc.stdout or ""
    if re.search(r"pending approval", text, re.IGNORECASE):
        return False, "the existing entry is still pending approval in claude"
    target = resolve_paths(home).home
    m = re.search(rf"{ENV_HOME}=(\S+)", text)
    entry_home = Path(m.group(1)).expanduser() if m else default_home()
    if entry_home != target:
        return False, (
            f"the existing entry serves {entry_home}, not this archive ({target})"
        )
    return True, None


def wire_claude(cli: str, home: Optional[str] = None) -> list[str]:
    """Add the read server to the claude CLI at user scope (all projects),
    carrying ``THREAD_ARCHIVE_HOME`` when ``home`` is not the default.

    Returns error strings, empty on full success.
    """
    errors: list[str] = []
    env = _home_env(home)
    for server, script in ((SEARCH_SERVER, "archive-mcp"),):
        argv = [cli, "mcp", "add", "--scope", "user", server]
        for key, value in (env or {}).items():
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
