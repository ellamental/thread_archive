"""Loading an archive as a tracked event: ``<home>/load-runs.jsonl`` + ``load-state.json``.

Bringing an archive's index up to date — replaying truth, rebuilding FTS,
embedding vectors — is the longest-running thing the product does, and on a cold
archive it is measured in hours. Without a record it is also the least visible:
the work happens inside one function call that returns a count at the end, so
"is it stuck or is it working", "how far along is it", and "which phase costs the
hours" are unanswerable while it matters and unanswerable afterwards.

A **run** is one attempt to load an archive. It owns **phases** (truth replay,
FTS, embed, …); a phase carries its wall time, an optional ``done``/``total``
progress counter, and a ``detail`` map of named sub-timings a phase accumulates
itself — which is how the embed phase reports where its hours actually went
(query vs encode vs write) rather than reporting only that it took them.

Two files, both outside ``truth/`` — this is install-local operational telemetry
about an archive, not archive content, so the truth mirror doesn't carry it:

``load-state.json``
    The **live** state, rewritten (atomically, throttled) as a run progresses, so
    any other process — the web viewer, a second CLI, the health page — can read
    how far along an in-flight load is. Carries the writer's ``pid`` so a reader
    can tell a running load from one whose process died mid-phase.

``load-runs.jsonl``
    Append-only history, one row per finished run. Phase timings over time turn
    "the embed is slow" into a series rather than an impression.

Advisory and fail-soft throughout: a telemetry write must never break the load it
describes. ``THREAD_ARCHIVE_LOAD_LOG=0`` disables both files.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

LEDGER_FILE = "load-runs.jsonl"
STATE_FILE = "load-state.json"

# A live-state rewrite costs a small atomic file replace. Throttled so a phase
# that advances thousands of times a second reports smoothly without turning
# progress reporting into the bottleneck it is measuring.
_STATE_WRITE_INTERVAL_S = 1.0


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_LOAD_LOG", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _home(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    from .._config import resolve_paths

    return resolve_paths().home


def state_path(home: Optional[Path] = None) -> Path:
    return _home(home) / STATE_FILE


def ledger_path(home: Optional[Path] = None) -> Path:
    return _home(home) / LEDGER_FILE


def _write_atomic(path: Path, payload: dict) -> None:
    """Replace ``path`` with ``payload`` as one atomic rename, so a reader polling
    the live state never catches a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Phase:
    """One named step of a load, with its own clock, progress counter, and
    sub-timing map.

    ``advance(n)`` moves the progress counter; ``mark(name, seconds)`` accumulates
    into ``detail`` so a phase can account for its own internal split. Both are
    cheap and neither writes to disk — the owning :class:`LoadRun` throttles the
    live-state rewrite."""

    __slots__ = ("name", "total", "done", "started", "elapsed", "detail", "counts", "_run")

    def __init__(self, run: "LoadRun", name: str, total: Optional[int]) -> None:
        self._run = run
        self.name = name
        self.total = total
        self.done = 0
        self.started = time.monotonic()
        self.elapsed = 0.0
        self.detail: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    def advance(self, n: int = 1) -> None:
        """Move the progress counter by ``n`` and let the run refresh live state."""
        self.done += n
        self._run._touch()

    def mark(self, name: str, seconds: float) -> None:
        """Accumulate ``seconds`` under ``name`` in this phase's sub-timing split."""
        self.detail[name] = self.detail.get(name, 0.0) + float(seconds)

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        """Time the block and :meth:`mark` it under ``name``."""
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.mark(name, time.monotonic() - t0)

    def count(self, name: str, n: int) -> None:
        """Record a named count this phase produced (rows written, docs skipped)."""
        self.counts[name] = self.counts.get(name, 0) + int(n)

    def rate(self) -> Optional[float]:
        """Units per second so far, or None before anything has been done."""
        el = self.elapsed or (time.monotonic() - self.started)
        return (self.done / el) if (self.done and el > 0) else None

    def eta_s(self) -> Optional[float]:
        """Seconds remaining at the rate so far — the number that turns a silent
        wait into a known one. None when there's no total or no rate yet."""
        r = self.rate()
        if not r or self.total is None or self.done >= self.total:
            return None
        return (self.total - self.done) / r

    def snapshot(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "done": self.done,
            "total": self.total,
            "elapsed_s": round(self.elapsed or (time.monotonic() - self.started), 3),
        }
        if (r := self.rate()) is not None:
            d["rate_per_s"] = round(r, 2)
        if (e := self.eta_s()) is not None:
            d["eta_s"] = round(e, 1)
        if self.detail:
            d["detail_s"] = {k: round(v, 3) for k, v in self.detail.items()}
        if self.counts:
            d["counts"] = dict(self.counts)
        return d


class NullPhase:
    """A phase that records nothing, with :class:`Phase`'s interface.

    The default for every function that reports progress, so an untracked call
    (a test, a one-off script, a library consumer) runs the same code path as a
    tracked one instead of branching on whether telemetry is present."""

    __slots__ = ("total", "done")

    def __init__(self) -> None:
        self.total: Optional[int] = None
        self.done = 0

    def advance(self, n: int = 1) -> None:
        self.done += n

    def mark(self, name: str, seconds: float) -> None:
        pass

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        yield

    def count(self, name: str, n: int) -> None:
        pass


class LoadRun:
    """One attempt to load an archive, as a sequence of timed phases.

    Constructed by :func:`load_run`, which owns the lifecycle. Every disk write is
    fail-soft: telemetry can't break the load."""

    def __init__(self, kind: str, home: Path, *, note: Optional[str] = None,
                 reporter=None) -> None:
        self.kind = kind
        self.home = home
        self.note = note
        # Called with this run's snapshot on every (throttled) state refresh — the
        # seam a foreground caller uses to show progress. Kept as a callback rather
        # than printing here: the ledger has no business deciding what a CLI, a
        # daemon log, or a test wants to do with the numbers.
        self.reporter = reporter
        self.started_at = _now()
        self._t0 = time.monotonic()
        self.phases: list[Phase] = []
        self.current: Optional[Phase] = None
        self.status = "running"
        self.error: Optional[str] = None
        self._last_write = 0.0

    def snapshot(self) -> dict:
        d: dict[str, Any] = {
            "kind": self.kind,
            "home": str(self.home),
            "pid": os.getpid(),
            "status": self.status,
            "started_at": self.started_at,
            "elapsed_s": round(time.monotonic() - self._t0, 3),
            "phase": self.current.name if self.current else None,
            "phases": [p.snapshot() for p in self.phases],
        }
        if self.note:
            d["note"] = self.note
        if self.error:
            d["error"] = self.error
        return d

    def _touch(self, *, force: bool = False) -> None:
        """Refresh the live-state file and notify the reporter, at most once per
        throttle interval."""
        now = time.monotonic()
        if not force and now - self._last_write < _STATE_WRITE_INTERVAL_S:
            return
        self._last_write = now
        snap = self.snapshot()
        if _enabled():
            try:
                _write_atomic(state_path(self.home), snap)
            except OSError as e:
                logger.debug("load telemetry: live state write failed (%s)", e)
        if self.reporter is not None:
            try:
                self.reporter(snap)
            except Exception as e:  # noqa: BLE001 — a reporter must not break the load
                logger.debug("load telemetry: reporter failed (%s)", e)

    @contextmanager
    def phase(self, name: str, total: Optional[int] = None) -> Iterator[Phase]:
        """Run a named phase, timed. ``total`` (when known) is what makes the
        phase's progress an ETA rather than just a counter."""
        ph = Phase(self, name, total)
        self.phases.append(ph)
        self.current = ph
        self._touch(force=True)
        try:
            yield ph
        finally:
            ph.elapsed = time.monotonic() - ph.started
            self.current = None
            self._touch(force=True)

    def _finish(self, status: str, error: Optional[str] = None) -> None:
        self.status = status
        self.error = error
        self._touch(force=True)
        if not _enabled():
            return
        record = {"at": _now(), "kind": "load-run",
                  "duration_s": round(time.monotonic() - self._t0, 3), **self.snapshot()}
        try:
            path = ledger_path(self.home)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.debug("load telemetry: ledger append failed (%s)", e)


@contextmanager
def load_run(kind: str, *, home: Optional[Path] = None, note: Optional[str] = None,
             reporter=None) -> Iterator[LoadRun]:
    """Track one archive load as ``kind`` (``reindex`` / ``embed`` / ``import``).

    Publishes live progress to ``<home>/load-state.json`` while it runs and appends
    a summary to ``<home>/load-runs.jsonl`` when it ends — including when it ends by
    raising, which is the case a "returns a count at the end" design loses entirely.
    The exception propagates; only the telemetry is swallowed. ``reporter`` is
    called with the run snapshot on each refresh, for a caller showing progress."""
    run = LoadRun(kind, _home(home), note=note, reporter=reporter)
    run._touch(force=True)
    try:
        yield run
    except BaseException as e:
        run._finish("failed", f"{type(e).__name__}: {e}")
        raise
    else:
        run._finish("ok")


# ── readers ───────────────────────────────────────────────────────────────────
def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def read_state(home: Optional[Path] = None, *, alive=_pid_alive) -> dict:
    """The live load state for ``home``, or ``{}``.

    A ``running`` state whose writing process is gone is reported as ``stalled``:
    a load that died mid-phase leaves its last state behind, and reporting that as
    still-running is the exact failure the record exists to make visible. ``alive``
    is the liveness predicate over the state's ``pid`` — the default checks this
    machine; a consumer watching an archive on another host injects its own."""
    try:
        state = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    if state.get("status") == "running" and not alive(state.get("pid")):
        state["status"] = "stalled"
    return state


def read_runs(limit: int = 20, home: Optional[Path] = None) -> list[dict]:
    """The most recent finished load runs, newest first."""
    try:
        lines = ledger_path(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in reversed(lines):
        if len(out) >= limit:
            break
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
