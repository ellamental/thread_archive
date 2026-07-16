"""Scheduled self-curation — the librarian and gardener drains.

The archive's headline behavior — "a memory that organizes itself" — needs a
driver, not just tools. This module is that driver: each drain spawns a
headless Claude Code instance (the ``claude`` CLI) against the archive's own
MCP servers and lets it work the curation queues.

Two drains, complements of each other:

* the **librarian** (hourly agent) curates *conversations* — per thread, ~3+
  salient message→topic citations/links plus a short search-first summary;
* the **gardener** (daily agent) curates the *graph* — merges duplicate
  topics, connects singletons, grows the part-of hierarchy, archives husks.

Each fire gates on **work left**: a read-only count over the SQLite index that
mirrors the corresponding queue's eligibility (``review_queue`` for the
librarian, the garden queues for the gardener), so a drained archive costs one
cheap query, not a Claude launch. If the definitions ever drift, the failure
mode is a launch that finds an empty queue and exits — visible in the run log,
never a silently parked drain; a *failed* count fails open (launches anyway)
for the same reason. Each launched fire is bounded twice: a per-run batch cap
passed in the prompt, and a hard subprocess timeout — timing out is the normal
exit path when the backlog outlasts one window, and the next fire continues.
A heartbeat file under ``<home>/logs/`` is touched on every fire, launched or
skipped, so freshness monitoring can tell "drained" from "dead".

The instance's MCP surface is strict and dedicated: a generated config
(``<home>/curation-mcp.json``) carrying exactly the archive's two servers —
``thread-archive`` (read; the shared HTTP server when it's up, else a stdio
subprocess) and ``thread-archive-librarian`` (curation writes) — passed with
``--mcp-config … --strict-mcp-config`` so whatever other servers the user's
Claude config carries never leak into an unattended, permission-bypassing run.

Each drain is operator-configurable per drain in ``config.json`` under
``curation.<kind>``: ``model`` / ``effort`` (defaults: Opus at xhigh effort,
read at fire time) and cadence — ``interval_minutes`` for the librarian,
``at`` ("HH:MM") for the gardener — read when the LaunchAgent is
(re)installed.

Requires the ``claude`` CLI (any login it already has pays for the runs); the
wizard and ``archive daemon install --librarian/--gardener`` only offer the
schedule when it's present.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Optional

from .._config import load_config, resolve_paths

logger = logging.getLogger(__name__)

# The claude CLI's model alias for the latest Opus — capable enough for
# unattended curation without pinning a dated snapshot id that a user's CLI may
# not know — at its highest reasoning effort (curation quality is worth the
# tokens; the runs are already batch-capped). Per-drain overrides live in
# ``config.json`` under ``curation.<kind>.model`` / ``.effort``.
DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "xhigh"

# Hard wall-clock stop for one drain, seconds. Generous — a batch of long
# threads is slow — and hitting it is a normal exit (the next fire continues).
TIMEOUT_S = 1800

# Mirror of review_queue's hold-back window: a thread that ingested events this
# recently is held out of the librarian's queue (likely a live session), so the
# gate must not count it either — counting it could launch an instance into an
# empty queue.
QUIET_MINUTES = 60


def _index_path(home: Optional[str]) -> Path:
    return resolve_paths(home).index_path


def librarian_backlog(home: Optional[str] = None) -> Optional[int]:
    """Count the conversation threads the librarian's queue still considers
    un-done.

    Un-done = event-bearing, non-archived conversation thread missing either
    half of the librarian's per-thread commit: a live topic citation (or a link
    touching it), or a non-empty stored summary — mirroring
    ``review_queue``'s eligibility, including its QUIET_MINUTES hold-back.
    Read-only over the SQLite index; ``None`` when the query failed (the
    caller fails open).
    """
    try:
        conn = sqlite3.connect(f"file:{_index_path(home)}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute(
                "SELECT count(*) FROM threads t "
                "WHERE t.thread_type = 'conversation' AND NOT t.archived "
                "  AND EXISTS (SELECT 1 FROM events e WHERE e.thread_id = t.id) "
                "  AND NOT EXISTS (SELECT 1 FROM events eq WHERE eq.thread_id = t.id "
                "                  AND eq.recorded_at >= datetime('now', :quiet)) "
                "  AND ( "
                "    t.summary IS NULL OR trim(t.summary) = '' "
                "    OR NOT ( "
                "      EXISTS (SELECT 1 FROM topic_messages tm "
                "              WHERE tm.thread_id = t.id AND tm.archived_at IS NULL) "
                "      OR EXISTS (SELECT 1 FROM thread_links tl "
                "                 WHERE tl.source_thread_id = t.id "
                "                    OR tl.target_thread_id = t.id) "
                "    ) "
                "  )",
                {"quiet": f"-{QUIET_MINUTES} minutes"},
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except Exception:
        logger.exception("librarian: backlog count failed")
        return None


def gardener_backlog(home: Optional[str] = None) -> Optional[int]:
    """Count the live topics the garden queues still consider broken.

    Broken = a live (non-archived) topic that is any of: a **singleton** (no
    link of any type to another live topic), **uncited** (no live citation),
    or **outside the hierarchy** (no part-of/contains edge to/from a live
    topic) — mirroring the garden queues' eligibility. Near-duplicate title
    pairs are diagnosed in-process by the knowledge layer and aren't
    SQL-expressible, so the gate deliberately ignores them; a dupes-only
    backlog is cleared the next time any other kind has work. Read-only over
    the SQLite index; ``None`` when the query failed (the caller fails open).
    """
    live_other = (
        "SELECT 1 FROM thread_links l JOIN threads o ON o.id = "
        "  CASE WHEN l.source_thread_id = t.id THEN l.target_thread_id "
        "       ELSE l.source_thread_id END "
        "WHERE (l.source_thread_id = t.id OR l.target_thread_id = t.id) "
        "  AND l.source_thread_id != l.target_thread_id "
        "  AND o.thread_type = 'topic' AND NOT o.archived"
    )
    try:
        conn = sqlite3.connect(f"file:{_index_path(home)}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute(
                "SELECT count(*) FROM threads t "
                "WHERE t.thread_type = 'topic' AND NOT t.archived "
                "  AND ( "
                "    NOT EXISTS (SELECT 1 FROM topic_messages tm "
                "                WHERE tm.topic_id = t.id AND tm.archived_at IS NULL) "
                f"    OR NOT EXISTS ({live_other}) "
                f"    OR NOT EXISTS ({live_other} AND l.link_type IN ('part-of', 'contains')) "
                "  )"
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except Exception:
        logger.exception("gardener: backlog count failed")
        return None


@dataclass(frozen=True)
class Drain:
    """One curation drain: its prompt, gate, per-run cap, and heartbeat."""

    kind: str
    prompt_file: str
    batch: int  # default per-run cap, in cap_unit
    cap_unit: str  # what the cap counts, for the appended run instruction
    gate: Callable[[Optional[str]], Optional[int]]


DRAINS = {
    "librarian": Drain(
        kind="librarian",
        prompt_file="librarian.md",
        batch=25,
        cap_unit="threads",
        gate=librarian_backlog,
    ),
    "gardener": Drain(
        kind="gardener",
        prompt_file="gardener.md",
        batch=30,
        cap_unit="write actions (merge / link / archive / rename each count as one)",
        gate=gardener_backlog,
    ),
}


def drain_config(kind: str, home: Optional[str] = None) -> dict:
    """This drain's entry in ``config.json`` (``curation.<kind>``), or ``{}``.

    Fail-soft like every config read: a missing or malformed entry means all
    defaults, never a dead drain.
    """
    entry = load_config(home).get("curation", {})
    entry = entry.get(kind, {}) if isinstance(entry, dict) else {}
    return entry if isinstance(entry, dict) else {}


def curation_settings(kind: str, home: Optional[str] = None) -> tuple[str, str]:
    """The ``(model, effort)`` this drain should run with.

    Read from ``config.json`` under ``curation.<kind>`` — e.g.
    ``{"curation": {"gardener": {"model": "opus", "effort": "xhigh"}}}`` —
    with each field falling back to the defaults independently. An explicitly
    empty ``effort`` ("" or null) means "don't pass ``--effort`` at all", the
    escape hatch for a claude CLI that doesn't know the flag. Fail-soft like
    every config read: malformed entries mean defaults, never a dead drain.
    """
    entry = drain_config(kind, home)
    model = entry.get("model", DEFAULT_MODEL)
    effort = entry.get("effort", DEFAULT_EFFORT)
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_MODEL
    if effort is None:
        effort = ""
    elif not isinstance(effort, str):
        effort = DEFAULT_EFFORT
    return model.strip(), effort.strip()


def librarian_interval(home: Optional[str] = None) -> Optional[int]:
    """The configured librarian fire interval in **seconds**, or ``None``
    (use the installer's default).

    Config key: ``curation.librarian.interval_minutes`` (a positive number).
    Applied when the LaunchAgent is (re)installed, not per fire — edit the
    config, then re-run ``archive daemon install --librarian``.
    """
    raw = drain_config("librarian", home).get("interval_minutes")
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return int(raw * 60)
    logger.warning(
        "librarian: config interval_minutes=%r is not a positive number — "
        "using the default cadence", raw,
    )
    return None


def gardener_at(home: Optional[str] = None) -> Optional[tuple[int, int]]:
    """The configured daily gardener fire time as ``(hour, minute)``, or
    ``None`` (use the installer's default).

    Config key: ``curation.gardener.at``, an ``"HH:MM"`` string (local time).
    Applied when the LaunchAgent is (re)installed, not per fire — edit the
    config, then re-run ``archive daemon install --gardener``.
    """
    raw = drain_config("gardener", home).get("at")
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = raw.strip().split(":")
        if len(parts) == 2:
            try:
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError:
                pass
            else:
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return hour, minute
    logger.warning(
        "gardener: config at=%r is not an HH:MM time — using the default "
        "cadence", raw,
    )
    return None


def resolve_claude() -> Optional[str]:
    """Locate the claude CLI: PATH first, then the standard ~/.local/bin install."""
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.is_file() else None


def _touch_heartbeat(kind: str, home: Optional[str]) -> None:
    """Mtime-only heartbeat: every fire touches it, launched or skipped.

    Written under ``<home>/logs/``, and mirrored as ``archive-<kind>.heartbeat``
    into the thread-family heartbeat dir when it exists (same contract as the
    nightly's family heartbeat in ``_ops/health.py``: fail-soft, skipped
    entirely outside the family, env-overridable so tests never stamp the real
    box's beat)."""
    hb = resolve_paths(home).home / "logs" / f"{kind}.heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.touch()
    family_dir = Path(
        os.environ.get("THREAD_ARCHIVE_HEARTBEAT_DIR")
        or Path.home() / ".thread" / "logs"
    )
    if family_dir.is_dir():
        try:
            (family_dir / f"archive-{kind}.heartbeat").touch()
        except OSError:
            logger.warning("%s: family heartbeat stamp failed", kind, exc_info=True)


def shared_mcp_up(port: int) -> bool:
    """Whether the shared archive-mcp HTTP server answers on loopback."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def mcp_config(home: Optional[str] = None) -> dict:
    """The strict MCP surface for a curation run: the archive's two servers,
    absolute commands, nothing else.

    The read server prefers the shared HTTP archive-mcp when it's up (one
    resident retrieval model instead of a fresh stdio subprocess per run);
    the librarian (write) server is always a stdio subprocess — it's light.
    Both stdio entries pin ``THREAD_ARCHIVE_HOME`` so the run curates the same
    home it was gated on, whatever the launching environment says.
    """
    from .._launchd import MCP_DEFAULT_PORT
    from .._setup.clients import console_script

    home_env = {"THREAD_ARCHIVE_HOME": str(resolve_paths(home).home)}
    if shared_mcp_up(MCP_DEFAULT_PORT):
        read: dict = {"type": "http", "url": f"http://127.0.0.1:{MCP_DEFAULT_PORT}/mcp"}
    else:
        read = {"command": console_script("archive-mcp"), "env": dict(home_env)}
    return {
        "mcpServers": {
            "thread-archive": read,
            "thread-archive-librarian": {
                "command": console_script("archive-librarian-mcp"),
                "env": dict(home_env),
                # Curation writes rebuild graph state on first use; don't let a
                # cold start read as a dead server.
                "timeoutMs": 120000,
            },
        }
    }


def prompt_text(kind: str, batch: int) -> str:
    """The packaged drain prompt plus this run's cap instruction."""
    drain = DRAINS[kind]
    base = (resources.files(__package__) / drain.prompt_file).read_text(encoding="utf-8")
    return (
        f"{base.rstrip()}\n\n## This run\n\n"
        f"Perform at most {batch} {drain.cap_unit} this run, then stop and "
        f"summarize what you did in a few lines."
    )


def run(
    kind: str,
    home: Optional[str] = None,
    *,
    batch: Optional[int] = None,
    timeout: int = TIMEOUT_S,
    gate: Optional[Callable[[Optional[str]], Optional[int]]] = None,
    claude: Optional[str] = None,
) -> int:
    """One fire: gate on work left, launch the drain, heartbeat either way.

    ``gate`` and ``claude`` are parameters so tests drive the launch flow
    without faking this module's internals; production callers use the
    defaults. Always returns 0 — a launchd one-shot has nobody to signal, and
    every outcome (skipped, drained, timed out, failed launch) is in the log.
    """
    drain = DRAINS[kind]
    batch = drain.batch if batch is None else batch

    backlog = (gate or drain.gate)(home)
    if backlog == 0:
        logger.info("%s: queue is drained (0 items) — skipping this fire", kind)
        _touch_heartbeat(kind, home)
        return 0
    logger.info(
        "%s: %s item(s) in the queue — launching drain",
        kind,
        backlog if backlog is not None else "unknown",
    )

    cli = claude or resolve_claude()
    if not cli:
        logger.error("%s: no `claude` CLI found — install Claude Code, or "
                     "`archive daemon uninstall --%s` to drop the schedule", kind, kind)
        _touch_heartbeat(kind, home)
        return 0

    paths = resolve_paths(home)
    config_path = paths.home / "curation-mcp.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(mcp_config(home), indent=2), encoding="utf-8")

    model, effort = curation_settings(kind, home)
    args = [
        cli,
        "--print",
        # Unattended run: nobody is present to approve tool calls. The strict
        # MCP config below is what keeps the blast radius to the archive's own
        # servers.
        "--permission-mode", "bypassPermissions",
        "--model", model,
        *(["--effort", effort] if effort else []),
        "--mcp-config", str(config_path),
        "--strict-mcp-config",
        prompt_text(kind, batch),
    ]

    logger.info(
        "%s: launching headless claude (model=%s%s, batch=%d, timeout=%ds)",
        kind, model, f", effort={effort}" if effort else "", batch, timeout,
    )
    try:
        # Own session + group-kill on timeout: the instance spawns MCP/tool
        # subprocesses, and timeout is a normal exit path here — a plain
        # subprocess.run(timeout=...) would SIGKILL only the direct child and
        # orphan that tree every window.
        proc = subprocess.Popen(
            args,
            cwd=str(Path.home()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            tail = (stdout or "").strip().splitlines()
            logger.info(
                "%s: instance exited rc=%s; %s",
                kind, proc.returncode, tail[-1] if tail else "(no output)",
            )
            if proc.returncode != 0 and stderr:
                logger.warning("%s: stderr tail: %s", kind, stderr.strip()[-500:])
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            logger.info("%s: hit the %ds budget; the next fire continues", kind, timeout)
    except Exception:
        logger.exception("%s: launch failed", kind)

    _touch_heartbeat(kind, home)
    return 0
