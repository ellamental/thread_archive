"""The dev panels — a separate server for the pages about the machinery.

Three pages whose subject is how the archive is *doing* rather than what it
holds: ``/retrieval`` (how search is performing, off the latency ledgers),
``/telemetry`` (the operational ledgers a maintainer reads together), and
``/lab`` (what the bench has to measure with, plus every recorded run).

They used to be routes inside the archive's own viewer, mounted by a
``dev_panels`` flag in config.json — which meant the shipped bundle carried
them, and the watcher that serves the archive also served them. They are their
own server now, on their own port, in a directory the wheel never sees. The
viewer at :8787 is the archive; this at :8789 is the instruments.

Nothing in ``src/thread_archive`` imports this. The dependency runs the other
way and only inward: this reads the archive's ledgers and the search lab's
registries the way ``search_lab/`` does — a repo-local consumer of private
modules, which is what a tool that ships with neither is free to be.

Run it with ``python -m devweb`` from the repo root.
"""

from __future__ import annotations

__all__ = ["DEFAULT_PORT", "route", "serve"]

#: Not 8787 (the archive's viewer) and not 8788 (the shared MCP server's
#: default) — the three run side by side on this machine.
DEFAULT_PORT = 8789


def __getattr__(name: str):
    # Lazy: importing the server pulls in the archive's private modules and the
    # search lab, which `python -m devweb --help` has no reason to pay for.
    if name in ("route", "serve"):
        from . import server

        return getattr(server, name)
    raise AttributeError(name)
