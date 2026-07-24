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
metrics write must never break the request it describes.
``THREAD_ARCHIVE_WEB_METRICS=0`` disables it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator, Optional

from .._config import resolve_paths

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
    """Size at which the ledger rotates to ``.jsonl.1`` (8 MB by default).

    Smaller than the retrieval ledger's cap: these rows are small and a browsing
    session makes a great many of them, and unlike search usage they have no second
    life as eval material — the recent distribution is the whole value.
    """
    return int(os.environ.get("THREAD_ARCHIVE_WEB_METRICS_MAX_BYTES") or 8 * 1024 * 1024)


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_WEB_METRICS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def record_request(
    path: str,
    *,
    status: int,
    duration_ms: float,
    size: Optional[int] = None,
    probe: Optional["SearchProbe"] = None,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Append one served request. Never raises.

    ``probe`` is folded in flat — same field names the retrieval ledger uses, so
    one analysis reads both — and only when it says a search ran. ``context`` is a
    :func:`thread_archive._retrieval._contention.sample`, itself already empty on a
    quiet machine, so a request with nothing competing writes no context at all.
    """
    if not _enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    if size is not None:
        record["size"] = size
    if probe is not None and probe.ran:
        record.update(probe.as_record())
    if _concurrent > 1:
        record["concurrent"] = _concurrent
    if context:
        record["context"] = context
    try:
        p = resolve_paths().home / LEDGER_FILE
        try:
            if p.stat().st_size >= max_bytes():
                p.replace(p.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record web request metrics", exc_info=True)
