"""The dev panels' HTTP server — read-only, loopback, stdlib.

Shaped like the archive's own viewer and deliberately not merged with it: the
routes here answer about the machinery (how search performs, what the ledgers
say, what the bench has), and the point of the split is that the archive's
server carries none of them.

What it does share is the guards. ``_host_allowed`` (DNS-rebinding defense) and
the security headers are imported from :mod:`thread_archive._web.server` rather
than restated, because a second copy is a second thing to get wrong and the two
servers have the same exposure: no auth, full read of local data, reachable by
any page the browser is also showing. Both are dev-only now, so importing across
costs nothing at ship time — neither is in the wheel.

GET only. Every page here reads; nothing in the instruments writes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from thread_archive._web.server import (
    _LOOPBACK_HOSTS,
    _NONLOCAL_OPTIN,
    _SECURITY_HEADERS,
    Response,
    _bundled_asset,
    _first,
    _host_allowed,
    _int,
    _ok,
    _serve_file,
    _text,
)

from . import DEFAULT_PORT

STATIC_DIR = (Path(__file__).parent / "static").resolve()

log = logging.getLogger(__name__)


# ── the search lab, which lives beside this and not in the package ───────────

def _lab(name: str):
    """A ``search_lab`` module by name.

    The lab is a repo-root directory on no import path by default, so the root is
    resolved from this file rather than from the process's working directory —
    a server started from anywhere still finds it. It need not survive the lab's
    absence: devweb and the lab ship together, which is to say neither ships.
    """
    import importlib
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module(f"search_lab.{name}")


#: How long an assembled inventory is served before it is walked again. The read
#: is a filesystem walk over tens of GB of corpora, and what it describes changes
#: on the timescale of a benchmark run — so a page that refreshes must not turn
#: into a sweep per refresh, and a corpus built a minute ago still shows up while
#: the operator is still looking at the page.
_INVENTORY_TTL_S = 30.0


def _inventory_payload(module):
    """The bench inventory, assembled at most once per :data:`_INVENTORY_TTL_S`."""
    cached = getattr(_inventory_payload, "_cached", None)
    if cached is not None and time.time() - cached[0] < _INVENTORY_TTL_S:
        return cached[1]
    payload = module.inventory()
    _inventory_payload._cached = (time.time(), payload)  # type: ignore[attr-defined]
    return payload


def _serve_shell() -> Response:
    """The SPA shell.

    No dev-panel stamping: every page this server has is a dev panel, so there is
    no switch to carry. That flag existed to tell one bundle which half of itself
    to mount, and the bundles are separate now.
    """
    return _serve_file(STATIC_DIR / "index.html")


# ── the router: pure (method, path, params) -> Response, socket-free ─────────

def route(method: str, path: str, params: dict) -> Response:
    """Resolve one request. Driven directly by the tests; no sockets involved."""
    if method != "GET":
        return _text(405, "the dev panels are read-only")

    if path == "/api/retrieval":
        # Read straight off the ledgers rather than the index — this is the one
        # view whose subject is the *search pipeline*, not the corpus, so it must
        # keep answering while a rebuild has the index unavailable.
        report = _lab("retrieval_report")
        # Hours, not days: the short windows are where a regression shows up the
        # same afternoon it lands, and a day is the coarsest thing they can say.
        return _ok(report.report(
            hours=_int(params, "hours", report.DEFAULT_HOURS, hi=365 * 24)))

    if path == "/api/telemetry":
        # Intentionally separate from the archive's status/stats: it reads
        # retained histories across ledgers with different producers and caps.
        # `resolve_paths`, not `open_archive` — the ledgers are files under the
        # home, so this needs the location and never the SQLite engine, and a
        # panel that opened the archive would keep answering wrong during a
        # rebuild that has the index swapped out.
        from thread_archive._config import resolve_paths

        from . import telemetry

        return _ok(telemetry.report(
            resolve_paths().home,
            hours=_int(params, "hours", 24, hi=365 * 24),
        ))

    if path == "/api/search-lab":
        # What the bench has to measure with — benchmark rows and corpora. Read
        # off the lab's own registries and the cache root on disk, so it
        # describes the box rather than the index, and answers during a rebuild.
        return _ok(_inventory_payload(_lab("inventory")))

    if path == "/api/search-lab/runs":
        # Every recorded benchmark run, not the newest per row. Deliberately
        # outside the inventory's cache: that one is a walk over tens of GB held
        # for minutes, and a run that just finished has to appear here now.
        module = _lab("inventory")
        return _ok(module.runs(
            row=_first(params, "row"),
            limit=_int(params, "limit", module.RUNS_LIMIT, hi=100_000),
        ))

    if path.startswith("/api/search-lab/runs/") and path.endswith("/queries"):
        # One run's per-query detail — which queries it failed, and (with `vs`)
        # which ones moved against another run. Read from that run's own sidecar,
        # so this costs one file open and the runs list above costs none.
        module = _lab("inventory")
        run_id = unquote(path[len("/api/search-lab/runs/"):-len("/queries")])
        return _ok(module.queries(
            run_id,
            vs=_first(params, "vs"),
            limit=_int(params, "limit", module.QUERIES_LIMIT, hi=5_000),
        ))

    if path.startswith("/api/"):
        # Unmatched API path — don't fall through to the SPA shell, or a renamed
        # endpoint answers 200 with HTML and the caller parses it as JSON.
        return _text(404, "no such endpoint")

    # A real static asset from the built bundle (assets/*.js|css, favicon…).
    rel = path.lstrip("/")
    asset = _bundled_asset(STATIC_DIR, rel) if rel else None
    if asset is not None:
        # Vite emits content-hashed filenames under assets/, so those are
        # immutable; anything else must be revalidated.
        headers = ({"Cache-Control": "public, max-age=31536000, immutable"}
                   if rel.startswith("assets/") else None)
        return _serve_file(asset, headers=headers)

    # Anything else is a client route (/retrieval, /telemetry, /lab, /lab/run/:id).
    return _serve_shell()


# ── the socket layer ─────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def _send_security_headers(self) -> None:
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)

    def _refuse(self, status: int, message: str) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — stdlib dispatch name
        # DNS-rebinding defense (see _host_allowed): a page on any domain can
        # point its own name at 127.0.0.1, but the browser still sends that name
        # as Host. The deliberate non-loopback opt-in disables the check, since
        # an exposed server is reached by a non-loopback name by definition.
        if not (_host_allowed(self.headers.get("Host"))
                or os.environ.get(_NONLOCAL_OPTIN) == "1"):
            self._refuse(403, "forbidden: bad Host header")
            return
        parsed = urlparse(self.path)
        try:
            status, ctype, out, headers = route("GET", parsed.path, parse_qs(parsed.query))
        except Exception:  # noqa: BLE001 — isolate per request; never kill the loop
            # Detail stays server-side: exception text can carry paths and query
            # internals, and the body goes to whoever reached the port.
            log.exception("dev panel request failed: %s", parsed.path)
            status, ctype, headers = 500, "application/json", {}
            out = json.dumps({"error": "internal error"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self._send_security_headers()
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):  # keep the foreground console quiet
        pass


def _check_bundle() -> None:
    """Fail loudly at start if the bundle was never built.

    devweb's static/ is build output and is not committed — the panels are a
    maintainer's tool, and a committed bundle in a dev directory is diff noise on
    every frontend edit. So the missing-bundle case is normal on a fresh clone
    and deserves the instruction rather than a 404 per page.
    """
    if not (STATIC_DIR / "index.html").is_file():
        raise SystemExit(
            f"no built bundle at {STATIC_DIR}.\n"
            "Build it first:  cd devweb/frontend && npm install && npm run build"
        )


def serve(*, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Start the dev panels on a background daemon thread and return the server.

    Refuses a non-loopback ``host`` unless ``THREAD_ARCHIVE_WEB_NONLOCAL=1``, for
    the reason the viewer does: no auth, and it reads this machine's ledgers.
    """
    if host not in _LOOPBACK_HOSTS and os.environ.get(_NONLOCAL_OPTIN) != "1":
        raise ValueError(
            f"refusing non-loopback bind {host!r}: the dev panels have no auth and "
            f"read this machine's ledgers. Set {_NONLOCAL_OPTIN}=1 to expose them "
            f"deliberately."
        )
    _check_bundle()
    httpd = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=httpd.serve_forever, name="devweb", daemon=True).start()
    return httpd
