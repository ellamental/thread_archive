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
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .. import _api as api

# The hierarchy edge vocabulary (owned by the gardener diagnostics module): a
# `part-of` link reads child→parent, a `contains` link parent→child. The topic
# tree is *derived* from these links — topics are the only nodes (one id space,
# no ontology tables), so the hierarchy is exactly as curated as the links are.
from .._knowledge.garden import HIERARCHY_DOWN as _HIERARCHY_DOWN
from .._knowledge.garden import HIERARCHY_UP as _HIERARCHY_UP

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


# /api/status behind a small TTL cache. The survey counts every table — seconds
# on a large archive — while the status bar asks on every page load, so requests
# serve the cached survey and a stale one refreshes in the background; only the
# first request a process ever sees pays the full cost (and serve_in_thread
# prewarms, so in the cohosted watcher not even that). Keyed by home: one
# process normally serves one archive, but tests point the engine at a fresh
# home per test and must not read a stale survey of the previous one.
_STATUS_TTL = 60.0
_status_lock = threading.Lock()
_status_cache: dict[str, tuple[float, dict]] = {}
_status_refreshing: set[str] = set()


def _refresh_status(home: str) -> dict:
    fresh = api.status()
    with _status_lock:
        _status_cache[home] = (time.monotonic(), fresh)
        _status_refreshing.discard(home)
    return fresh


def _status() -> dict:
    home = str(api.open_archive().home)
    with _status_lock:
        cached = _status_cache.get(home)
        if cached is not None:
            age = time.monotonic() - cached[0]
            if age >= _STATUS_TTL and home not in _status_refreshing:
                _status_refreshing.add(home)
                threading.Thread(
                    target=_refresh_status, args=(home,),
                    name="archive-web-status", daemon=True,
                ).start()
            return cached[1]
    return _refresh_status(home)


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
# topics have their own pages, and 'system' threads (Task-tool subagent runs —
# see the claude-code importer) are machinery, not sessions someone opens by
# recency. Both stay reachable through /api/threads?types=….
_DEFAULT_HIDDEN_TYPES = ("topic", "system")


def _list_threads(*, limit: int, q: Optional[str], types: Optional[list[str]] = None) -> list[dict]:
    """Recent threads by last *activity* — the newest event's ``occurred_at``,
    falling back to the row's ``updated_at`` for event-less threads. The raw
    ``updated_at`` column can't mean "recently active" here: it is the truth
    checkpoint's dirty-flag, bumped by any metadata write (a librarian summary
    would re-surface a years-old thread) and untouched by event ingest. With
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


def _list_topics(*, limit: int, q: Optional[str]) -> dict:
    """Live topics with their graph metadata, highest-pagerank first.

    The whole live set is loaded before ranking — topics are curated, so the
    population is inherently small, and ranking by pagerank *then* truncating is
    what makes the limit meaningful (an updated_at-truncated fetch would drop
    central topics). ``q`` filters on title/name substring."""
    from sqlalchemy import func, select

    from .._knowledge import get_status, get_topic_graph_metadata
    from .._store import Thread, TopicMessage, get_session

    api.open_archive()
    stmt = (
        select(Thread.id, Thread.title, Thread.name, Thread.topic_kind,
               Thread.description, Thread.updated_at)
        .where(Thread.thread_type == "topic")
        .where(Thread.archived.is_(False))
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Thread.title.ilike(like) | Thread.name.ilike(like))
    with get_session() as s:
        rows = s.execute(stmt).all()
        evidence_counts = dict(
            s.execute(
                select(TopicMessage.topic_id, func.count())
                .where(TopicMessage.archived_at.is_(None))
                .group_by(TopicMessage.topic_id)
            ).all()
        )
    meta = get_topic_graph_metadata([r.id for r in rows])
    topics = [
        {
            "id": r.id,
            "title": r.title or r.name,
            "topic_kind": r.topic_kind,
            "description": r.description,
            "evidence_count": evidence_counts.get(r.id, 0),
            "link_count": (meta.get(r.id) or {}).get("link_count", 0),
            "community": (meta.get(r.id) or {}).get("community"),
            "pagerank": (meta.get(r.id) or {}).get("pagerank", 0.0),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
    topics.sort(key=lambda t: (-t["pagerank"], t["id"]))
    return {"topics": topics[:limit], "graph": get_status()}




def _topic_tree() -> dict:
    """The derived topic hierarchy: a forest over live topics' part-of/contains
    links. Roots are parents that are no one's child. A child with several
    parents appears under each (it's a DAG rendered as a tree); a cycle is cut
    at the edge that would revisit an ancestor. Conversations and archived
    topics never enter — an edge to one simply doesn't shape the tree."""
    from sqlalchemy import select

    from .._store import Thread, ThreadLink, get_session

    api.open_archive()
    with get_session() as s:
        live = {
            r.id: {"id": r.id, "title": r.title or r.name, "topic_kind": r.topic_kind}
            for r in s.execute(
                select(Thread.id, Thread.title, Thread.name, Thread.topic_kind)
                .where(Thread.thread_type == "topic")
                .where(Thread.archived.is_(False))
            )
        }
        rows = s.execute(
            select(ThreadLink.source_thread_id, ThreadLink.target_thread_id, ThreadLink.link_type)
            .where(ThreadLink.link_type.in_((_HIERARCHY_UP, _HIERARCHY_DOWN)))
        ).all()

    children: dict[int, set[int]] = {}
    for src, tgt, link_type in rows:
        child, parent = (src, tgt) if link_type == _HIERARCHY_UP else (tgt, src)
        if child == parent or child not in live or parent not in live:
            continue
        children.setdefault(parent, set()).add(child)

    child_ids = {c for kids in children.values() for c in kids}

    def build(tid: int, ancestors: frozenset) -> dict:
        node = dict(live[tid])
        kids = sorted(
            children.get(tid, set()) - ancestors,
            key=lambda c: ((live[c]["title"] or "").lower(), c),
        )
        node["children"] = [build(c, ancestors | {tid}) for c in kids]
        return node

    def weight(node: dict) -> int:
        return 1 + sum(weight(c) for c in node["children"])

    forest = [build(r, frozenset()) for r in sorted(set(children) - child_ids)]
    forest.sort(key=lambda n: (-weight(n), (n["title"] or "").lower(), n["id"]))
    return {
        "roots": forest,
        "topics_in_hierarchy": len((child_ids | set(children)) & set(live)),
        "topics_total": len(live),
    }


def _topic_detail(topic_id: int, *, evidence_limit: int) -> Optional[dict]:
    """One topic with its links, citations, and community peers — the reader's
    whole page in one response. ``None`` when the id isn't a topic (conversations
    have their own reader). Archived topics still render (a merged-away topic
    stays referenced from kg history); they just carry no graph metadata."""
    from sqlalchemy import select

    from .._knowledge import get_community_peers, get_topic_graph_meta
    from .._store import Thread, ThreadLink, TopicMessage, get_session

    api.open_archive()
    with get_session() as s:
        t = s.get(Thread, topic_id)
        if t is None or t.thread_type != "topic":
            return None
        links = []
        other = Thread.__table__.alias("other")
        for direction, own_col, other_col in (
            ("out", ThreadLink.source_thread_id, ThreadLink.target_thread_id),
            ("in", ThreadLink.target_thread_id, ThreadLink.source_thread_id),
        ):
            rows = s.execute(
                select(ThreadLink.link_type, ThreadLink.strength, ThreadLink.evidence,
                       other.c.id, other.c.title, other.c.name, other.c.thread_type)
                .join(other, other.c.id == other_col)
                .where(own_col == topic_id)
                .order_by(ThreadLink.strength.desc(), other.c.id)
            ).all()
            links.extend(
                {
                    "direction": direction,
                    "other_id": r.id,
                    "other_title": r.title or r.name,
                    "other_type": r.thread_type,
                    "link_type": r.link_type,
                    "strength": r.strength,
                    "evidence": r.evidence,
                }
                for r in rows
            )
        cited = Thread.__table__.alias("cited")
        evidence = [
            {
                "event_id": r.event_id,
                "thread_id": r.thread_id,
                "thread_title": r.title or r.name,
                "quote": r.quote,
                "created_at": r.created_at,
            }
            for r in s.execute(
                select(TopicMessage.event_id, TopicMessage.thread_id, TopicMessage.quote,
                       TopicMessage.created_at, cited.c.title, cited.c.name)
                .join(cited, cited.c.id == TopicMessage.thread_id)
                .where(TopicMessage.topic_id == topic_id)
                .where(TopicMessage.archived_at.is_(None))
                .order_by(TopicMessage.event_id)
                .limit(evidence_limit)
            ).all()
        ]
        detail = {
            "id": t.id,
            "title": t.title or t.name,
            "topic_kind": t.topic_kind,
            "description": t.description,
            "summary": t.summary,
            "archived": bool(t.archived),
            "created_at": t.inserted_at,
            "updated_at": t.updated_at,
        }
    detail["graph"] = get_topic_graph_meta(topic_id)
    detail["links"] = links
    detail["evidence"] = evidence
    detail["peers"] = get_community_peers(topic_id, limit=8)
    return detail


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
        return _ok(_status())

    if path == "/api/sources":
        return _ok({"sources": _list_sources()})

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

    if path == "/api/topics/tree":
        return _ok(_topic_tree())

    if path == "/api/topics":
        return _ok(_list_topics(limit=_int(params, "limit", 200), q=_first(params, "q")))

    if path.startswith("/api/topic/"):
        try:
            tid = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return _text(404, "bad topic id")
        detail = _topic_detail(tid, evidence_limit=_int(params, "evidence_limit", 200))
        if detail is None:
            return 404, "application/json", json.dumps({"error": f"no topic with id {tid}"}).encode(), {}
        return _ok(detail)

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
    # Prewarm the status survey so even the first /api/status a fresh process
    # serves comes from cache instead of paying the multi-second count.
    threading.Thread(target=_status, name="archive-web-status-warm", daemon=True).start()
    return httpd
