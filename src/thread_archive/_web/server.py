"""The web viewer's server: a socket-free router + a thin stdlib HTTP adapter.

The whole read surface the UI needs already exists as the plain Python library
(:mod:`thread_archive._api`): ``search`` / ``read_thread`` / ``status``. This is a
skin over it — no ranking/fusion logic is duplicated here. :func:`route` is a pure
``(method, path, params) -> (status, content_type, body, headers)`` function so
tests drive it without opening a socket. :func:`serve_in_thread` runs it in a
background daemon thread so the always-on ``thread_archive watch --web`` process can
cohost the viewer (one process, one engine) — that's how the read surface gets a
persistent URL with no extra daemon or standalone web verb.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .. import _api as api

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

    Hits already carrying ``thread_source`` (the grouped/browse shapes annotate it)
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
    verdict, note = _search_quality(hits[0]["term_hits"], len(terms), bool(hits[0].get("_did_rerank")))
    return {"verdict": verdict, "note": note, "n_terms": len(terms)}


def _resolve_dup_threads(hits: "list[EventHit]") -> None:
    """Resolve each hit's ``_dup_thread_ids`` fold into a ``dup_threads`` list of
    ``{thread_id, title}`` — the other conversations whose matching text is
    identical to this hit's (a forked session, a fleet of agents carrying one
    prompt). Ids alone would render as bare numbers; the reader needs a name to
    decide whether the twin is worth opening. One query for the whole page."""
    from sqlalchemy import select

    from .._store import Thread, get_session

    wanted = {tid for h in hits for tid in h.get("_dup_thread_ids") or ()}
    if not wanted:
        return
    with get_session() as s:
        titles: dict[str, Optional[str]] = {
            r.id: r.title
            for r in s.execute(select(Thread.id, Thread.title).where(Thread.id.in_(wanted))).all()
        }
    for h in hits:
        ids = h.get("_dup_thread_ids")
        if ids:
            h["dup_threads"] = [{"thread_id": t, "title": titles.get(t)} for t in ids]


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
    return _survey("status", api.status, _STATUS_TTL)


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


def _list_threads(*, limit: int, q: Optional[str], types: Optional[list[str]] = None) -> list[dict]:
    """Recent threads by last *activity* — the newest event's ``occurred_at``,
    falling back to the row's ``updated_at`` for event-less threads. The raw
    ``updated_at`` column can't mean "recently active" here: it is the truth
    checkpoint's dirty-flag, bumped by any metadata write (a stored-summary
    write would re-surface a years-old thread) and untouched by event ingest. With
    ``types`` given, exactly those ``thread_type`` values are listed; without
    it, topics and system threads (subagent runs) are hidden — the sidebar's
    default. Archived threads never list; ``q`` filters on title/name
    substring."""
    from sqlalchemy import DateTime, func, select

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
    stmt = (
        select(Thread.id, Thread.title, Thread.name, Thread.source,
               Thread.thread_type, last_active_at)
        .where(Thread.archived.is_(False))
    )
    if types:
        stmt = stmt.where(Thread.thread_type.in_(types))
    else:
        stmt = stmt.where(Thread.thread_type.not_in(_DEFAULT_HIDDEN_TYPES))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Thread.title.ilike(like) | Thread.name.ilike(like))
    stmt = stmt.order_by(last_active_at.desc()).limit(limit)
    with get_session() as s:
        rows = s.execute(stmt).all()
    return [
        {
            "id": r.id,
            "title": r.title or r.name,
            "source": r.source,
            "thread_type": r.thread_type,
            # the row's date in list consumers: last activity, not the raw column
            "updated_at": r.last_active_at.isoformat() if r.last_active_at else None,
        }
        for r in rows
    ]


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


def resolve_archive_link(link_id: str, source: Optional[str] = None) -> Optional[str]:
    """Resolve a pasted/sprayed ref to its archive (ULID) thread id.

    This is the archive-link lookup an editor needs ("I have a session uuid, open the
    conversation"), and the same lookup that lets a bare ref be pasted straight into
    ``/archive/<ref>``. Without ``source`` it is exactly
    :func:`thread_archive._retrieval.read.resolve_thread_ref` — the three ref
    shapes every surface accepts: a ULID (the primary key), an all-digit legacy
    integer alias (``Thread.legacy_id``, a permanent alias — pasted integer links
    keep resolving), or a provider session id via the shared
    :func:`thread_archive._store.resolve.resolve_session_source_id` union
    (``Thread.source_id`` plus the ``ImportState`` watermarks — a compaction
    continuation's uuid lives only there), the same union the MCP reader uses, so
    every surface answers alike. ``source`` narrows to one provider (an editor
    knows its own and sprays session-id candidates only — see
    ``/api/archive-link``), so with it resolution is session-id-only: a sprayed
    junk candidate must never land on an unrelated thread through the ULID or
    legacy-alias branches. Owned here: the watcher cohosts the persistent server,
    so the archive serves its own editor links."""
    from .._retrieval.read import resolve_thread_ref
    from .._store import get_session, resolve_session_source_id

    api.open_archive()
    with get_session() as s:
        if source:
            return resolve_session_source_id(s, link_id, source=source.replace("_", "-"))
        return resolve_thread_ref(s, link_id)


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
        return _ok(_status())

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
        import re as _re

        m = _re.match(r"^([0-9a-f]{64})(\.[A-Za-z0-9]{1,8})?$", path[len("/api/blob/"):])
        if not m:
            return _text(404, "bad blob name")
        api.open_archive()
        from .._truth.blobs import blob_file, media_type_for_path

        bp = blob_file(m.group(1))
        if bp is None:
            return _text(404, "no such blob")
        try:
            body = bp.read_bytes()
        except OSError:
            return _text(404, "no such blob")
        return 200, media_type_for_path(bp), body, {
            "Cache-Control": "public, max-age=31536000, immutable",
        }

    if path == "/api/search":
        q = (_first(params, "q") or "").strip()
        if not q:
            # Empty query = browse, the same contract as MCP thread_search: one
            # row per thread by last activity, honoring the structural filters.
            # Rows carry thread_source / n_events; content options don't apply.
            hits = api.search(
                "",
                limit=_int(params, "limit", 30),
                source=_csv(params, "source"),
                since=_first(params, "since"),
                until=_first(params, "until"),
                agents=_first(params, "agents"),
            )
            return _ok({"query": "", "browse": True, "hits": hits,
                        "quality": None, "subjects": _subjects_payload(hits)})
        hits = api.search(
            q,
            limit=_int(params, "limit", 30),
            source=_csv(params, "source"),
            content_types=_csv(params, "content_types"),
            since=_first(params, "since"),
            until=_first(params, "until"),
            agents=_first(params, "agents"),
            # Fold cross-thread duplicate content only: a query matching a common
            # opener ("hey grok") otherwise spends the whole page on N threads
            # showing the same line. A thread's *own* several matches still each
            # get a row — the reader is browsing, not spending result slots.
            group="dup",
            context_lines=0,  # the viewer builds its own ±1 snippet from full_content
        )
        quality = _quality_signal(hits, q)
        _resolve_dup_threads(hits)
        return _ok({"query": q, "browse": False, "hits": _shape_search_hits(hits, q),
                    "quality": quality, "subjects": _subjects_payload(hits)})

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
        # flat transcript string (CLI-shaped; kept for back-compat). 'full' shows
        # tool calls (with thinking), 'chat' is the readable assistant text only.
        transcript = api.read_thread(tid, mode="full" if tools else "chat")
        return _ok({"thread_id": tid, "transcript": transcript})

    if path == "/api/threads":
        return _ok(
            {
                "threads": _list_threads(
                    limit=_int(params, "limit", 100),
                    q=_first(params, "q"),
                    types=_csv(params, "types"),
                )
            }
        )

    if path == "/api/thread-types":
        return _ok({"types": _list_thread_types()})

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
    def _send_security_headers(self) -> None:
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)

    def do_GET(self):  # noqa: N802 — stdlib dispatch name
        # DNS-rebinding defense (see _host_allowed). The deliberate non-loopback
        # opt-in also disables the check: an exposed server is reached by a
        # non-loopback name by definition.
        if not _host_allowed(self.headers.get("Host")) and os.environ.get(_NONLOCAL_OPTIN) != "1":
            body = b"forbidden: bad Host header"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
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
        self._send_security_headers()
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the foreground console quiet
        pass


class _ArchiveHTTPServer(ThreadingHTTPServer):
    """HTTP server that owns the background work it starts.

    The stdlib server tracks request threads, but the three startup prewarms are
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

    For ``thread_archive watch --web``: the always-on watcher process cohosts the read
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
