"""MCP client detection + wiring for the setup flow.

Finds agent clients on this machine (currently: the ``claude`` CLI) and wires
the archive's read server into them, or produces the JSON config block for
any other client. The curation write server (``archive-librarian-mcp``) is the
archive-librarian plugin's to wire — installing the plugin brings its own
``.mcp.json``. Commands are wired by **absolute path** to this
environment's console scripts — the client's runtime PATH may not include the
env that installed the package (a venv, a pipx/uv tool dir), and a bare name
that resolves today can silently stop resolving after a shell change.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

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


def mcp_config_block() -> str:
    """The ``mcpServers`` JSON any MCP client accepts, absolute commands."""
    return json.dumps(
        {
            "mcpServers": {
                SEARCH_SERVER: {"command": console_script("archive-mcp")},
            }
        },
        indent=2,
    )


def claude_cli() -> Optional[str]:
    return shutil.which("claude")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
    )


def claude_has_server(cli: str, server: str = SEARCH_SERVER) -> Optional[bool]:
    """Whether the claude CLI already knows the server; ``None`` when the probe
    itself fails (old CLI, weird install) — callers treat unknown as unwired."""
    try:
        return _run([cli, "mcp", "get", server]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def wire_claude(cli: str) -> list[str]:
    """Add the read server to the claude CLI at user scope (all projects).

    Returns error strings, empty on full success.
    """
    errors: list[str] = []
    for server, script in ((SEARCH_SERVER, "archive-mcp"),):
        argv = [cli, "mcp", "add", "--scope", "user", server, "--", console_script(script)]
        try:
            proc = _run(argv)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"{server}: {e}")
            continue
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            errors.append(f"{server}: {detail[-1] if detail else 'claude mcp add failed'}")
    return errors
