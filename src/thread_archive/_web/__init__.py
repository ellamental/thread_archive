"""``archive web`` — an on-demand local viewer over the archive.

Datasette-shaped: one CLI command starts a tiny stdlib HTTP server bound to
localhost, serves a single self-contained page (search + reader), and exposes a
few JSON endpoints that call straight into :mod:`thread_archive._api`. It is not a
daemon — it runs in the foreground and dies on Ctrl-C — and pulls in no new
dependency (stdlib only), so the package stays serverless and Python-only.
"""

from __future__ import annotations

from .server import resolve_archive_link, route, serve, serve_in_thread

__all__ = ["route", "serve", "serve_in_thread", "resolve_archive_link"]
