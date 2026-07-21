"""Where a desktop harness keeps its data, per OS — the one place that mapping lives.

A CLI harness writes under a home-relative path (``~/.codex``, ``~/.grok``) that
ports across OSes for free. A desktop or Electron harness (Cursor, the Claude
app, VS Code) does not: its store sits under a different per-user root on each
OS, and a watcher that hard-codes one root silently captures nothing on the
others — the store is simply absent, so the watcher self-gates off and says
nothing, which reads as "no such conversations" rather than "wrong path".

A provider resolves its default store from here instead of re-deriving the
mapping in its own watcher, so the per-OS knowledge is written and reviewed once
and a source gains a new OS the day this function learns it.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Optional


def app_data_dir() -> Optional[Path]:
    """The per-user application-data root for this OS, or ``None`` where archive
    has no known one.

    The parent a desktop app's own data directory hangs off — a provider joins
    the app's name and the rest of its store path onto it::

        base = app_data_dir()
        db = base / "Cursor" / "User" / "globalStorage" / "state.vscdb" if base else None

    - **macOS** — ``~/Library/Application Support``
    - **Linux** — ``$XDG_CONFIG_HOME`` when it names an absolute path, else
      ``~/.config`` (matching where Electron apps put ``appData``)
    - **Windows** — ``%APPDATA%`` (``None`` when that variable is unset)

    ``None`` on any other platform. A provider treats ``None`` the same as an
    absent store — it stays dormant rather than raising — so an unsupported OS
    costs a source nothing.
    """
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support"
    if system == "Linux":
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg and os.path.isabs(xdg):
            return Path(xdg)
        return Path.home() / ".config"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else None
    return None
