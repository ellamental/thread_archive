"""The read-only local viewer, cohosted by the watcher (``archive watch --web``).

Datasette-shaped: a tiny stdlib HTTP server bound to localhost, serving a single
self-contained page (search + reader) plus a few JSON endpoints that call
straight into :mod:`thread_archive._api`. It runs on a daemon thread inside the
always-on watcher process — no second daemon, no standalone CLI verb — and pulls
in no new dependency (stdlib only), so the package stays serverless and
Python-only.
"""

from __future__ import annotations

from .server import resolve_archive_link, route, serve_in_thread

__all__ = ["route", "serve_in_thread", "resolve_archive_link"]
