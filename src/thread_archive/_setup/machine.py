"""The host the setup flow runs against — what it can ask it, what it changes.

Setup is the one part of the archive that acts on the machine *outside* the
archive home: it asks whether the always-on watcher and the nightly backup are
already scheduled for this home, whether this install has the optional
embeddings extra, and — with consent — it schedules the agents.

Every one of those goes through a :class:`Machine`, which the flow is handed
(:func:`..wizard.run_setup`, :func:`..wizard.print_status`) rather than reaching
for a service manager itself. The default is this process's own host; a caller
driving the flow against another host model passes its own. The scheduling
mechanism (launchd on macOS, systemd on Linux) is resolved behind
:mod:`.._service`, so the flow never names one.

The agent probes are home-aware: a scheduled agent is per-user, so one pointed at
a different ``THREAD_ARCHIVE_HOME`` must not read as covering the home being asked
about. An agent whose manifest sets no home env — the ``host/`` install shape — is
read as the default wiring, so an operator-installed agent counts as covering the
default home and setup leaves it alone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

from .._config import default_home, resolve_paths


class Machine:
    """This host: the scheduled agents on it and the packages installed in it."""

    @property
    def can_schedule(self) -> bool:
        """Whether this host has a service manager for the always-on pieces — the
        watcher and the nightly backup (launchd on macOS, systemd on Linux)."""
        from .. import _service

        return _service.can_schedule()

    @property
    def service_kind(self) -> Optional[str]:
        """The scheduler backing this host ("launchd"/"systemd"), or ``None`` — the
        marker the setup flow records for an install it made."""
        from .. import _service

        return _service.service_kind()

    # ── what is already scheduled ────────────────────────────────────────────

    def watcher_running(self, home: Optional[str] = None) -> bool:
        """Whether the always-on watcher is loaded and covers ``home``."""
        return self._agent_covers_home("watcher", home)

    def backup_running(self, home: Optional[str] = None) -> bool:
        """Whether the nightly-backup agent is loaded and covers ``home``."""
        return self._agent_covers_home("backup", home)

    def backup_dest(self) -> Optional[str]:
        """Where the installed nightly-backup agent writes, or ``None`` when
        that can't be read off its manifest (an external wrapper-script shape)."""
        from .. import _service

        return _service.backup_agent_dest()

    # ── what this install has ────────────────────────────────────────────────

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
        with the reason when the service manager refuses the load."""
        from .. import _service

        _service.install_watcher(home)

    def install_backup(self, dest: str, home: Optional[str] = None) -> None:
        """Schedule the nightly backup pipeline for ``home`` → ``dest``. Raises
        ``SystemExit`` with the reason when the service manager refuses."""
        from .. import _service

        _service.install_backup(dest, home)

    # ── the shared probe ─────────────────────────────────────────────────────

    def _agent_covers_home(self, agent: str, home: Optional[str]) -> bool:
        if not self.can_schedule:
            return False
        from .. import _service

        try:
            if not _service.agent_loaded(agent):
                return False
            agent_home = _service.agent_home(agent)
        except OSError:  # pragma: no cover — service-manager binary missing
            return False
        # An agent with no home in its manifest runs against the *default* home —
        # a scheduled process gets no shell env, and this process's
        # $THREAD_ARCHIVE_HOME is not evidence (open_archive pins the currently-
        # selected home there, so reading it would make every agent appear to
        # cover whatever home is being asked about). Compare literal paths, not
        # env-mediated resolution.
        agent_path = Path(agent_home).expanduser() if agent_home else default_home()
        return agent_path == resolve_paths(home).home
