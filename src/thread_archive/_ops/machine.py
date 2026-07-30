"""What the machine itself was doing — the denominator under every duration here.

A wall-clock number is a measurement of two things: the work, and the box the work
ran on. This archive's daemons, its MCP servers, its viewer, its bench and the
operator's own sessions all share one machine, and the spread between a quiet box
and a busy one is not subtle — the same model load measures seconds on the first
and over a minute on the second. Without these, a regression in the code and a
loaded afternoon are the same row.

Two readings, both cheap enough to take on any path that records a duration:

``load1``
    The one-minute load average. Raw rather than divided by the core count: it is
    the number the operator already reads off ``uptime``, and cores are a property
    of the machine rather than of the thing being measured.

``rss_mb``
    Peak resident memory of this process, a high-water mark since it started. It
    never falls, so it does not say what is held *now* — it says whether this
    process has ever been big enough to hurt the machine it shares, which is the
    question worth asking of a process that reads a vector pack whole into memory
    and holds two models resident.

Both return ``None`` rather than raising where the platform has no such concept,
because every caller here is recording telemetry and none of them may fail for it.
Lives in ``_ops`` because three unrelated callers need it — the retrieval
contention sample, the watcher's idle rollup, the lab's latency gate — and the
alternative is what it replaced: a copy each, quietly rounding differently, so two
reports over one machine disagreed about what its load was.
"""

from __future__ import annotations

import os
import sys
from typing import Optional


def load1() -> Optional[float]:
    """The machine's one-minute load average, or None where the OS has none."""
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):  # no such concept on this platform
        return None


#: Divisor turning ``ru_maxrss`` into megabytes. The field's unit is the one thing
#: about it that is not portable — bytes on the BSDs (macOS among them), kilobytes
#: on Linux — and getting it wrong is a thousand-fold error in a number nobody
#: would double-check, so it is resolved once here against the platform rather than
#: at each call site.
_MAXRSS_PER_MB = (1024.0 * 1024.0) if sys.platform == "darwin" else 1024.0


def rss_mb() -> Optional[float]:
    """Peak resident memory of this process in MB, or None where unavailable."""
    try:
        import resource  # Unix-only; imported here so the module loads without it

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _MAXRSS_PER_MB, 1)
    except Exception:  # noqa: BLE001 — advisory; never break the caller
        return None
