"""Setup + status for humans — the flow behind ``thread-archive setup``.

See :mod:`.wizard` for the flow, :mod:`.machine` for the host it reads and
installs onto, :mod:`.clients` for MCP client wiring, and :mod:`.uninstall`
for the reverse — the ``uninstall`` verb's removal flow and leftovers report. The ``setup`` verb of
:mod:`..cli` drives :func:`.wizard.run_setup`; ``main`` is that flow's
standalone dispatcher (``python -m thread_archive._setup``).
Private machinery like everything else underscore-prefixed — the command's
*existence* is the product surface, its internals are not.
"""

from __future__ import annotations

from .wizard import main

__all__ = ["main"]
