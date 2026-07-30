"""What the machine itself was doing — the denominator under every duration here.

A wall-clock number is a measurement of two things: the work, and the box the work
ran on. This archive's daemons, its MCP servers, its viewer, its bench and the
operator's own sessions all share one machine, and the spread between a quiet box
and a busy one is not subtle — the same model load measures seconds on the first
and over a minute on the second. Without these, a regression in the code and a
loaded afternoon are the same row.

Three readings, all cheap enough to take on any path that records a duration:

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

``rss_now_mb``
    Resident memory *at this instant*, which is the one that says whether the
    process still has what it loaded. The pair is the point: a warmed search server
    that peaked at 3 GB and is resident at 45 MB has been evicted to swap while
    every other warmth signal still reads warm — the models are constructed, the
    process is hours old, and the next query pays to fault the vector matrix back
    in. Peak alone cannot see that, because a high-water mark never falls.

All three return ``None`` rather than raising where the platform has no such
concept, because every caller here is recording telemetry and none may fail for it.
Lives in ``_ops`` because three unrelated callers need it — the retrieval
contention sample, the watcher's idle rollup, the lab's latency gate — and the
alternative is what it replaced: a copy each, quietly rounding differently, so two
reports over one machine disagreed about what its load was.
"""

from __future__ import annotations

import ctypes
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


class _ProcTaskInfo(ctypes.Structure):
    """Darwin's ``struct proc_taskinfo`` (``sys/proc_info.h``).

    Declared whole rather than truncated at the field we read: ``proc_pidinfo``
    validates the buffer size it is handed against the size the kernel expects and
    returns 0 for a short one, so the tail fields have to be here even though only
    ``pti_resident_size`` is used."""

    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64), ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64), ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64), ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32), ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32), ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32), ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32), ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32), ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32), ("pti_priority", ctypes.c_int32),
    ]


#: ``PROC_PIDTASKINFO`` — the ``proc_pidinfo`` flavour returning :class:`_ProcTaskInfo`.
_PROC_PIDTASKINFO = 4


def rss_now_mb() -> Optional[float]:
    """Resident memory of this process in MB *right now*, or None where unavailable.

    Read straight from the kernel on both platforms rather than through a
    dependency: this runs on every recorded search, so it has to cost a syscall
    rather than a subprocess, and the archive carries no ``psutil``."""
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
            info = _ProcTaskInfo()
            got = libc.proc_pidinfo(os.getpid(), _PROC_PIDTASKINFO, ctypes.c_uint64(0),
                                    ctypes.byref(info), ctypes.sizeof(info))
            if got != ctypes.sizeof(info):
                return None
            return round(info.pti_resident_size / (1024.0 * 1024.0), 1)
        # Linux: statm's second field is resident pages.
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0), 1)
    except Exception:  # noqa: BLE001 — advisory; never break the caller
        return None
