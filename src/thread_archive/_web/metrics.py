"""The web viewer's request ledger: ``<home>/web-requests.jsonl``.

The viewer is a real read surface over the same index the MCP tools use — it runs
searches, hydrates threads, and computes surveys — and it is the one surface whose
latency nobody has ever recorded. A page that takes twenty seconds and a page that
takes two hundred milliseconds look the same from the outside: both eventually
render.

Deliberately its own file rather than rows in ``retrieval-usage.jsonl``. That
ledger is the observed ground truth future retrieval evals are built from — an
agent's queries and the reads that followed them — and browsing is a different
behavior with different intent. Mixing a human clicking around into the set of
"queries an agent asked" would quietly bias every eval mined from it.

Endpoint and outcome only: path, status, response size, wall time. Query strings
are left out — the path alone answers which endpoint is slow, and the ledger has
no reason to accumulate whatever anyone typed into the search box.

A request that ran a search carries that search's stage breakdown too. The viewer
drives the same engine the MCP tools do, so ``/api/search`` has the same tail they
have — and an endpoint total is no more diagnostic here than an arm total is
there. The rows stay cheap for everything else: the breakdown rides along only
when the probe says retrieval actually happened.

``concurrent`` is the viewer's own contention signal, distinct from the retrieval
one. The server is a :class:`~http.server.ThreadingHTTPServer` and the SPA opens
several requests per page, so a slow endpoint is routinely slow *while* others are
being served from the same process — and one multi-second search is enough to make
every request beside it look degraded.

Advisory and fail-soft throughout, like every other telemetry writer here: a
metrics write must never break the request it describes. Recorded only on an
install being developed on (:mod:`.._ops.telemetry`) — someone reading their own
conversations is not served by a row per page they opened;
``THREAD_ARCHIVE_WEB_METRICS`` overrides either way.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator, Optional

from .._config import resolve_paths
from .._ops import ledger as _ledger
from .._ops import telemetry as _telemetry

if TYPE_CHECKING:  # the probe is duck-typed at runtime — no import cost per request
    from .._retrieval._probe import SearchProbe

logger = logging.getLogger(__name__)

LEDGER_FILE = "web-requests.jsonl"

_CONCURRENT_LOCK = threading.Lock()
_concurrent = 0


@contextmanager
def serving() -> Iterator[None]:
    """Count this request as being served for its duration.

    Wraps dispatch at the handler, so a sample taken inside includes the caller
    itself — a request served alone reports ``1``, which is why the field is only
    recorded above that.
    """
    global _concurrent
    with _CONCURRENT_LOCK:
        _concurrent += 1
    try:
        yield
    finally:
        with _CONCURRENT_LOCK:
            _concurrent -= 1


def max_bytes() -> int:
    """Size at which the ledger rotates to a new segment (8 MB by default).

    Smaller than the retrieval ledger's cap because these rows are small and a
    browsing session makes a great many of them — it bounds what one read walks,
    not how much history is kept. Rotation retains every segment
    (:mod:`.._ops.ledger`).
    """
    return _ledger.env_max_bytes("THREAD_ARCHIVE_WEB_METRICS_MAX_BYTES", 8 * 1024 * 1024)


def enabled(home: Optional[Any] = None) -> bool:
    """Whether this install records served requests at all.

    Off unless the install is being developed on (:mod:`.._ops.telemetry`);
    ``THREAD_ARCHIVE_WEB_METRICS`` overrides in either direction. Public so the
    handler can skip building a record — the contention sample above all — that
    nothing is going to write, and so a reader of this ledger can tell an empty
    window from an install that writes nothing.
    """
    return _telemetry.recording("THREAD_ARCHIVE_WEB_METRICS", home)


def record_request(
    path: str,
    *,
    status: int,
    duration_ms: float,
    method: str = "GET",
    size: Optional[int] = None,
    probe: Optional["SearchProbe"] = None,
    workload: Optional[dict[str, Any]] = None,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Append one served request. Never raises.

    ``probe`` is folded in flat — same field names the retrieval ledger uses, so
    one analysis reads both — and only when it says a search ran. ``context`` is a
    :func:`thread_archive._retrieval._contention.sample`, itself already empty on a
    quiet machine, so a request with nothing competing writes no context at all.

    ``workload`` is the *shape* of the ask — ``limit`` and ``page`` — folded in flat
    on the same field names for the same reason. It is not the query-string
    exception it looks like: how many rows were asked for and how far into the set
    is the single largest thing separating one search's cost from another's, and a
    reader that cannot see it must either treat a 40-row page-9 walk as a question
    or treat every question as a walk. The query *text* stays out.

    ``method`` is recorded only when it isn't a read: an upload's cost is the
    uploader's connection, not this archive's, and a row that looked like a GET of
    the same path would drag that time into the read-latency distribution.
    """
    if not enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    if method != "GET":
        record["method"] = method
    if size is not None:
        record["size"] = size
    if probe is not None and probe.ran:
        record.update(probe.as_record())
    if workload:
        record.update(workload)
    if _concurrent > 1:
        record["concurrent"] = _concurrent
    if context:
        record["context"] = context
    try:
        _ledger.append(resolve_paths().home / LEDGER_FILE, record, max_bytes=max_bytes())
    except Exception:  # noqa: BLE001 — telemetry must never break the request
        logger.warning("could not record web request metrics", exc_info=True)
