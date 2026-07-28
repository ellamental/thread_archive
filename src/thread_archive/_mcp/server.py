"""Library-native MCP server.

Exposes ``thread_search`` + ``thread_read`` as MCP tools. The tools themselves —
signatures, docstrings (which are the tool schema and description), and every
line of retrieval behaviour — live in :mod:`thread_archive._tools`, shared with
the ``thread-archive search`` / ``thread-archive read`` CLI verbs so the two
front doors cannot drift. This module is the MCP half: the transport, the bind
plan, and the cohosted catch-up ingest. No web framework, no HTTP, no route
layer — it dispatches straight to the library functions in-process.

The tools and, by default, the server process are read-only. An operator may
explicitly set ``THREAD_ARCHIVE_MCP_INGEST=1`` to cohost lazy catch-up ingest
(see :class:`IngestThrottle` and :mod:`.._watcher.lazy`): a throttled background
pass at startup and around tool calls keeps the archive current with no daemon
installed, and degrades to a no-op flock probe when the always-on watcher owns
ingest. Setup-generated stdio client entries carry that explicit opt-in.

The archive home comes from ``$THREAD_ARCHIVE_HOME`` (set by the MCP client
config), else ``~/.thread/archive``. Run per-client over stdio (the default)::

    python -m thread_archive._mcp.server

or as one shared always-on server over streamable-HTTP, so many agents share a
single resident model instead of one 3 GB process each::

    archive-mcp --http --host 127.0.0.1 --port 8788

``import mcp`` below is the external MCP SDK (top-level absolute import); this
package is ``thread_archive._mcp`` and never shadows it.
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP

from .. import _tools
from .._config import ENV_MCP_INGEST
from .._retrieval import start_warm_models

logger = logging.getLogger(__name__)

mcp = FastMCP("thread-archive")

# ── lazy catch-up ingest ──────────────────────────────────────────────────────
# The opted-in zero-daemon freshness path: the server runs a background catch-up
# pass at startup and (throttled) around tool calls when its client config sets
# THREAD_ARCHIVE_MCP_INGEST=1.
# Cross-process safety lives in the pass itself (see _watcher.lazy): the
# ingest-owner flock makes every pass a no-op probe while the always-on watcher
# daemon — or another server's pass — owns ingest. The behavior is off unless
# THREAD_ARCHIVE_MCP_INGEST explicitly opts in.
_INGEST_MIN_INTERVAL = 300.0  # seconds between catch-up attempts in this process


def ingest_enabled() -> bool:
    """Whether cohosted catch-up ingest has been explicitly enabled.

    Read per call so a value set after import is honored. Missing, malformed,
    and negative values are all read-only; only an affirmative value opts in.
    """
    return os.environ.get(ENV_MCP_INGEST, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


class IngestThrottle:
    """The gate on one process's cohosted catch-up ingest: at most one pass in
    flight, at most one attempt per ``_INGEST_MIN_INTERVAL``, and none at all
    while the kill-switch is off.

    The server's own gate is the module-level :data:`INGEST`; the state is held
    on the instance rather than in module globals, so a caller that wants an
    independent gate constructs its own.
    """

    def __init__(self) -> None:
        self.last = 0.0  # monotonic time of the last attempt (0 = never)
        self.running = threading.Lock()  # one in-flight catch-up per process

    def claim(self) -> bool:
        """Take the in-flight slot for a catch-up pass, or refuse it. A true
        return means the caller owns the slot and must :meth:`release` it."""
        if not ingest_enabled():
            return False
        now = time.monotonic()
        if self.last and now - self.last < _INGEST_MIN_INTERVAL:
            return False
        if not self.running.acquire(blocking=False):
            return False  # a catch-up is already in flight in this process
        self.last = now
        return True

    def release(self) -> None:
        """Hand the in-flight slot back, so the next attempt past the interval
        can claim it."""
        self.running.release()

    def run_pass(self) -> None:
        """Run one catch-up pass on a claimed slot, releasing it whatever
        happens. Never raises — ingest is advisory to retrieval."""
        try:
            from .._watcher import catch_up_once

            catch_up_once()
        except Exception:  # noqa: BLE001 — advisory; retrieval must not care
            logger.exception("lazy catch-up ingest failed")
        finally:
            self.release()

    def maybe_catch_up(self) -> None:
        """Kick a background catch-up pass, throttled. Never blocks the caller and
        never raises — retrieval must work identically with ingest disabled, owned
        by another process, or broken."""
        if not self.claim():
            return
        threading.Thread(
            target=self.run_pass, name="archive-lazy-ingest", daemon=True
        ).start()


INGEST = IngestThrottle()


def _served(fn: Callable[..., str]) -> Callable[..., str]:
    """Wrap a :mod:`thread_archive._tools` function as this server serves it:
    a throttled catch-up ingest kick, then the tool itself.

    The kick is the server's own — a one-shot CLI process would be killed with
    its background pass half-run, so the shared implementation stays free of it.
    ``functools.wraps`` carries the implementation's signature, annotations, and
    docstring across, and those are exactly what FastMCP builds the tool's schema
    and description from: the contract an agent reads is the one written beside
    the code that answers it.
    """

    @functools.wraps(fn)
    def tool(*args: object, **kwargs: object) -> str:
        INGEST.maybe_catch_up()
        return fn(*args, **kwargs)

    return tool


thread_search = mcp.tool()(_served(_tools.thread_search))
thread_read = mcp.tool()(_served(_tools.thread_read))


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# The transports FastMCP.run() serves.
Transport = Literal["stdio", "sse", "streamable-http"]


@dataclass(frozen=True)
class ServePlan:
    """What one ``archive-mcp`` invocation decided to do.

    ``transport`` is the argument :meth:`FastMCP.run` takes, or ``None`` for the
    stdio default. ``host``/``port`` are the parsed bind and apply to the HTTP
    transport only.
    """

    transport: Optional[Transport] = None
    warm: bool = False
    host: str = "127.0.0.1"
    port: int = 8788


def _parser() -> argparse.ArgumentParser:
    # stdio (default) is one server per client — every connecting agent spawns its own
    # process, and this one loads the embedding stack. --http instead
    # serves streamable-HTTP on one loopback port so every agent shares a single always-on
    # server (one model resident, not one per client); the LaunchAgent runs that mode and the
    # MCP client config points at the URL. Stdio stays the default so `claude mcp add …
    # archive-mcp` and `python -m thread_archive._mcp.server` keep working with no daemon.
    parser = argparse.ArgumentParser(prog="archive-mcp", description=__doc__)
    parser.add_argument(
        "--http", action="store_true",
        help="serve streamable-HTTP (shared, always-on) instead of per-client stdio",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (--http only)")
    parser.add_argument("--port", type=int, default=8788, help="HTTP bind port (--http only)")
    return parser


def plan_serve(argv: Optional[Sequence[str]] = None) -> ServePlan:
    """Read a command line (the process's when ``argv`` is None) into the plan
    :func:`main` executes — transport, bind, and whether to warm the models.

    Exits through the parser's usage error when the requested bind is not
    loopback and ``THREAD_ARCHIVE_MCP_NONLOCAL=1`` is unset — the same guard
    the web viewer applies, for the same reason: this server is unauthenticated
    full read of the archive, so exposing it beyond the machine must be a
    deliberate act, not a typo'd ``--host``.
    """
    parser = _parser()
    args = parser.parse_args(argv)

    # Only the shared HTTP server warms the model stack: it's the one hot copy every client
    # shares, so its ~3 GB of models pays off. A per-client stdio server stays lean (models
    # unloaded, load is lazy on first use) — a client that only reads, never searches, or
    # whose config points at stdio rather than the shared server, shouldn't each hold 3 GB. A
    # standalone stdio deployment with no shared server opts back in with
    # THREAD_ARCHIVE_MCP_WARM=1.
    warm = args.http or os.environ.get("THREAD_ARCHIVE_MCP_WARM", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not args.http:
        return ServePlan(transport=None, warm=warm, host=args.host, port=args.port)
    if args.host not in LOOPBACK_HOSTS and os.environ.get(
        "THREAD_ARCHIVE_MCP_NONLOCAL"
    ) != "1":
        parser.error(
            f"refusing non-loopback bind {args.host!r}: archive-mcp has no auth and "
            f"serves the full archive. Set THREAD_ARCHIVE_MCP_NONLOCAL=1 to expose "
            f"it deliberately."
        )
    return ServePlan(transport="streamable-http", warm=warm, host=args.host, port=args.port)


def apply_settings(plan: ServePlan) -> None:
    """Point the server at the plan's bind, ready for its HTTP transport.

    Stateless + JSON responses: each request is self-contained (no held-open
    per-client SSE stream or server-side session to track across many agents),
    and the read-only tools have nothing to push back. ``run()`` reads these off
    ``mcp.settings`` when it starts, so they are set before it is called.
    """
    mcp.settings.host = plan.host
    mcp.settings.port = plan.port
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Serve, per ``argv`` (the process's command line by default). Blocks in the
    transport's run loop until the client disconnects or the process is stopped."""
    plan = plan_serve(argv)
    # Warm the embedding model at startup. The cold load is tens of
    # seconds; when it lands inside the first conceptual search it can exceed the client's
    # MCP request timeout (commonly 60s), which surfaces to the model as a failed tool call.
    if plan.warm:
        start_warm_models()
    # Startup catch-up: whatever landed in the local stores since the last
    # ingest (by any process) is searchable by the time the first query
    # arrives — or shortly after; the pass is additive, never blocking.
    INGEST.maybe_catch_up()
    if plan.transport is None:
        mcp.run()
        return
    apply_settings(plan)
    mcp.run(plan.transport)


if __name__ == "__main__":
    main()
