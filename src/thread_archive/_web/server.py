"""The web viewer's server: a socket-free router + a thin stdlib HTTP adapter.

The whole read surface the UI needs already exists as the plain Python library
(:mod:`thread_archive._api`): ``search`` / ``read_thread`` / ``status``. This is a
skin over it — no ranking/fusion logic is duplicated here. :func:`route` is a pure
``(method, path, params, body) -> (status, content_type, body, headers)`` function
so tests drive it without opening a socket. :func:`serve_in_thread` runs it in a
background daemon thread so the always-on ``thread-archive watch --web`` process can
cohost the viewer (one process, one engine) — that's how the read surface gets a
persistent URL with no extra daemon. ``thread-archive web`` opens that URL; it
never starts a server of its own.

A few endpoints write: ``POST /api/upload`` accepts an account-export ZIP into
the drop zone the cohosting watcher already imports from, and ``POST
/api/notices/{silence,unsilence}`` records which health notices the operator has
put aside. They carry their own cross-site guards (see :func:`_write_allowed`)
on top of the Host check every request passes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from .. import _api as api
from .. import _docs
from .._retrieval import _contention, _probe
from . import metrics as _metrics

if TYPE_CHECKING:
    from .._retrieval._types import EventHit

STATIC_DIR = (Path(__file__).parent / "static").resolve()

log = logging.getLogger(__name__)

# The viewer has no auth and serves full search/read over the archive, so it
# binds loopback only. A non-loopback bind exposes the memory-of-record to the
# network and needs the explicit env opt-in checked in serve_in_thread.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_NONLOCAL_OPTIN = "THREAD_ARCHIVE_WEB_NONLOCAL"
_SECURITY_HEADERS = {
    # Transcript text is untrusted. Keep every automatic subresource on this
    # loopback origin even if a renderer regression emits an external URL.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self'; font-src 'self'; connect-src 'self'; media-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


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


# A header no cross-origin HTML form can set. Sending it forces the browser to
# preflight the request, and this server answers no preflight — so a write can
# only come from a page this server served. Required on every write.
_WRITE_HEADER = "X-Archive-Write"


def _origin_allowed(origin: Optional[str]) -> bool:
    """Whether a write's ``Origin`` names this loopback server.

    The Host check above cannot see this attack: a page on any domain can post a
    form at ``http://127.0.0.1:8787`` and the browser sends *this server's* name
    as Host. What it also sends is the attacking page's Origin, which is not
    loopback. An absent Origin is rejected — every browser sends one on a
    cross-origin write, so its absence is not a same-origin request."""
    if not origin:
        return False
    parsed = urlparse(origin.strip())
    if parsed.scheme not in ("http", "https"):
        return False
    return _host_allowed(parsed.netloc)


def _write_allowed(headers) -> bool:
    """Whether a write request may proceed: same-origin, and unforgeable as a form.

    Both guards are needed. The custom header alone would pass a request from a
    page that has managed a preflight; a loopback Origin alone trusts a header
    non-browser clients set freely. Under the deliberate non-loopback opt-in the
    origin is by definition not loopback, so only the header requirement holds —
    exposing the viewer is already the act that accepts that."""
    if headers.get(_WRITE_HEADER) is None:
        return False
    if os.environ.get(_NONLOCAL_OPTIN) == "1":
        return True
    return _origin_allowed(headers.get("Origin"))


class _Readable(Protocol):
    """The one thing a request body's source has to do. Stated as a protocol so
    the router depends on ``read``, not on a socket — which is what lets a test
    hand it a ``BytesIO`` and drive the real upload path."""

    def read(self, size: int = ..., /) -> bytes: ...


class RequestBody:
    """An unread request body: the socket to read it from, and its declared length.

    Handed to :func:`route` instead of ``bytes`` because the one thing posted here
    is an account export, routinely gigabytes — buffering it whole to hand the
    router a ``bytes`` would spend the archive's memory on a file whose only
    destination is disk. :meth:`spool_to` streams it in chunks instead.

    ``remaining`` is what the HTTP adapter reads afterward to decide whether the
    connection can be kept alive: a body the router declined to read leaves bytes
    in the socket that the next request on that connection would parse as its own
    request line."""

    __slots__ = ("stream", "length", "remaining")

    def __init__(self, stream: _Readable, length: int) -> None:
        self.stream = stream
        self.length = length
        self.remaining = length

    def spool_to(self, path: Path) -> int:
        """Write the body to ``path`` in chunks; returns the bytes written.

        A short read means the client went away mid-upload; the count comes back
        below :attr:`length` and the caller discards the partial file rather than
        letting a truncated ZIP reach the drop zone."""
        with open(path, "wb") as fh:
            while self.remaining > 0:
                chunk = self.stream.read(min(_UPLOAD_CHUNK, self.remaining))
                if not chunk:
                    break
                fh.write(chunk)
                self.remaining -= len(chunk)
        return self.length - self.remaining


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


#: Stamped into the served shell when the operator has asked for the dev-panel
#: link. The panels are a different app on a different server; this tag is the
#: whole difference between a rail that offers a way over to them and one that
#: does not — see ``frontend/src/dev.ts``, which reads it, and
#: :func:`.._config.dev_panels`.
_DEV_PANELS_META = b'\n    <meta name="thread-archive-dev-panels" content="1">'


def _serve_shell() -> Response:
    """The SPA shell, carrying the operator's dev-link choice.

    A meta tag rather than an inline script or a JSON endpoint. Inline script is
    out because the CSP forbids it, and an endpoint is out because the answer is
    wanted at the first render: a rail that grew a link a fetch later would shift
    the navigation under a cursor already moving.

    The config is read per request — it is one small JSON file, and the point of
    a switch in a file is that flipping it shows up on the next page load rather
    than at the next restart of the watcher hosting this.
    """
    from .._config import dev_panels, load_config

    status, ctype, body, headers = _serve_file(STATIC_DIR / "index.html")
    if status == 200 and dev_panels(load_config()):
        body = body.replace(b"<head>", b"<head>" + _DEV_PANELS_META, 1)
    return status, ctype, body, headers


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


# The viewer's search snippet: the matched line plus one line of context on each
# side. A ceiling keeps a match inside a wall-of-text single line from dumping the
# whole line into the results list.
_VIEWER_SNIPPET_LINES = 1
_VIEWER_SNIPPET_MAX = 400


def _shape_search_hits(hits: "list[EventHit]", query: str) -> "list[EventHit]":
    """Rewrite each hit's ``snippet`` for the viewer: apply the hit thread's own
    provider display policy, then keep the matched line plus one line of context
    on each side.

    Applying the policy is what keeps a result row and the thread it opens
    showing the same text — a harness whose prompts arrive wrapped in scaffolding
    would otherwise match on a raw snippet the reader never displays. The policy
    is per hit, not per result list: one search spans every provider, so the
    lookup is grouped by thread and each hit gets its own thread's policy."""
    from .._providers import render_policy
    from .._retrieval._context import context_window
    from .._retrieval.read import _display_user_content

    sources = _hit_thread_sources(hits)
    policies = {src: render_policy(src) for src in set(sources.values())}
    for h in hits:
        render = policies.get(sources.get(h["thread_id"]))
        content = _display_user_content(h.get("full_content") or h.get("snippet") or "", render)
        snip = context_window(content, query, _VIEWER_SNIPPET_LINES).strip()
        if len(snip) > _VIEWER_SNIPPET_MAX:
            snip = snip[:_VIEWER_SNIPPET_MAX].rstrip() + " …"
        h["snippet"] = snip or (h.get("snippet") or "").strip()
    return hits


def _hit_thread_sources(hits: "list[EventHit]") -> "dict[str, Optional[str]]":
    """``{thread_id: source}`` for the threads these hits belong to.

    Hits already carrying ``thread_source`` (a browse row annotates it)
    are taken as they are; the rest are looked up in one query. A thread that has
    since vanished simply has no source and renders as stored."""
    from sqlalchemy import select

    from .._store import Thread, get_session

    known = {h["thread_id"]: h["thread_source"] for h in hits if h.get("thread_source")}
    missing = {h["thread_id"] for h in hits} - set(known)
    if missing:
        with get_session() as s:
            rows = s.execute(select(Thread.id, Thread.source).where(Thread.id.in_(missing))).all()
        known.update({r.id: r.source for r in rows})
    return known


def _quality_signal(hits: "list[EventHit]", query: str) -> Optional[dict]:
    """The match-quality signal the MCP render carries, JSON-shaped for the
    viewer: a top-hit verdict (strong / partial / weak / semantic, with the
    caution note the verdicts below strong carry) plus a ``term_hits`` count
    stamped on every hit — how many of the query's ``n_terms`` terms literally
    appear in it — so the UI can badge K/N per hit. None for a term-less query,
    matching the MCP header. Runs before :func:`_shape_search_hits` rewrites
    snippets: the count reads the raw hit text."""
    from .._retrieval.format import _search_quality, query_terms, term_hit_count

    terms = query_terms(query)
    if not hits or not terms:
        return None
    for h in hits:
        h["term_hits"] = term_hit_count(h.get("full_content") or h.get("snippet") or "", terms)
    verdict, note = _search_quality(hits[0]["term_hits"], len(terms))
    return {"verdict": verdict, "note": note, "n_terms": len(terms)}

def _page_facts(hits, limit: int) -> dict:
    """The pagination facts a result page carries, read off the
    :class:`.._retrieval._types.Results` the search returned.

    Read with ``getattr`` defaults rather than by type: ``rank`` and the test
    helpers hand back plain lists, and a page that can't state its position is
    better than an attribute error. ``exhaustive`` is the field that decides how
    the viewer may phrase ``total``: false means the ranked walk stopped at the
    pool it scored, so the total is real but paging cannot reach all of it —
    exactly the distinction the MCP renderer draws with ``of`` versus ``≥``.
    """
    return {
        "total": getattr(hits, "total", None),
        "total_threads": getattr(hits, "total_threads", None),
        "capped": bool(getattr(hits, "capped", False)),
        "exhaustive": bool(getattr(hits, "exhaustive", False)),
        "page": int(getattr(hits, "page", 1)),
        "pages": getattr(hits, "pages", None),
        "page_size": limit,
    }


def _subjects_payload(hits: "list[EventHit]") -> list[dict]:
    """The relevant-subjects lens over a result set, JSON-shaped: the
    topics these hits cluster under, each with how many result conversations it
    links. Empty when the lens is disabled or has nothing for these hits."""
    from .._retrieval import subjects as _subjects

    if not hits or not _subjects.enabled():
        return []
    return [
        {"topic_id": tid, "title": title, "chats": chats}
        for tid, title, chats in _subjects.subjects_for_results(hits)
    ]


# The whole-archive status survey (/api/status) behind a small TTL cache. It
# counts across every row — seconds on a large archive — while the status bar
# asks on every page load, so requests serve the cached survey and a stale one
# refreshes in the background; only the first request a process ever sees pays
# the full cost (and serve_in_thread prewarms, so in the cohosted watcher not
# even that). Keyed by (survey, home): one process normally serves one archive,
# but tests point the engine at a fresh home per test and must not read a stale
# survey of the previous one.
_STATUS_TTL = 60.0
_survey_lock = threading.Lock()
_survey_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_survey_refreshing: set[tuple[str, str]] = set()
# Cold-fill guard: the first request for a key computes, and any request that
# arrives while that pass is running waits for its result instead of starting a
# second. Without it the prewarm and the page load that races it both survey the
# whole archive, and the two contend for the same database — measurably slower
# than either pass alone.
_survey_inflight: dict[tuple[str, str], threading.Event] = {}
# How long a waiter blocks before deciding the filling thread is gone and taking
# the work itself. Only reached if that thread died without setting its event.
_SURVEY_WAIT_S = 60.0


def _refresh_survey(key: tuple[str, str], compute) -> dict:
    fresh = compute()
    with _survey_lock:
        _survey_cache[key] = (time.monotonic(), fresh)
        _survey_refreshing.discard(key)
    return fresh


def _survey(name: str, compute, ttl: float) -> dict:
    """One named survey, served from cache and refreshed off the request path."""
    key = (name, str(api.open_archive().home))
    while True:
        with _survey_lock:
            cached = _survey_cache.get(key)
            if cached is not None:
                age = time.monotonic() - cached[0]
                if age >= ttl and key not in _survey_refreshing:
                    _survey_refreshing.add(key)
                    threading.Thread(
                        target=_refresh_survey, args=(key, compute),
                        name=f"archive-web-{name}", daemon=True,
                    ).start()
                return cached[1]
            existing = _survey_inflight.get(key)
            mine = existing is None
            waiting = existing if existing is not None else threading.Event()
            if mine:
                _survey_inflight[key] = waiting
        if mine:
            try:
                return _refresh_survey(key, compute)
            finally:
                # Wake the waiters whether the pass succeeded or raised: on a
                # failure they find the cache still empty and one of them retries,
                # rather than every waiter blocking out its full timeout.
                with _survey_lock:
                    _survey_inflight.pop(key, None)
                waiting.set()
        waiting.wait(_SURVEY_WAIT_S)


def _status() -> dict:
    """The survey's counts from cache; its operational records read fresh.

    Only the counts are expensive, and only they tolerate age. The records
    (``api.operational_records``) are what the health page ages against *now*,
    so serving a cached copy of them turns idle time into a fault: nothing
    refreshes this cache but a request, so a page opened after twenty quiet
    minutes would read a twenty-minute-old "capture last checked" stamp and call
    a perfectly live watcher stalled."""
    return {
        **_survey("status", api.status, _STATUS_TTL),
        **api.operational_records(),
    }


def _list_sources() -> list[dict]:
    """Distinct conversation sources with thread counts, biggest first — the
    vocabulary for the search page's source filter (topics and archived threads
    are outside the search corpus, so they don't vote)."""
    from sqlalchemy import func, select

    from .._store import Thread, get_session

    api.open_archive()
    with get_session() as s:
        rows = s.execute(
            select(Thread.source, func.count())
            .where(Thread.thread_type != "topic")
            .where(Thread.archived.is_(False))
            .group_by(Thread.source)
            .order_by(func.count().desc(), Thread.source)
        ).all()
    return [{"source": r[0], "threads": r[1]} for r in rows if r[0]]


# Thread types the recent list hides when no explicit ``types`` filter is given:
# topics are separate artifacts, and 'system' threads (Task-tool subagent runs —
# see the claude-code importer) are machinery, not sessions someone opens by
# recency. Both stay reachable through /api/threads?types=….
_DEFAULT_HIDDEN_TYPES = ("topic", "system")


def _list_threads(
    *,
    limit: int,
    page: int,
    q: Optional[str],
    types: Optional[list[str]] = None,
) -> dict:
    """Recent threads by last *activity* — the newest event's ``occurred_at``,
    falling back to the row's ``updated_at`` for event-less threads. The raw
    ``updated_at`` column can't mean "recently active" here: it is the truth
    checkpoint's dirty-flag, bumped by any metadata write (a stored-summary
    write would re-surface a years-old thread) and untouched by event ingest. With
    ``types`` given, exactly those ``thread_type`` values are listed; without
    it, topics and system threads (subagent runs) are hidden — the sidebar's
    default. Archived threads never list; ``q`` filters on title/name
    substring. Results are paginated rather than silently capped: the response
    carries the matching total and enough page metadata for every row to remain
    reachable. Each row also carries a compact preview of its first non-empty
    user message."""
    from sqlalchemy import DateTime, func, select
    from sqlalchemy.sql.elements import ColumnElement

    from .._store import Event, Thread, get_session

    api.open_archive()  # ensure the engine is up for this process' home
    newest_event_at = (  # one (thread_id, id) index seek per candidate row
        select(Event.occurred_at)
        .where(Event.thread_id == Thread.id)
        .order_by(Event.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    last_active_at = func.coalesce(
        newest_event_at, Thread.updated_at, type_=DateTime(timezone=True)
    ).label("last_active_at")
    user_content = func.trim(
        func.json_extract(Event.payload, "$.content"), " \t\r\n"
    )
    first_user_message = (
        select(func.substr(user_content, 1, 200))
        .where(
            Event.thread_id == Thread.id,
            Event.event_type.in_(("user_message_sent", "thread_message_sent")),
            func.length(user_content) > 0,
        )
        .order_by(Event.id.asc())
        .limit(1)
        .scalar_subquery()
        .label("first_user_message")
    )
    filters: list[ColumnElement[bool]] = [Thread.archived.is_(False)]
    if types:
        filters.append(Thread.thread_type.in_(types))
    else:
        filters.append(Thread.thread_type.not_in(_DEFAULT_HIDDEN_TYPES))
    if q:
        like = f"%{q}%"
        filters.append(Thread.title.ilike(like) | Thread.name.ilike(like))

    stmt = (
        select(Thread.id, Thread.title, Thread.name, Thread.source,
               Thread.thread_type, last_active_at, first_user_message)
        .where(*filters)
        .order_by(last_active_at.desc(), Thread.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    with get_session() as s:
        total = int(
            s.execute(
                select(func.count()).select_from(Thread).where(*filters)
            ).scalar_one()
        )
        rows = s.execute(stmt).all()
    threads = [
        {
            "id": r.id,
            "title": r.title or r.name,
            "source": r.source,
            "thread_type": r.thread_type,
            # the row's date in list consumers: last activity, not the raw column
            "updated_at": r.last_active_at.isoformat() if r.last_active_at else None,
            "first_user_message": r.first_user_message,
        }
        for r in rows
    ]
    return {
        "threads": threads,
        "total": total,
        "page": page,
        "page_size": limit,
        "pages": (total + limit - 1) // limit,
    }


def _list_thread_types() -> list[dict]:
    """Distinct thread types with counts, biggest first — the vocabulary for the
    all-threads page's type filter. Archived threads don't vote (they don't
    list), so a type that only exists archived doesn't offer a dead checkbox."""
    from sqlalchemy import func, select

    from .._store import Thread, get_session

    api.open_archive()
    with get_session() as s:
        rows = s.execute(
            select(Thread.thread_type, func.count())
            .where(Thread.archived.is_(False))
            .group_by(Thread.thread_type)
            .order_by(func.count().desc(), Thread.thread_type)
        ).all()
    return [{"thread_type": r[0], "threads": r[1]} for r in rows if r[0]]


# ---------------------------------------------------------------------------
# the drop zone: uploading an account export
# ---------------------------------------------------------------------------
# An upload lands in ``<home>/dumps/`` and is imported by the export-drop watcher
# cohosting this server — the page hands the ZIP to the same folder a Finder drag
# would, so there is one import path with one set of settle / retain / quarantine
# rules (:mod:`thread_archive._watcher.export_drop`) rather than a second copy of
# them here. Importing inline would also hold a request thread for however long a
# multi-gigabyte export takes.
#
# The write is a spool to a dot-prefixed temp file plus an atomic rename, so the
# watcher never sees a partially-written drop: it skips dotfiles, and what appears
# under a scanned name appears whole.

_UPLOAD_CHUNK = 1 << 20
_UNSAFE_DROP_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_DROP_NAME_MAX = 120
_ZIP_SUFFIX = ".zip"


def free_margin() -> int:
    """Free space an upload must leave behind it (1 GiB by default).

    An account export is a large file, and the archive it feeds grows again while
    importing it — filling the disk to the last byte would break the very import
    the upload exists for, and take the watcher's other sources down with it.
    Raise ``THREAD_ARCHIVE_UPLOAD_FREE_MARGIN`` on a machine that needs more
    headroom than that."""
    return int(os.environ.get("THREAD_ARCHIVE_UPLOAD_FREE_MARGIN") or 1 << 30)


def _drop_filename(raw: str) -> Optional[str]:
    """A safe ``dumps/`` filename for an uploaded export, or None if it isn't one.

    Only ``.zip`` is accepted: all three account exports download as one, and the
    drop watcher's scan considers nothing else at top level — a name it would
    never look at is not a drop, it is litter that sits in the folder forever.
    The name is reduced to its basename (both separators, since the uploader's
    machine picks which) and to a conservative character set, with leading dots
    stripped: the watcher skips dotfiles, and ``..`` must not survive as a name."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name.lower().endswith(_ZIP_SUFFIX):
        return None
    stem = _UNSAFE_DROP_CHARS.sub("-", name[: -len(_ZIP_SUFFIX)]).strip("-.")
    return (stem[:_DROP_NAME_MAX] or "export") + _ZIP_SUFFIX


def _unique_drop_path(dumps: Path, filename: str) -> Path:
    """``dumps/<filename>``, numbered until it names nothing that already exists.

    The number goes before the extension, never after it: the watcher only
    considers files whose suffix is ``.zip``, so a ``foo.zip.1`` would sit in the
    drop zone unseen and unimported."""
    dest = dumps / filename
    stem = filename[: -len(_ZIP_SUFFIX)]
    n = 1
    while dest.exists():
        dest = dumps / f"{stem}-{n}{_ZIP_SUFFIX}"
        n += 1
    return dest


def _classify_drop(path: Path) -> tuple[Optional[str], Optional[str]]:
    """``(kind, label)`` of the first registered provider that claims this bundle.

    The drop watcher's own claim, asked early — whichever spec answers here is the
    one that will import the file, and a bundle nothing claims is one nothing
    would have imported. A ``detect`` that raises is "not mine", exactly as in the
    watcher: a provider that cannot decide must not block one that can."""
    from .._providers import export_specs

    for provider, spec in export_specs():
        try:
            if spec.detect(path):
                return spec.kind, spec.label
        except Exception:  # noqa: BLE001 — a broken detect must not block the rest
            log.exception(
                "upload: provider %r failed to classify %s — treated as not its export",
                provider.name, path.name,
            )
    return None, None


def _export_labels() -> str:
    """The exports this install can import, for an error the uploader can act on."""
    from .._providers import export_specs

    return ", ".join(spec.label for _, spec in export_specs()) or "none registered"


def _receive_drop(raw_name: str, body: RequestBody) -> Response:
    """Take an uploaded account export into the drop zone.

    Spooled, classified, then renamed into place, so what the watcher eventually
    scans is a complete bundle some provider has already claimed. An unrecognized
    ZIP is refused here rather than dropped: the watcher would only quarantine it
    seconds later, and the uploader still has the download, so an answer now is
    worth more than a file in ``failed/``. Hand-dropping into the folder stays the
    escape hatch for a shape this classifier hasn't learned yet."""
    filename = _drop_filename(raw_name)
    if filename is None:
        return _text(400, "an account export uploads as a .zip")
    dumps = api.open_archive().dumps_dir
    try:
        dumps.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _text(500, f"cannot open the drop zone: {e}")
    try:
        dumps.chmod(0o700)  # conversation content: the home's privacy, re-asserted
    except OSError:
        pass

    if body.length + free_margin() > shutil.disk_usage(dumps).free:
        return _text(507, "not enough free disk space for this export")

    tmp = dumps / f".upload-{os.getpid()}-{uuid4().hex}.part"
    try:
        written = body.spool_to(tmp)
        if written != body.length:
            tmp.unlink(missing_ok=True)
            return _text(400, "the upload ended early — nothing was imported")
        kind, label = _classify_drop(tmp)
        if kind is None:
            tmp.unlink(missing_ok=True)
            return _text(
                415,
                f"not a recognized account export ({_export_labels()}). "
                f"Upload the ZIP as downloaded, without unpacking or repacking it.",
            )
        dest = _unique_drop_path(dumps, filename)
        os.replace(tmp, dest)
    except OSError as e:
        log.warning("upload: could not write %s into the drop zone: %s", filename, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return _text(500, f"could not write the upload: {e}")
    log.info("upload: accepted %s export %s (%d bytes)", label, dest.name, written)
    return _ok({"name": dest.name, "kind": kind, "label": label,
                "bytes": written, "dumps_dir": str(dumps)})


def _drop_entry(path: Path) -> dict:
    """One drop as ``{name, bytes, at}``. A directory reports no size — summing a
    whole unpacked export's tree on every poll costs more than the number is worth."""
    try:
        st = path.stat()
        return {
            "name": path.name,
            "bytes": None if path.is_dir() else st.st_size,
            "at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        }
    except OSError:  # vanished mid-scan — the watcher moves these under us
        return {"name": path.name, "bytes": None, "at": None}


def _drop_entries(directory: Path, *, skip: tuple[str, ...] = ()) -> list[dict]:
    """Every drop in one directory, newest first. Dotfiles (in-flight uploads) and
    the reserved subdirs are not drops and never list."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    out = [
        _drop_entry(e) for e in entries
        if not e.name.startswith(".") and e.name not in skip
    ]
    out.sort(key=lambda e: (e["at"] or "", e["name"]), reverse=True)
    return out


def _drops() -> dict:
    """The drop zone's census: waiting to import, imported, needs a look.

    The uploader's honest progress signal, because the import is the watcher's
    work and not this server's. A drop leaves ``waiting`` when the watcher has
    taken it, and reappears under ``imported`` (kept as the recovery copy — the
    normalized truth can't be assumed to carry everything the download did) or
    under ``failed`` (quarantined for review, never deleted)."""
    from .._watcher.export_drop import (
        IMPORTED_DIRNAME,
        QUARANTINE_DIRNAME,
        RESERVED_DIRNAMES,
    )

    dumps = api.open_archive().dumps_dir
    imported: list[dict] = []
    for kind_dir in sorted((dumps / IMPORTED_DIRNAME).glob("*")):
        if kind_dir.is_dir():
            imported += [{**e, "kind": kind_dir.name} for e in _drop_entries(kind_dir)]
    imported.sort(key=lambda e: (e["at"] or "", e["name"]), reverse=True)
    return {
        "dumps_dir": str(dumps),
        "waiting": _drop_entries(dumps, skip=RESERVED_DIRNAMES),
        "imported": imported,
        "failed": _drop_entries(dumps / QUARANTINE_DIRNAME),
    }


def resolve_archive_link(link_id: str, source: Optional[str] = None) -> Optional[str]:
    """Resolve a pasted/sprayed ref to its archive (ULID) thread id.

    This is the archive-link lookup an editor needs ("I have a session uuid, open the
    conversation"), and the same lookup that lets a bare ref be pasted straight into
    ``/archive/<ref>``. Without ``source`` it is exactly
    :func:`thread_archive._retrieval.read.resolve_thread_ref` — the three ref
    shapes every surface accepts: a ULID (the primary key), an all-digit legacy
    integer alias (``Thread.legacy_id``, a permanent alias — pasted integer links
    keep resolving), or a provider session id via the shared
    :func:`thread_archive._providers.resolve_session_ref` union
    (``Thread.source_id`` plus the ``ImportState`` watermarks — a compaction
    continuation's uuid lives only there), the same union the MCP reader uses, so
    every surface answers alike. ``source`` narrows to one provider (an editor
    knows its own and sprays session-id candidates only — see
    ``/api/archive-link``), so with it resolution is session-id-only: a sprayed
    junk candidate must never land on an unrelated thread through the ULID or
    legacy-alias branches. Owned here: the watcher cohosts the persistent server,
    so the archive serves its own editor links."""
    from .._providers import resolve_session_ref
    from .._retrieval.read import resolve_thread_ref
    from .._store import get_session

    api.open_archive()
    with get_session() as s:
        if source:
            return resolve_session_ref(s, link_id, source=source.replace("_", "-"))
        return resolve_thread_ref(s, link_id)


#: What ``/api/search`` serves when the client names no ``limit``. The viewer
#: always names one (it paints a fixed page), so this is the bare-URL case.
SEARCH_LIMIT = 30


def _search_shape(params: dict) -> tuple[int, int]:
    """The ``(limit, page)`` a search asked for.

    One reader for the route that serves the search and the dispatcher that
    records it, so the recorded shape is the shape that ran — a second copy of the
    default here would file a bare-URL search under whatever number this one
    drifted to.
    """
    return _int(params, "limit", SEARCH_LIMIT), _int(params, "page", 1, hi=1_000_000)


# ---------------------------------------------------------------------------
# the router (socket-free, the testable core)
# ---------------------------------------------------------------------------
def route(
    method: str, path: str, params: dict, body: Optional[RequestBody] = None
) -> Response:
    """Resolve one request to ``(status, content_type, body, extra_headers)``.

    ``body`` is the unread request body, present only on a write. The adapter has
    already established that a write may be made at all — the router's job is
    what it means, not whether it is allowed."""
    if method == "POST":
        if path == "/api/upload":
            if body is None:
                return _text(400, "missing upload body")
            return _receive_drop(_first(params, "name") or "", body)
        if path in ("/api/notices/silence", "/api/notices/unsilence"):
            # The notice key rides the query string rather than a JSON body: the
            # write guard is the header and Origin, not the content type, and
            # keeping the one body-reading endpoint the one that needs a body
            # (a multi-gigabyte export) leaves the router with nothing to parse.
            key = _first(params, "key")
            if not key:
                return _text(400, "missing notice key")
            try:
                if path.endswith("/silence"):
                    return _ok(api.silence_notice(key))
                return _ok(api.unsilence_notice(key))
            except KeyError:
                # Silencing something that isn't firing would park a silence in
                # the store waiting to hide a future occurrence — refuse, and say
                # so, rather than accept a write with no condition behind it.
                return _text(404, f"no active notice with key {key!r}")
        # Every other path is a read surface, so the method is what is wrong with
        # this request — not the address.
        return _text(405, "method not allowed")
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
        return _ok(_status())

    if path == "/api/notices":
        # The action queue with silences applied. Its own endpoint rather than a
        # field on /api/status: it is cheap (records + import probes, no index
        # counting), and silencing one has to re-read it immediately — which must
        # not mean re-running the survey behind status.
        return _ok(api.notices())

    if path == "/api/loads":
        # Live load progress + recent runs. Cheap by construction — two small
        # files off the home, no index counting — so a page watching a running
        # load can poll it without competing with the load for the store.
        return _ok(api.load_status(limit=_int(params, "limit", 20, hi=200)))

    if path == "/api/disk":
        # What the home costs, by kind. Its own endpoint rather than a field on
        # /api/status because it walks the directory tree: the health page polls
        # status every 30s and has no reason to re-walk 40k files that often.
        # This one is polled too, and several viewers poll it at once, so it takes
        # the staleness budget that keeps those onto one walk — a served number
        # that is seconds old is indistinguishable from a fresh one at this scale.
        from .._ops.disk import POLL_MAX_AGE_S

        return _ok(api.disk_usage(max_age_s=POLL_MAX_AGE_S))

    if path == "/api/drops":
        # The drop zone as the upload page reads it. Directory listings only —
        # cheap enough to poll while an import the watcher owns runs elsewhere.
        return _ok(_drops())

    if path == "/api/sources":
        return _ok({"sources": _list_sources()})

    if path == "/api/stats":
        # Token/cost analytics. Backed by an incrementally-maintained rollup
        # (_store._metrics), so only the first survey on a fresh cache is slow —
        # thereafter it folds just new events. By default every model is listed (no
        # silent cap); `?models=N` optionally caps the by-model list.
        models = _first(params, "models")
        limit = int(models) if models and models.isdigit() else None
        return _ok(api.stats(model_limit=limit))

    if path.startswith("/api/stats/model/"):
        # Per-model drill-down. The tail is the model name — taken whole (model ids
        # like 'deepseek/deepseek-v4-pro' contain slashes) and percent-decoded (the
        # SPA links with encodeURIComponent; a hand-typed literal slash works too).
        model = unquote(path[len("/api/stats/model/"):])
        detail = api.model_stats(model)
        if detail is None:
            return 404, "application/json", json.dumps({"error": f"no data for model {model!r}"}).encode(), {}
        return _ok(detail)

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

    if path.startswith("/api/blob/"):
        # A blob-store file (extracted/materialized image or document content —
        # see _truth.blobs), addressed by content hash; the reader's <img> tags
        # point here. The name regex is the traversal guard, and content
        # addressing makes the response immutable, so cache it hard.
        m = re.match(r"^([0-9a-f]{64})(\.[A-Za-z0-9]{1,8})?$", path[len("/api/blob/"):])
        if not m:
            return _text(404, "bad blob name")
        api.open_archive()
        from .._truth.blobs import blob_file, media_type_for_path

        # The URL's extension, when it carries one, is what resolves the file: the
        # same bytes can be stored under several (one message's image/png is
        # another's image/svg+xml), and a link that said .png must not be answered
        # with an SVG's Content-Type.
        bp = blob_file(m.group(1), ext=m.group(2))
        if bp is None:
            return _text(404, "no such blob")
        try:
            blob = bp.read_bytes()
        except OSError:
            return _text(404, "no such blob")
        return 200, media_type_for_path(bp), blob, {
            "Cache-Control": "public, max-age=31536000, immutable",
            # Blob content is untrusted: its bytes and its declared media type both
            # came out of an archived payload. Most of it is inert as an image, but
            # a stored SVG *navigated to* — the reader's images link through to
            # full size — is a document on this origin, and the page-level policy
            # only stops it scripting, not painting. `sandbox` drops it into an
            # opaque origin, so what it can impersonate is nothing. Documents only:
            # a response CSP does not apply to a subresource, so the <img> that
            # renders the same blob inline is unaffected.
            "Content-Security-Policy": "sandbox",
        }

    if path == "/api/search":
        q = (_first(params, "q") or "").strip()
        # Both shapes page. `page` is 1-based and every page is a slice of ONE
        # ordering (see _retrieval.search) — the viewer walks a result set rather
        # than being handed a cut and told to narrow the query.
        limit, page = _search_shape(params)
        if not q:
            # Empty query = browse, the same contract as MCP thread_search: one
            # row per thread by last activity, honoring the structural filters.
            # Rows carry thread_source / n_events; content options don't apply.
            hits = api.search(
                "",
                limit=limit,
                page=page,
                source=_csv(params, "source"),
                since=_first(params, "since"),
                until=_first(params, "until"),
                agents=_first(params, "agents"),
            )
            return _ok({"query": "", "browse": True, "hits": hits,
                        "quality": None, "subjects": _subjects_payload(hits),
                        **_page_facts(hits, limit)})
        hits = api.search(
            q,
            limit=limit,
            page=page,
            source=_csv(params, "source"),
            content_types=_csv(params, "content_types"),
            since=_first(params, "since"),
            until=_first(params, "until"),
            agents=_first(params, "agents"),
            context_lines=0,  # the viewer builds its own ±1 snippet from full_content
        )
        quality = _quality_signal(hits, q)
        return _ok({"query": q, "browse": False, "hits": _shape_search_hits(hits, q),
                    "quality": quality, "subjects": _subjects_payload(hits),
                    **_page_facts(hits, limit)})

    if path.startswith("/api/read/") or path.startswith("/api/thread/"):
        # The tail is a thread ref — a ULID id, a legacy integer alias, or a
        # provider session id — resolved the same way every other surface does.
        ref = unquote(path.rsplit("/", 1)[-1])
        tid = resolve_archive_link(ref)
        if tid is None:
            return _text(404, f"no thread with id {ref}")
        thinking = _bool(params, "thinking", path.startswith("/api/thread/"))
        tools = _bool(params, "tools", True)
        if path.startswith("/api/thread/"):
            # structured render blocks (the viewer's reader)
            return _ok(api.read_thread_structured(tid, include_thinking=thinking, include_tools=tools))
        # flat transcript string (CLI-shaped). 'full' shows
        # tool calls (with thinking), 'chat' is the readable assistant text only.
        transcript = api.read_thread(tid, mode="full" if tools else "chat")
        return _ok({"thread_id": tid, "transcript": transcript})

    if path == "/api/threads":
        return _ok(
            _list_threads(
                limit=_int(params, "limit", 100),
                page=_int(params, "page", 1, hi=1_000_000),
                q=_first(params, "q"),
                types=_csv(params, "types"),
            )
        )

    if path == "/api/thread-types":
        return _ok({"types": _list_thread_types()})

    # ---- the manual: the same pages `thread-archive docs` prints ----
    # Markdown source, rendered in the browser by the renderer the transcripts
    # already use. Served from the resolver rather than the static bundle, so a
    # clone's edit to docs/public/ is live on the next request with no rebuild.
    if path == "/api/docs":
        return _ok({"pages": [
            {"slug": p.slug, "title": p.title, "summary": p.summary} for p in _docs.pages()
        ]})
    if path.startswith("/api/docs/"):
        doc = _docs.find(unquote(path[len("/api/docs/"):]))
        if doc is None:
            return _text(404, "no such manual page")
        return _ok({"slug": doc.slug, "title": doc.title, "markdown": doc.read()})

    # unmatched API path — don't fall through to the SPA shell
    if path.startswith("/api/"):
        return _text(404, "not found")

    # ---- a real static asset from the built bundle (assets/*.js|css, favicon…) ----
    rel = path.lstrip("/")
    if rel:
        candidate = (STATIC_DIR / rel).resolve()
        # Containment spelled as an equality-or-ancestor test, not
        # `is_relative_to`: the two decide identically, but CodeQL's
        # path-injection query models this form as a sanitizer and the other
        # not at all, so the terser spelling reds the scan on a request path
        # that is already checked.
        if (candidate == STATIC_DIR or STATIC_DIR in candidate.parents) and candidate.is_file():
            # Vite emits content-hashed filenames under assets/ — a changed file
            # gets a new URL, so the browser may cache these forever.
            cache = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if rel.startswith("assets/") else None
            )
            return _serve_file(candidate, headers=cache)

    # ---- SPA fallback: every other path renders the app shell (client routing) ----
    return _serve_shell()


# ---------------------------------------------------------------------------
# the HTTP adapter (the only networked part)
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def _send_security_headers(self) -> None:
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)

    def _refuse(self, status: int, message: str) -> None:
        """Answer a request that never reached the router. Closes the connection:
        a refused write has an unread body still in the socket."""
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        # DNS-rebinding defense (see _host_allowed). The deliberate non-loopback
        # opt-in also disables the check: an exposed server is reached by a
        # non-loopback name by definition.
        if _host_allowed(self.headers.get("Host")) or os.environ.get(_NONLOCAL_OPTIN) == "1":
            return True
        self._refuse(403, "forbidden: bad Host header")
        return False

    def do_GET(self):  # noqa: N802 — stdlib dispatch name
        if not self._host_ok():
            return
        self._dispatch("GET", None)

    def do_POST(self):  # noqa: N802 — stdlib dispatch name
        if not self._host_ok():
            return
        if not _write_allowed(self.headers):
            self._refuse(403, "forbidden: cross-site write")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._refuse(411, "length required")
            return
        if length < 0:
            self._refuse(400, "bad content length")
            return
        self._dispatch("POST", RequestBody(self.rfile, length))

    def _dispatch(self, method: str, body: Optional[RequestBody]) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        # Timed around the router, not inside it: `route` is the socket-free core
        # the tests drive directly, and it should stay a pure function of its
        # arguments. This is also the boundary where a failed request is still a
        # request — the 500 path below is recorded with the time it burned, since a
        # slow failure is the most interesting latency there is.
        _started = time.monotonic()
        # One probe around the whole dispatch rather than a search-path special
        # case: endpoints other than /api/search reach the engine too, and a probe
        # nobody fills costs a contextvar set and reports nothing.
        probe = None
        span = None
        try:
            with _metrics.serving(), _contention.in_flight() as span, _probe.install() as probe:
                status, ctype, out, headers = route(method, parsed.path, params, body)
        except Exception:  # noqa: BLE001 — isolate per request; never kill the loop
            # Detail stays server-side: exception text can carry paths/SQL/query
            # internals, and the body goes to whoever reached the port.
            log.exception("web request failed: %s", parsed.path)
            status, ctype, headers = 500, "application/json", {}
            out = json.dumps({"error": "internal error"}).encode()
        if body is not None and body.remaining:
            # A body the router declined to read is still sitting in the socket,
            # where the next keep-alive request would parse it as its own request
            # line. Close rather than drain: the unread remainder is a whole
            # export. send_header('Connection', 'close') sets close_connection.
            headers = {**headers, "Connection": "close"}
        _metrics.record_request(
            parsed.path,
            method=method,
            status=status,
            duration_ms=(time.monotonic() - _started) * 1000.0,
            size=len(out),
            probe=probe,
            # What the search asked for, read the same way the route read it. A
            # viewer page is 40 rows deep by default, so without this every
            # browse of the result set looks like a question that took a second.
            workload=(
                dict(zip(("limit", "page"), _search_shape(params)))
                if parsed.path == "/api/search" else None
            ),
            # Sampled after the work, at the surface that served it — the same
            # place the MCP tools sample theirs. The viewer shares a process with
            # the watcher, so a background matrix or graph refresh here is
            # competing with this very request. The request also *enters* the
            # in-flight span rather than only reading it: the viewer serves its
            # pages concurrently, and a surface that samples without entering makes
            # its own load invisible to every peak, its own included.
            context=({**_contention.sample(), **_contention.peak_inflight(span)}
                     if _metrics.enabled() else None),
        )
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


class _ArchiveHTTPServer(ThreadingHTTPServer):
    """HTTP server that owns the background work it starts.

    The stdlib server tracks request threads, but the startup prewarms are
    ours. Joining them on a clean close prevents a stopped cohost from leaving
    database work running against an archive the caller has already torn down.
    """

    def __init__(self, server_address, handler) -> None:
        super().__init__(server_address, handler)
        self._prewarm_threads: list[threading.Thread] = []

    def start_prewarm(self, target, *, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._prewarm_threads.append(thread)
        thread.start()

    def server_close(self) -> None:
        super().server_close()
        for thread in self._prewarm_threads:
            thread.join(timeout=5)


def serve_in_thread(*, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    """Start the viewer on a background daemon thread and return the server.

    For ``thread-archive watch --web``: the always-on watcher process cohosts the read
    surface so there's a persistent URL without a second daemon. Assumes the caller
    already opened the archive (the watcher does). The thread is a daemon, so it dies
    with the process; for a clean stop the caller calls ``shutdown()`` then
    ``server_close()`` — in that order, since a bare close doesn't wake the
    serve_forever poller on Linux and the port keeps accepting meanwhile.

    Refuses a non-loopback ``host`` unless ``THREAD_ARCHIVE_WEB_NONLOCAL=1``: the
    viewer is unauthenticated full read of the archive, so exposing it beyond the
    machine must be a deliberate act, not a typo'd ``--web-host``."""
    if host not in _LOOPBACK_HOSTS and os.environ.get(_NONLOCAL_OPTIN) != "1":
        raise ValueError(
            f"refusing non-loopback bind {host!r}: the viewer has no auth and serves "
            f"the full archive. Set {_NONLOCAL_OPTIN}=1 to expose it deliberately."
        )
    httpd = _ArchiveHTTPServer((host, port), _Handler)
    threading.Thread(target=httpd.serve_forever, name="archive-web", daemon=True).start()
    # Prewarm the status survey so even the first /api/status a fresh process
    # serves comes from cache instead of paying the multi-second count.
    httpd.start_prewarm(_prewarm_status, name="archive-web-status-warm")
    # Prewarm the stats rollup the same way: build/refresh the token-cost cache in the
    # background so the first /api/stats serves an already-warm table. Only the very
    # first build (or the one after a reindex) is slow; a restart folds just the delta.
    httpd.start_prewarm(_prewarm_stats, name="archive-web-stats-warm")
    return httpd


def _prewarm_status() -> None:
    """Best-effort background status survey at server start; see _prewarm_stats.

    Every prewarm is fail-soft for the same reason: an archive that can't be
    opened at start (a half-restored home, a reindex mid-swap) must cost the
    first real request its survey, not spill an unhandled traceback into the
    cohosting watcher's log.
    """
    try:
        _status()
    except Exception:  # noqa: BLE001 — warm-up must never crash the server thread
        log.debug("status prewarm failed", exc_info=True)


def _prewarm_stats() -> None:
    """Best-effort background build of the stats rollup at server start. A failure
    (archive momentarily unavailable) just means the first real request pays the cost."""
    try:
        api.stats()
    except Exception:  # noqa: BLE001 — warm-up must never crash the server thread
        log.debug("stats prewarm failed", exc_info=True)
