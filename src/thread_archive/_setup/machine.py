"""The host the setup flow runs against — what it can ask it, what it changes.

Setup is the one part of the archive that acts on the machine *outside* the
archive home: it asks whether the always-on watcher, the nightly backup and the
curation drains are already scheduled for this home, whether this install has
the optional curation and embedding packages, and — with consent — it schedules
the LaunchAgents.

Every one of those goes through a :class:`Machine`, which the flow is handed
(:func:`..wizard.run_setup`, :func:`..wizard.print_status`) rather than reaching
for launchd itself. The default is this process's own host; a caller driving the
flow against another host model passes its own.

The agent probes are home-aware: LaunchAgents are per-user, so an agent pointed
at a different ``THREAD_ARCHIVE_HOME`` must not read as covering the home being
asked about. An agent whose plist sets no home env — the ``host/`` install shape
— is read as the default wiring, so an operator-installed agent counts as
covering the default home and setup leaves it alone.
"""

from __future__ import annotations

import importlib.util
import plistlib
import sys
from pathlib import Path
from typing import Optional

from .._config import default_home, resolve_paths

# The optional curation package's drains. archive only ever reads their state —
# scheduling them is that package's own daemon command.
LIBRARIAN_LABEL = "com.thread-archive.librarian"
GARDENER_LABEL = "com.thread-archive.gardener"


class Machine:
    """This host: the scheduled agents on it and the packages installed in it."""

    @property
    def macos(self) -> bool:
        """Whether the launchd-backed steps are available at all — the watcher
        and the nightly backup ship for macOS only."""
        return sys.platform == "darwin"

    # ── what is already scheduled ────────────────────────────────────────────

    def watcher_running(self, home: Optional[str] = None) -> bool:
        """Whether the always-on watcher is loaded and covers ``home``."""
        from .. import _launchd

        return self._agent_covers_home(_launchd.WATCHER_LABEL, home)

    def backup_running(self, home: Optional[str] = None) -> bool:
        """Whether the nightly-backup agent is loaded and covers ``home``."""
        from .. import _launchd

        return self._agent_covers_home(_launchd.BACKUP_LABEL, home)

    def backup_dest(self) -> Optional[str]:
        """Where the installed nightly-backup agent writes, or ``None`` when
        that can't be read off its plist (an external wrapper-script shape)."""
        from .. import _launchd

        return _launchd.backup_agent_dest()

    def curation_running(self, home: Optional[str] = None) -> bool:
        """Both curation drains scheduled for this home. A half-installed pair
        reads as not running."""
        return self._agent_covers_home(LIBRARIAN_LABEL, home) and self._agent_covers_home(
            GARDENER_LABEL, home
        )

    # ── what this install has ────────────────────────────────────────────────

    def curation_installed(self) -> bool:
        """Whether the optional curation package is importable in this env — the
        gate on every setup surface that would otherwise recommend a command the
        machine doesn't have."""
        return importlib.util.find_spec("thread_librarian") is not None

    def embeddings_installed(self) -> bool:
        """Whether semantic search is installed (the heavy ``[embeddings]``
        extra); without it the archive searches lexically."""
        try:
            return importlib.util.find_spec("sentence_transformers") is not None
        except (ImportError, ValueError):  # pragma: no cover — importlib edge
            return False

    # ── changes ──────────────────────────────────────────────────────────────

    def install_watcher(self, home: Optional[str] = None) -> None:
        """Schedule the always-on watcher for ``home``. Raises ``SystemExit``
        with the reason when launchd refuses the load."""
        from .. import _launchd

        _launchd.install_watcher(home)

    def install_backup(self, dest: str, home: Optional[str] = None) -> None:
        """Schedule the nightly backup pipeline for ``home`` → ``dest``. Raises
        ``SystemExit`` with the reason when launchd refuses the load."""
        from .. import _launchd

        _launchd.install_backup(dest, home)

    # ── the shared probe ─────────────────────────────────────────────────────

    def _agent_covers_home(self, label: str, home: Optional[str]) -> bool:
        if not self.macos:
            return False
        from .. import _launchd

        try:
            proc = _launchd._launchctl("print", f"gui/{_launchd._uid()}/{label}")
            if proc.returncode != 0:
                return False
        except OSError:  # pragma: no cover — launchctl missing
            return False
        try:
            plist = plistlib.loads(_launchd._plist_path(label).read_bytes())
            agent_home = plist.get("EnvironmentVariables", {}).get("THREAD_ARCHIVE_HOME")
        except (OSError, plistlib.InvalidFileException):
            agent_home = None  # loaded, plist unreadable — assume the default wiring
        # An agent with no plist home env runs against the *default* home —
        # launchd gives it no shell env, and this process's $THREAD_ARCHIVE_HOME
        # is not evidence (open_archive pins the currently-selected home there,
        # so reading it would make every agent appear to cover whatever home is
        # being asked about). Compare literal paths, not env-mediated resolution.
        agent_path = Path(agent_home).expanduser() if agent_home else default_home()
        return agent_path == resolve_paths(home).home
