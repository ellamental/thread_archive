"""Durable record of ingest faults: ``<home>/ingest-errors.jsonl``.

Every other ingest fault signal is either lossy or perishable. ``watch_errors_last``
in ``health.json`` keeps the last five messages and a running count, and is
*cleared on green* — so a fault that resolves leaves no trace that it ever
happened. ``ingest-runs.jsonl`` keeps a per-pass ``errors`` count with no
messages, and only for passes that did work. The messages themselves land in the
daemon's stderr, which is not telemetry: nothing reads it, nothing bounds it, and
by the time a question is asked about a bad week it is a multi-megabyte file
nobody opens.

That gap is the one an archive can least afford, because an ingest fault means
conversations are not being captured, and the harness prunes its transcripts on
its own schedule regardless. A fault that lasts days and then clears is exactly
the shape of silent, permanent loss — and exactly the shape the perishable
records erase.

**Rows are per signature, not per occurrence.** A broken source does not fail
once; it fails every poll, for as long as it stays broken, which is how an
error log becomes a hundred thousand lines saying one thing. The signature
(:func:`signature`) strips the varying parts of a message — ids, paths, numbers —
so every recurrence folds onto the row that already describes it, and a row is
written only when the count reaches the next power of ten. A fault that fires
sixty-five thousand times costs six rows: the first sighting, then 10, 100, 1000,
and so on. First sighting is never delayed — the alerting row is written the
moment a signature appears, and the later rows carry the magnitude.

Counts are per process. A restart starts a fresh tally, which the ``since`` stamp
makes legible: a row is "this signature, this many times, starting here", not a
claim about all of history. Readers sum across rows.

Append-only JSONL, advisory, fail-soft — a ledger write must never break the poll
it describes. ``THREAD_ARCHIVE_INGEST_ERRORS_LOG=0`` disables it. At
``max_bytes()`` the file rotates to a stamped segment and a fresh one starts; every
segment is retained (:mod:`.ledger`).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import ledger as _ledger

logger = logging.getLogger(__name__)

LEDGER_FILE = "ingest-errors.jsonl"

#: Substitutions that turn one message into the class of messages it belongs to.
#:
#: Order is the whole correctness argument: every identifier rule must fire before
#: the bare-number rule reaches it. An id is mostly digits, so a digits-first pass
#: rewrites each id to a *different* string of ``N``\ s and every occurrence of one
#: fault signs as its own fault — the exact failure this ledger exists to prevent,
#: reintroduced by the thing meant to prevent it.
#:
#: Every identifier form collapses to the *same* placeholder. A uuid, a ulid, and a
#: reshaped session id are one varying field as far as folding is concerned, so
#: giving them distinct placeholders splits one fault into several rows — which is
#: the failure this ledger exists to prevent, and providers do not reliably hand
#: out canonical ids, so the shapes genuinely do mix within a single fault.
#: The bare-token rule requires hex-only, six or more characters, and at least one
#: digit — together enough to exclude English (no ordinary word is hex-only at that
#: length *and* carries a digit), which matters because over-collapsing is the one
#: failure mode worse than splitting: it merges unrelated faults into a bucket no
#: reader can take apart again.
_VARYING = (
    (re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b"), "<id>"),
    (re.compile(r"\b[0-9a-fA-F]{2,}(?:-[0-9a-fA-F]{2,})+\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{6,}\b"), "<id>"),
    (re.compile(r"(?<![\w/])/[^\s:]+"), "<path>"),
    (re.compile(r"\d+"), "N"),
)

#: A signature's row is rewritten when its count reaches one of these. Powers of
#: ten: enough resolution to tell a blip from an outage, few enough rows that an
#: outage cannot flood the ledger it is being recorded in.
_DECADE = re.compile(r"^10*$")


class _Tally:
    """Per-signature counts for this process, and the first time each was seen."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count: dict[str, int] = {}
        self._since: dict[str, str] = {}

    def bump(self, sig: str, at: str) -> tuple[int, str, bool]:
        """Count one occurrence. Returns ``(count, since, worth_writing)``."""
        with self._lock:
            n = self._count.get(sig, 0) + 1
            self._count[sig] = n
            since = self._since.setdefault(sig, at)
        return n, since, n == 1 or bool(_DECADE.match(str(n)))

    def reset(self) -> None:
        with self._lock:
            self._count.clear()
            self._since.clear()


_TALLY = _Tally()


def signature(message: str) -> str:
    """The class of faults ``message`` belongs to.

    Two messages share a signature when they differ only in which item, path, or
    number they name — which is the axis a recurring fault varies along and the
    one a reader never wants a separate row for."""
    sig = message
    for pattern, placeholder in _VARYING:
        sig = pattern.sub(placeholder, sig)
    return sig[:300]


def source_of(message: str) -> str:
    """The source a watcher error names, by the convention every producer follows:
    the message opens with the source or label, then a colon or a space.

    ``"unknown"`` when a message doesn't follow it — an unattributed fault is still
    worth recording, and guessing an owner for it would be worse than saying so."""
    head = re.split(r"[:\s]", message.strip(), maxsplit=1)[0]
    return head or "unknown"


def max_bytes() -> int:
    return _ledger.env_max_bytes("THREAD_ARCHIVE_INGEST_ERRORS_MAX_BYTES", 8 * 1024 * 1024)


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_INGEST_ERRORS_LOG", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def record(errors: Iterable[str], *, home) -> None:
    """Fold this poll's ``errors`` into the ledger. Never raises.

    Called on every poll that produced an error, ahead of any throttle: the folding
    here is what makes throttling unnecessary, and a throttle in front of it would
    drop the first sighting of a *new* fault to spare the ledger a repeat of an old
    one."""
    if not _enabled():
        return
    try:
        at = datetime.now(timezone.utc).isoformat()
        for message in errors:
            if not message:
                continue
            sig = signature(message)
            count, since, worth_writing = _TALLY.bump(sig, at)
            if not worth_writing:
                continue
            _ledger.append(
                home / LEDGER_FILE,
                {
                    "at": at,
                    "kind": "ingest-error",
                    "source": source_of(message),
                    "signature": sig,
                    "count": count,
                    "since": since,
                    "sample": message[:500],
                },
                max_bytes=max_bytes(),
            )
    except Exception:  # noqa: BLE001 — advisory; the poll loop must survive
        logger.debug("could not record ingest errors", exc_info=True)


def reset_tally() -> None:
    """Forget this process's counts, so the next occurrence is a first sighting."""
    _TALLY.reset()


def summarize(home, *, source: Optional[str] = None) -> list[dict]:
    """Every signature the ledger holds, worst first.

    Each signature's rows are a rising series within one process run, so its total
    is the sum of the *last* row of each run rather than of every row — adding all
    of them would count the same failures once per decade threshold they crossed.

    ``count`` is a **floor**. Rows land on powers of ten, so a fault seen 1,200 times
    last recorded itself at 1,000 and the 200 since are real but unwritten. Callers
    that display it should say so; the number is a magnitude, not a tally."""
    runs: dict[tuple[str, str], dict] = {}
    for row in _ledger.iter_rows(home / LEDGER_FILE):
        sig = row.get("signature")
        if not sig or (source and row.get("source") != source):
            continue
        # (signature, since) identifies one process's run of one fault; later rows
        # of that run supersede earlier ones.
        runs[(sig, row.get("since", ""))] = row
    totals: dict[str, dict] = {}
    for row in runs.values():
        entry = totals.setdefault(row["signature"], {
            "signature": row["signature"],
            "source": row.get("source", "unknown"),
            "count": 0,
            "first": row.get("since", ""),
            "last": row.get("at", ""),
            "sample": row.get("sample", ""),
        })
        entry["count"] += row.get("count", 0)
        entry["first"] = min(entry["first"] or row.get("since", ""), row.get("since", ""))
        entry["last"] = max(entry["last"] or row.get("at", ""), row.get("at", ""))
    return sorted(totals.values(), key=lambda e: -e["count"])
