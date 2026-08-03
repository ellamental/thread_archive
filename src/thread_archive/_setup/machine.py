"""The host the setup flow runs against — what it can ask it, what it changes.

Setup is the one part of the archive that acts on the machine *outside* the
archive home: it asks whether the always-on watcher and the nightly backup are
already scheduled for this home, whether this install has the optional
embeddings extra, whether the cohosted viewer is answering — and, with consent,
it schedules the agents and opens that viewer in a browser.

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
import socket
import time
import webbrowser
from collections.abc import Callable
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

    def agent_label(self, agent: str) -> str:
        """This host's own id for one of the archive's agents — a launchd label,
        a systemd unit — for a report that names what it is about to touch."""
        from .. import _service

        return _service.label(agent)

    def agent_installed(self, agent: str) -> bool:
        """Whether the agent's manifest is on this host, loaded or not.

        The wider question than :meth:`watcher_running`: an agent whose manifest
        is present but whose process is down is still scheduled, and is still
        something an uninstall has to remove.
        """
        if not self.can_schedule:
            return False
        from .. import _service

        try:
            return _service.agent_installed(agent)
        except OSError:  # pragma: no cover — service-manager binary missing
            return False

    def agent_home(self, agent: str) -> Optional[str]:
        """The archive home the installed agent's manifest pins, or ``None`` when
        it pins none (the default-home wiring) or can't be read."""
        from .. import _service

        try:
            return _service.agent_home(agent)
        except OSError:  # pragma: no cover — service-manager binary missing
            return None

    def agent_covers_home(self, agent: str, home: Optional[str] = None) -> bool:
        """Whether the installed agent serves ``home`` — asked of the manifest,
        not of a running process."""
        return self._covers(self.agent_home(agent), home)

    # ── what this install has ────────────────────────────────────────────────

    def embeddings_installed(self) -> bool:
        """Whether semantic search is installed (the heavy ``[embeddings]``
        extra); without it the archive searches lexically."""
        try:
            return importlib.util.find_spec("sentence_transformers") is not None
        except (ImportError, ValueError):  # pragma: no cover — importlib edge
            return False

    def viewer_ready(self, port: int, *, attempts: int = 20, delay: float = 0.5) -> bool:
        """Whether the watcher's cohosted viewer accepts connections on ``port``.

        Polled rather than asked once: the watcher serving it may have been
        installed seconds ago and still be starting, and a browser pointed at it
        too early lands on a refused connection instead of the archive.
        """
        for attempt in range(attempts):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    return True
            except OSError:
                if attempt + 1 < attempts:
                    time.sleep(delay)
        return False

    # ── changes ──────────────────────────────────────────────────────────────

    def install_watcher(self, home: Optional[str] = None) -> None:
        """Schedule the always-on watcher for ``home``. Raises ``SystemExit``
        with the reason when the service manager refuses the load."""
        from .. import _service

        _service.install_agent("watcher", home)

    def install_backup(self, dest: str, home: Optional[str] = None) -> None:
        """Schedule the nightly backup pipeline for ``home`` → ``dest``. Raises
        ``SystemExit`` with the reason when the service manager refuses."""
        from .. import _service

        _service.install_agent("backup", home, dest=dest)

    def uninstall_agent(self, agent: str) -> None:
        """Unschedule an agent and remove its manifest. Raises ``SystemExit``
        with the reason when the service manager refuses."""
        from .. import _service

        _service.uninstall_agent(agent)

    def open_browser(
        self, url: str, *, opener: Callable[[str], bool] = webbrowser.open
    ) -> bool:
        """Open ``url`` in this host's browser. ``False`` when it has none — a
        headless box is a reason to print the URL, never to fail setup.

        ``opener`` is where "a browser" comes from: this host's, or another
        caller's, so a run can be driven without a window opening on a screen.
        """
        try:
            return bool(opener(url))
        except Exception:  # noqa: BLE001 — a browser is never worth failing setup over
            return False

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
        return self._covers(agent_home, home)

    @staticmethod
    def _covers(agent_home: Optional[str], home: Optional[str]) -> bool:
        """Whether an agent pinning ``agent_home`` serves the archive at ``home``.

        An agent with no home in its manifest runs against the *default* home — a
        scheduled process gets no shell env, so this process's own selection is
        not evidence about the agent's. Compare literal paths, not resolution
        through whatever this process happens to have open.
        """
        agent_path = Path(agent_home).expanduser() if agent_home else default_home()
        return agent_path == resolve_paths(home).home
