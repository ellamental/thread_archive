"""The web viewer's server: a socket-free router + a thin stdlib HTTP adapter.

The whole read surface the UI needs already exists as the plain Python library
(:mod:`thread_archive._api`): ``search`` / ``read_thread`` / ``status``. This is a
skin over it — no ranking/fusion logic is duplicated here. :func:`route` is a pure
``(method, path, params) -> (status, content_type, body, headers)`` function so
tests drive it without opening a socket. :func:`serve_in_thread` runs it in a
background daemon thread so the always-on ``archive watch --web`` process can
cohost the viewer (one process, one engine) — that's how the read surface gets a
persistent URL with no extra daemon or standalone web verb.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .. import _api as api

STATIC_DIR = (Path(__file__).parent / "static").resolve()

log = logging.getLogger(__name__)

# The viewer has no auth and serves full search/read over the archive, so it
# binds loopback only. A non-loopback bind exposes the memory-of-record to the
# network and needs the explicit env opt-in checked in serve_in_thread.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_NONLOCAL_OPTIN = "THREAD_ARCHIVE_WEB_NONLOCAL"


def _host_allowed(host: Optional[str]) -> bool:
    """Whether a request's Host header names this loopback server.

    DNS-rebinding defense: a malicious page can point its own domain at 127.0.0.1
    and read the archive through the victim's browser — but the browser still sends
    that domain as Host, so requiring a loopback name blocks it. Handles the
    ``host:port`` and bracketed ``[::1]:port`` forms; an absent/empty Host is
    rejected (every real browser and HTTP/1.1 client sends one)."""
    if not host:
        return False
    host = host.strip()
    if host.startswith("["):  # [::1] or [::1]:8787
        end = host.find("]")
        if end == -1:
            return False
        name = host[1:end]
    elif host.count(":") == 1:  # host:port (a bare ::1 has two colons)
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    return name.lower() in _LOOPBACK_HOSTS

# Content types for the built bundle (vite emits hashed assets/*.js|css + fonts).
_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _json_default(obj):
    """Serialize the non-JSON-native values the api hits carry (datetimes)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


# Responses are uniform 4-tuples: (status, content_type, body, extra_headers).
Response = tuple[int, str, bytes, dict]


def _ok(obj) -> Response:
    return 200, "application/json", json.dumps(obj, default=_json_default).encode(), {}


def _text(status: int, msg: str) -> Response:
    return status, "text/plain; charset=utf-8", msg.encode(), {}


def _redirect(url: str) -> Response:
    return 302, "text/plain; charset=utf-8", b"", {"Location": url}


def _serve_file(p: Path, *, headers: Optional[dict] = None) -> Response:
    if not p.is_file():
        return _text(404, "not found")
    ctype = _CTYPES.get(p.suffix.lower(), "application/octet-stream")
    return 200, ctype, p.read_bytes(), headers or {}


def _first(params: dict, key: str) -> Optional[str]:
    v = params.get(key, [None])[0]
    return v if v else None


def _repeated(params: dict, key: str, limit: int = 10) -> list[str]:
    """Every value given for ``key``, in order, deduped and capped."""
    seen: list[str] = []
    for v in params.get(key, []):
        v = (v or "").strip()
        if v and v not in seen:
            seen.append(v)
    return seen[:limit]


def _int(params: dict, key: str, default: int, *, lo: int = 1, hi: int = 500) -> int:
    """Parse an int query param, clamped to ``[lo, hi]``. Unparseable values fall
    back to ``default``. The clamp is load protection: a negative limit reaches
    SQLite as ``LIMIT -1`` (unlimited) and a huge one is an unbounded read, and the
    viewer is unauthenticated, so no request gets to pick an unbounded value."""
    try:
        v = int(params.get(key, [default])[0])
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _bool(params: dict, key: str, default: bool) -> bool:
    v = _first(params, key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _csv(params: dict, key: str) -> Optional[list[str]]:
    v = _first(params, key)
    if not v:
        return None
    return [s for s in (x.strip() for x in v.split(",")) if s] or None


def _list_threads(*, limit: int, q: Optional[str]) -> list[dict]:
    """Recent conversation threads for the sidebar (newest first). Topics and
    archived threads are excluded; ``q`` filters on title/name substring."""
    from sqlalchemy import select

    from .._store import Thread, get_session

    api.open_archive()  # ensure the engine is up for this process' home
    stmt = (
        select(Thread.id, Thread.title, Thread.name, Thread.source, Thread.updated_at)
        .where(Thread.thread_type != "topic")
        .where(Thread.archived.is_(False))
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Thread.title.ilike(like) | Thread.name.ilike(like))
    stmt = stmt.order_by(Thread.updated_at.desc()).limit(limit)
    with get_session() as s:
        rows = s.execute(stmt).all()
    return [
        {
            "id": r.id,
            "title": r.title or r.name,
            "source": r.source,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def resolve_archive_link(link_id: str, source: Optional[str] = None) -> Optional[int]:
    """Resolve a provider session id to its archive thread id.

    This is the archive-link lookup an editor needs ("I have a session uuid, open the
    conversation"), and the same lookup that lets a bare uuid be pasted straight into
    ``/archive/<uuid>``. Resolution is the shared
    :func:`thread_archive._store.resolve.resolve_session_source_id` union —
    ``Thread.source_id`` (export importers never write ``ImportState``) plus the
    ``ImportState`` watermarks (a compaction continuation's uuid lives only there) —
    the same union the MCP reader uses, so both surfaces answer alike.
    Deliberately **no** integer primary-key branch: callers spray candidate ids
    that are expected not to resolve (see ``/api/archive-link``), and an all-digit
    junk candidate must never land on an unrelated PK. ``source`` narrows to one
    provider (an editor knows its own); omit it to resolve across every provider.
    Owned here: the watcher cohosts the persistent server, so the archive serves
    its own editor links."""
    from .._store import get_session, resolve_session_source_id

    api.open_archive()
    with get_session() as s:
        return resolve_session_source_id(
            s, link_id, source=source.replace("_", "-") if source else None
        )


# ---------------------------------------------------------------------------
# the router (socket-free, the testable core)
# ---------------------------------------------------------------------------
def route(method: str, path: str, params: dict) -> Response:
    """Resolve one request to ``(status, content_type, body, extra_headers)``."""
    if method != "GET":
        return _text(405, "method not allowed")

    # ---- JSON API: thin wrappers over thread_archive._api ----
    if path == "/api/health":
        # Cheap liveness for probes (the family manifest's health URL).
        # /api/status is the real survey but counts the whole index — seconds,
        # not the milliseconds a poller budgets.
        paths = api.open_archive()
        return _ok({"ok": paths.index_path.exists(), "home": str(paths.home)})

    if path == "/api/status":
        return _ok(api.status())

    if path == "/api/archive-link":
        # ``id`` may repeat: a caller that cannot tell which of the uuids it can
        # see is the session id sends every candidate, best guess first, and the
        # archive — the only party that knows what was actually imported — picks
        # the first that resolves. An editor webview holds ids for turns, drafts
        # and client-side threads that look exactly like a session uuid and can
        # never resolve; making them harmless beats guessing right.
        link_ids = _repeated(params, "id")
        if not link_ids:
            return _text(400, "missing id")
        source = _first(params, "source")
        for link_id in link_ids:
            tid = resolve_archive_link(link_id, source)
            if tid is not None:
                url = f"/archive/{tid}"
                if _bool(params, "redirect", False):
                    return _redirect(url)
                return _ok({"thread_id": tid, "url": url, "id": link_id})
        tried = ", ".join(link_ids)
        return 404, "application/json", json.dumps({"error": f"no thread for id={tried}"}).encode(), {}

    if path == "/api/search":
        q = (_first(params, "q") or "").strip()
        if not q:
            return _ok({"query": "", "hits": []})
        hits = api.search(
            q,
            limit=_int(params, "limit", 30),
            source=_csv(params, "source"),
            content_types=_csv(params, "content_types"),
            since=_first(params, "since"),
            until=_first(params, "until"),
        )
        return _ok({"query": q, "hits": hits})

    if path.startswith("/api/read/") or path.startswith("/api/thread/"):
        try:
            tid = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return _text(404, "bad thread id")
        thinking = _bool(params, "thinking", path.startswith("/api/thread/"))
        tools = _bool(params, "tools", True)
        if path.startswith("/api/thread/"):
            # structured render blocks (the viewer's reader)
            return _ok(api.read_thread_structured(tid, include_thinking=thinking, include_tools=tools))
        # flat transcript string (CLI-shaped; kept for back-compat). 'full' shows
        # tool calls (with thinking), 'chat' is the readable assistant text only.
        transcript = api.read_thread(tid, mode="full" if tools else "chat")
        return _ok({"thread_id": tid, "transcript": transcript})

    if path == "/api/threads":
        return _ok(
            {"threads": _list_threads(limit=_int(params, "limit", 100), q=_first(params, "q"))}
        )

    # unmatched API path — don't fall through to the SPA shell
    if path.startswith("/api/"):
        return _text(404, "not found")

    # ---- a real static asset from the built bundle (assets/*.js|css, favicon…) ----
    rel = path.lstrip("/")
    if rel:
        candidate = (STATIC_DIR / rel).resolve()
        if (candidate == STATIC_DIR or STATIC_DIR in candidate.parents) and candidate.is_file():
            # Vite emits content-hashed filenames under assets/ — a changed file
            # gets a new URL, so the browser may cache these forever.
            cache = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if rel.startswith("assets/") else None
            )
            return _serve_file(candidate, headers=cache)

    # ---- SPA fallback: every other path renders the app shell (client routing) ----
    return _serve_file(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# the HTTP adapter (the only networked part)
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — stdlib dispatch name
        # DNS-rebinding defense (see _host_allowed). The deliberate non-loopback
        # opt-in also disables the check: an exposed server is reached by a
        # non-loopback name by definition.
        if not _host_allowed(self.headers.get("Host")) and os.environ.get(_NONLOCAL_OPTIN) != "1":
            body = b"forbidden: bad Host header"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        parsed = urlparse(self.path)
        try:
            status, ctype, body, headers = route("GET", parsed.path, parse_qs(parsed.query))
        except Exception:  # noqa: BLE001 — isolate per request; never kill the loop
            # Detail stays server-side: exception text can carry paths/SQL/query
            # internals, and the body goes to whoever reached the port.
            log.exception("web request failed: %s", parsed.path)
            status, ctype, headers = 500, "application/json", {}
            body = json.dumps({"error": "internal error"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the foreground console quiet
        pass


def serve_in_thread(*, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    """Start the viewer on a background daemon thread and return the server.

    For ``archive watch --web``: the always-on watcher process cohosts the read
    surface so there's a persistent URL without a second daemon. Assumes the caller
    already opened the archive (the watcher does). The thread is a daemon, so it dies
    with the process; the caller may ``server_close()`` on shutdown for a clean stop.

    Refuses a non-loopback ``host`` unless ``THREAD_ARCHIVE_WEB_NONLOCAL=1``: the
    viewer is unauthenticated full read of the archive, so exposing it beyond the
    machine must be a deliberate act, not a typo'd ``--web-host``."""
    if host not in _LOOPBACK_HOSTS and os.environ.get(_NONLOCAL_OPTIN) != "1":
        raise ValueError(
            f"refusing non-loopback bind {host!r}: the viewer has no auth and serves "
            f"the full archive. Set {_NONLOCAL_OPTIN}=1 to expose it deliberately."
        )
    httpd = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=httpd.serve_forever, name="archive-web", daemon=True).start()
    return httpd
