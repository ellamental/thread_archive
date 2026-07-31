"""The local viewer, cohosted by the watcher (``thread-archive watch --web``).

Datasette-shaped: a tiny stdlib HTTP server bound to localhost, serving a single
self-contained page (search + reader) plus a few JSON endpoints that call
straight into :mod:`thread_archive._api`. It runs on a daemon thread inside the
always-on watcher process — no second daemon, no standalone CLI verb — and pulls
in no new dependency (stdlib only), so the package stays serverless and
Python-only.

Reading is the whole surface but one: an account export can be uploaded to the
drop zone the cohosting watcher imports from, so bringing a provider's account
history in doesn't require finding a folder in a terminal.
"""

from __future__ import annotations

from .server import (
    ROUTES,
    RequestBody,
    Route,
    resolve_archive_link,
    route,
    serve_in_thread,
)

__all__ = ["route", "ROUTES", "Route", "serve_in_thread", "resolve_archive_link",
           "RequestBody"]
