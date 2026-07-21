"""First contact and the human status view — the ``thread_archive`` command.

The consumer front door: install the package, run ``thread_archive``, and the
product explains itself — it discovers the machine's conversation stores and
shows what it found *before* touching anything, states exactly where copies
will live (local only), imports with consent and narration, then offers the
always-on watcher, a scheduled nightly backup, and MCP wiring. Every step can
be skipped, and decisions persist in ``<home>/config.json`` (see
:mod:`.._config`) where every ingest path respects them.

``archive`` remains the operator seam (backup / verify / nightly / daemon);
this command owns setup and status only — retrieval stays with the MCP tools
and the web viewer.

Non-interactive use: ``thread_archive --yes`` accepts every default without
prompting (how an agent drives it). Without ``--yes``, a non-TTY invocation
performs no work — discovery and guidance only, never a surprise ingest.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import __version__
from .._config import load_config, resolve_paths, save_config
from .machine import Machine


def source_label(name: str) -> str:
    """A source's human label, falling back to its bare name.

    Read from the provider registry, so a plugin's source is presented by the
    name its author chose rather than appearing here as an unlabelled string.
    Fail-soft: setup must still run if the registry can't be built.
    """
    try:
        from .._providers import labels

        return labels().get(name, name)
    except Exception:  # noqa: BLE001 — a label is never worth failing setup over
        return name


def _export_labels() -> str:
    """The account-export vendors setup can name, from the registry."""
    try:
        from .._providers import export_specs

        return " / ".join(spec.label for _, spec in export_specs()) or "none registered"
    except Exception:  # noqa: BLE001 — never worth failing setup over
        return "account exports"


def _followers() -> list:
    """Providers whose enablement follows another source's."""
    try:
        from .._providers import registry

        return [p for p in registry().values() if p.follows]
    except Exception:  # noqa: BLE001 — never worth failing setup over
        return []


# ── small formatting helpers ─────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"  # pragma: no cover — unreachable


def _fmt_when(mtime: Optional[float]) -> str:
    if mtime is None:
        return "?"
    age = time.time() - mtime
    if age < 48 * 3600:
        return "today" if age < 24 * 3600 else "yesterday"
    return datetime.fromtimestamp(mtime).strftime("%b %Y")


def _fmt_range(earliest: Optional[float], latest: Optional[float]) -> str:
    if earliest is None and latest is None:
        return ""
    lo, hi = _fmt_when(earliest), _fmt_when(latest)
    return lo if lo == hi else f"{lo} → {hi}"


def _say(msg: str = "") -> None:
    print(msg, flush=True)


# ── prompting ────────────────────────────────────────────────────────────────


def _interactive(args: argparse.Namespace) -> bool:
    return not args.yes and sys.stdin.isatty() and sys.stdout.isatty()


def _ask(
    prompt: str, *, default: str, interactive: bool, read: Callable[[str], str] = input
) -> str:
    """One line of input, lowercased; Enter (or any non-TTY path) → default.

    ``read`` is where answers come from — the terminal by default, another
    reader when the flow is driven from somewhere else."""
    if not interactive:
        return default
    try:
        raw = read(prompt).strip().lower()
    except EOFError:
        return default
    return raw or default


def _ask_path(
    prompt: str, *, interactive: bool, read: Callable[[str], str] = input
) -> str:
    """A path prompt. Unlike :func:`_ask` it preserves case (a filesystem path
    is case-sensitive); empty string on skip / EOF / non-TTY."""
    if not interactive:
        return ""
    try:
        return read(prompt).strip()
    except EOFError:
        return ""


# ── the setup flow ───────────────────────────────────────────────────────────


def run_setup(
    args: argparse.Namespace,
    watchers: Optional[list] = None,
    *,
    interactive: Optional[bool] = None,
    ask: Callable[..., str] = _ask,
    machine: Optional[Machine] = None,
) -> int:
    """Discover → consent → import → watcher → backup → MCP wiring.
    Returns exit code.

    The flow's collaborators are keyword parameters: ``interactive`` overrides
    the TTY auto-detect, ``ask`` is the prompt, ``watchers`` are the sources to
    discover, and ``machine`` (see :mod:`.machine`) is the host whose scheduled
    agents setup reads and installs — so a run can be scripted end to end
    without the flow reaching for this process's own terminal and launchd.
    """
    from .._watcher import provider_watchers

    machine = machine if machine is not None else Machine()
    interactive = _interactive(args) if interactive is None else interactive
    if not interactive and not args.yes:
        # No TTY and no --yes: never ingest as a side effect of being glanced at.
        _say("thread_archive: no terminal to ask questions in.")
        _say("  run `thread_archive` interactively, or `thread_archive --yes` to accept")
        _say("  every default (discover + import all sources, install the watcher,")
        _say("  wire detected MCP clients). Nothing was imported.")
        return 0

    paths = resolve_paths(args.home)
    cfg = load_config(args.home)

    _say("thread_archive — every AI conversation on this machine, archived and searchable.")
    _say()
    _say(f"  archive home: {paths.home}  (plain JSONL + SQLite)")
    _say("  local only: conversations are COPIED into that directory. Nothing is uploaded anywhere.")
    _say()

    # 1. Discover — stat-only, nothing imported yet.
    _say("Scanning for conversation stores…")
    watchers = watchers if watchers is not None else provider_watchers()
    found, absent = [], []
    for w in watchers:
        try:
            report = w.discover()
        except Exception:  # noqa: BLE001 — a broken store must not stop setup
            report = None
        if report is not None and report.available:
            found.append((w, report))
        else:
            absent.append(w.source_name)
    for w, r in found:
        label = source_label(r.name)
        items = f"{r.items:,} sessions" if r.items is not None else "live store"
        span = _fmt_range(r.earliest, r.latest)
        _say(f"  [x] {label:<15} {items:>15}   {_fmt_bytes(r.bytes):>9}   {span}")
    if absent:
        _say(f"  not found: {', '.join(source_label(n) for n in absent)}")
    _say(f"  account exports ({_export_labels()}): drop the ZIP into {paths.dumps_dir} anytime.")
    _say()

    # 2. Consent + selection.
    selected = [w for w, _ in found]
    edited = False
    if not found:
        _say("No local stores found — the archive starts empty and fills as sources appear.")
        do_import = False
    else:
        total = _fmt_bytes(sum(r.bytes for _, r in found))
        answer = ask(
            f"Import these now? Roughly {total} of source data; a large store takes a few minutes.\n"
            "  [Enter] import all · e = edit selection · s = skip import  > ",
            default="", interactive=interactive,
        )
        if answer == "e":
            edited = True
            selected = []
            for w, r in found:
                label = source_label(r.name)
                keep = ask(f"  include {label}? [Y/n] > ", default="y", interactive=interactive)
                if keep not in ("n", "no"):
                    selected.append(w)
        do_import = answer not in ("s", "n", "no") and bool(selected)

    # Persist source opt-outs (only deviations are recorded; absence = enabled).
    # A per-source "no" in the edit pass is a lasting opt-out; skipping the
    # import wholesale is not — the source stays enabled for later ingest.
    chosen = {w.source_name for w in selected}
    sources_cfg = cfg.setdefault("sources", {})
    for w, _ in found:
        name = w.source_name
        if edited and name not in chosen:
            sources_cfg[name] = {"enabled": False}
        else:
            sources_cfg.pop(name, None)
    # A source that follows another (a recovery pass over its store) inherits that
    # source's choice — recovering from a store the operator opted out of would
    # reintroduce exactly what they declined. Ingest enforces this too; writing it
    # here as well keeps config.json a full statement of what will be captured.
    for follower in _followers():
        target = sources_cfg.get(follower.follows or "", {})
        if isinstance(target, dict) and target.get("enabled") is False:
            sources_cfg[follower.name] = {"enabled": False}
        else:
            sources_cfg.pop(follower.name, None)
    save_config(cfg, args.home)

    # 3. Import, narrated per source.
    if do_import and not args.skip_import:
        _import_selected(args, selected)
    elif args.skip_import:
        _say("Import skipped (--skip-import).")
    _say()

    # 4. The always-on watcher.
    cfg["setup"] = cfg.get("setup", {})
    cfg["setup"]["watcher"] = _offer_watcher(args, interactive, machine, ask=ask)
    _say()

    # 5. The scheduled nightly backup.
    cfg["setup"]["backup"] = _offer_backup(args, interactive, machine)
    _say()

    # 6. MCP wiring.
    cfg["setup"]["clients"] = {"claude": _offer_mcp(args, interactive, ask=ask)}
    _say()

    # 7. Done.
    cfg["setup"]["completed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save_config(cfg, args.home)
    _say("Done. Ask your agent: \"what have we discussed about …?\"")
    _say("  status anytime:   thread_archive")
    if cfg["setup"]["watcher"] in ("launchd", "already-running"):
        _say("  web viewer:       http://127.0.0.1:8787")
    _say(f"  account exports:  drop ZIPs into {paths.dumps_dir}")
    if not machine.embeddings_installed():
        _say("  semantic search:  not installed — `.venv/bin/pip install -e '.[embeddings]'` from the clone adds it (large: torch)")
    return 0


def _import_selected(args: argparse.Namespace, selected: list) -> None:
    from .. import _api as api

    api.open_archive(args.home)
    _say()
    # Importer warnings (unjoinable subagents, odd provider rows) are log
    # detail, not conversation — with no handler configured they'd land on the
    # user's terminal via logging.lastResort and bury the narration. During
    # setup they go to a file instead.
    log_dir = resolve_paths(args.home).home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "setup.log"
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        _import_selected_locked(args, selected, log_path)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    st = api.status(home=args.home)
    _say(f"  archive now: {st['threads']:,} conversations, {st['events']:,} events, searchable.")


def _import_selected_locked(args: argparse.Namespace, selected: list, log_path) -> None:
    from .._truth import shared_ingest_lock
    from .._watcher import Watcher
    from .._watcher.lazy import try_ingest_owner_lock

    with try_ingest_owner_lock() as owned:
        if not owned:
            _say("An always-on watcher already ingests for this archive — skipping the manual")
            _say("import; it is (or will shortly be) caught up.")
            return
        _say("Importing…")
        totals_events = 0
        errors: list[str] = []
        with shared_ingest_lock():
            for w in selected:
                label = source_label(w.source_name)
                print(f"  {label:<15} ", end="", flush=True)
                t0 = time.monotonic()
                try:
                    result = w.poll()
                except Exception as e:  # noqa: BLE001 — one source must not stop setup
                    _say(f"failed: {e}")
                    errors.append(f"{w.source_name}: {e}")
                    continue
                totals_events += result.events_created
                errors.extend(result.errors)
                _say(
                    f"{result.items_imported:,} conversations, "
                    f"{result.events_created:,} events  ({time.monotonic() - t0:.0f}s)"
                )
            if totals_events:
                Watcher(watchers=selected, home=args.home, embed=False).maintain()
        if errors:
            _say(f"  {len(errors)} item(s) could not be imported (the rest are in; details: {log_path}):")
            for err in errors[:3]:
                _say(f"    ! {err}")


def _offer_watcher(
    args: argparse.Namespace,
    interactive: bool,
    machine: Machine,
    *,
    ask: Callable[..., str] = _ask,
) -> str:
    """Offer the always-fresh upgrade. Returns the recorded outcome."""
    if args.skip_watcher:
        _say("Watcher skipped (--skip-watcher).")
        return "skipped"
    if not machine.macos:
        _say("Keep it fresh: the always-on watcher ships for macOS only right now.")
        _say("  Without it, setup can explicitly enable catch-up ingest in the archive's")
        _say("  MCP client entry — searches stay close to current when those tools are used.")
        return "unavailable"

    if machine.watcher_running(args.home):
        _say("Keep it fresh: the always-on watcher is already installed and running.")
        return "already-running"
    _say("Keep it fresh? A background watcher (launchd) tails these stores so new")
    _say("conversations land within seconds, and serves the web viewer at http://127.0.0.1:8787.")
    _say("It also checks release tags about once a day and reports when an update is")
    _say("available. Applying it is explicit: `archive self-update`.")
    answer = ask(
        "  [Enter] install watcher · s = skip (MCP wiring can enable catch-up)  > ",
        default="", interactive=interactive,
    )
    if answer in ("s", "n", "no"):
        _say("  Skipped — opted-in MCP catch-up covers freshness; `thread_archive setup` to revisit.")
        return "skipped"
    try:
        machine.install_watcher(args.home)
    except SystemExit as e:
        _say(f"  Could not install the watcher: {e}")
        _say("  Opted-in MCP catch-up still covers freshness; `archive daemon install` to retry.")
        return "failed"
    _say("  Installed — always-on, restarts on crash, web viewer at http://127.0.0.1:8787.")
    return "launchd"


def _conversation_count(home: Optional[str]) -> int:
    """Conversations already on disk — the size of the history a catch-up would
    work through. Best-effort: the offer degrades to a generic prompt, never an
    error, if the store can't be counted."""
    try:
        from sqlalchemy import func, select

        from .. import _api as api
        from .._store import Thread, get_session

        api.open_archive(home)
        with get_session() as s:
            return int(s.execute(
                select(func.count()).select_from(Thread).where(
                    Thread.thread_type == "conversation"
                )
            ).scalar_one() or 0)
    except Exception:  # noqa: BLE001 — a count is not worth failing setup over
        return 0


def _offer_backup(args: argparse.Namespace, interactive: bool, machine: Machine) -> dict:
    """Offer to schedule the nightly backup pipeline (backup → verify →
    restore drill). Returns the recorded outcome: ``{"status": ...}`` plus a
    ``dest`` when one is known."""
    if args.skip_backup:
        _say("Nightly backup skipped (--skip-backup).")
        return {"status": "skipped"}
    if not machine.macos:
        _say("Backups: scheduled nightly backup ships for macOS only right now.")
        _say("  Back up by hand anytime with `archive backup <dest>` (a copy of truth/ IS")
        _say("  the backup), or point your own scheduler at `archive nightly <dest>`.")
        return {"status": "unavailable"}

    # An already-loaded backup agent is left untouched — this is what keeps the
    # wizard from clobbering an operator-installed pipeline (e.g. the host/ layer's
    # NAS backup with its own remount + notify wiring) on a re-run.
    if machine.backup_running(args.home):
        dest = machine.backup_dest()
        _say("Backups: a nightly backup job is already installed"
             + (f" → {dest}." if dest else "."))
        return {"status": "already-installed", **({"dest": dest} if dest else {})}

    _say("Schedule backups? A nightly job (launchd) mirrors the archive to a directory,")
    _say("verifies it, and runs a restore drill.")
    dest = args.backup_dest or _ask_path(
        "  Where should nightly backups go? A directory of your choice,\n"
        "  or [Enter] to skip  > ",
        interactive=interactive,
    )
    if not dest:
        _say("  Skipped — back up anytime with `archive backup <dest>`; "
             "`thread_archive setup` to revisit.")
        return {"status": "skipped"}
    dest_path = Path(dest).expanduser()
    if not dest_path.is_absolute():
        dest_path = dest_path.resolve()
    try:
        machine.install_backup(str(dest_path), args.home)
    except SystemExit as e:
        _say(f"  Could not schedule backup: {e}")
        _say("  Back up by hand with `archive backup <dest>`, or "
             "`archive daemon install --backup --dest <path>` to retry.")
        return {"status": "failed"}
    _say(f"  Scheduled — nightly at 04:00 → {dest_path}: backup, verify, restore drill.")
    _say("  `thread_archive` shows the last run's result.")
    return {"status": "launchd", "dest": str(dest_path)}


def _offer_mcp(
    args: argparse.Namespace, interactive: bool, *, ask: Callable[..., str] = _ask
) -> str:
    """Offer to wire detected MCP clients. Returns the recorded outcome."""
    from . import clients

    if args.skip_mcp:
        _say("MCP wiring skipped (--skip-mcp).")
        return "skipped"
    cli = clients.claude_cli()
    if cli is None:
        _say("Connect your agents: no supported client CLI found (looked for: claude).")
        _say("  MCP config for any client:")
        _say(_indent(clients.mcp_config_block(args.home)))
        return "printed"
    wired, problem = clients.claude_server_report(cli, home=args.home)
    if wired:
        _say("Connect your agents: claude already has the archive's MCP server"
             " for this archive.")
        return "already-wired"
    _say("Connect your agents? Found: claude (Claude Code).")
    if problem:
        _say(f"  Note: {problem}; wiring adds a user-scope entry for this archive.")
    answer = ask(
        "  [Enter] wire MCP (search/read, user scope) · p = print config only · s = skip  > ",
        default="", interactive=interactive,
    )
    if answer == "p":
        _say(_indent(clients.mcp_config_block(args.home)))
        return "printed"
    if answer in ("s", "n", "no"):
        _say("  Skipped — `thread_archive setup` to revisit, or wire any client with:")
        _say(_indent(clients.mcp_config_block(args.home)))
        return "skipped"
    errors = clients.wire_claude(cli, home=args.home)
    if errors:
        _say("  Wiring hit trouble:")
        for err in errors:
            _say(f"    ! {err}")
        _say("  Manual config for any MCP client:")
        _say(_indent(clients.mcp_config_block(args.home)))
        return "failed"
    _say("  Wired: thread-archive (search/read), user scope — every Claude Code")
    _say("  session can now search this archive.")
    return "wired"


def _indent(block: str, by: str = "    ") -> str:
    return "\n".join(by + line for line in block.splitlines())


# ── status ───────────────────────────────────────────────────────────────────


def print_status(args: argparse.Namespace, *, machine: Optional[Machine] = None) -> int:
    from .. import _api as api
    from ..cli import _age

    machine = machine if machine is not None else Machine()
    st = api.status(home=args.home)
    cfg = load_config(args.home)

    _say("thread_archive — archive status")
    _say()
    _say(f"  home:     {st['home']}")
    # Topic threads are curation artifacts, not conversations — counting them as
    # conversations overstates what was actually preserved.
    topics = st.get("topics", 0)
    convs = st["threads"] - topics
    topics_part = f" · {topics:,} topics" if topics else ""
    _say(f"  archive:  {convs:,} conversations{topics_part} · {st['events']:,} events · {st['fts_indexed']:,} indexed")
    if machine.macos:
        _say(f"  watcher:  {'running' if machine.watcher_running(args.home) else 'not running — `thread_archive setup` offers it'}")
    disabled = sorted(
        name for name, entry in cfg.get("sources", {}).items()
        if isinstance(entry, dict) and entry.get("enabled") is False
    )
    _say(f"  sources:  {'disabled: ' + ', '.join(disabled) if disabled else 'all enabled'}")
    v, b = st.get("last_verify"), st.get("last_backup")
    if b:
        _say(f"  backup:   {'ok' if b['ok'] else 'FAILED'} {_age(b['at'])} → {b['dest']}")
    else:
        _say("  backup:   never recorded — a copy of truth/ IS the backup")
    if v:
        _say(f"  verify:   {'ok' if v['ok'] else 'FAILED'} {_age(v['at'])}")
    # The scheduled pipeline's verdict outranks the ad-hoc backup line above: a
    # green ad-hoc backup must not mask a red scheduled nightly.
    n = st.get("last_nightly")
    if n and not n.get("ok"):
        stages = ", ".join(n.get("failed_stages") or []) or "see logs/backup-stdout.log"
        _say(f"  nightly:  FAILED ({stages}) {_age(n['at'])} → {n.get('dest')} — `archive status` has detail")
    elif n:
        _say(f"  nightly:  ok {_age(n['at'])} → {n.get('dest')}")
    if machine.macos:
        if machine.backup_running(args.home):
            dest = machine.backup_dest()
            _say("  schedule: nightly backup + verify + restore-drill installed"
                 + (f" → {dest}" if dest else ""))
        else:
            _say("  schedule: no nightly backup — `thread_archive setup` offers it "
                 "(or `archive daemon install --backup --dest <path>`)")
    _say()
    _say("  search/read: the archive-mcp tools · web viewer: http://127.0.0.1:8787 (with the watcher)")
    _say("  re-run setup: thread_archive setup · operator CLI: archive --help")
    return 0


# ── entry point ──────────────────────────────────────────────────────────────


def _setup_completed(args: argparse.Namespace) -> bool:
    if load_config(args.home).get("setup", {}).get("completed_at"):
        return True
    # No config (pre-setup install, or a hand-built home): a populated archive
    # means someone set this up — don't greet them like a stranger. Gate on the
    # index *file* first: answering "is this set up?" must never scaffold a
    # fresh home as a side effect (api.status opens — and thereby creates —
    # the engine).
    if not resolve_paths(args.home).index_path.exists():
        return False
    try:
        from .. import _api as api

        return api.status(home=args.home)["threads"] > 0
    except Exception:  # noqa: BLE001 — an unreadable home reads as fresh
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thread_archive",
        description="Set up and check the local AI-conversation archive. "
                    "Bare `thread_archive` runs first-time setup, then becomes the status view.",
        epilog="Operator verbs (backup, verify, reindex, …) live on the `archive` command.",
    )
    parser.add_argument("command", nargs="?", choices=["setup", "status"], default=None,
                        help="force setup or status (default: setup on first run, status after)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="accept every default; never prompt (agent/script mode)")
    parser.add_argument("--home", default=None,
                        help="archive home dir (default: $THREAD_ARCHIVE_HOME or ~/.thread/archive)")
    parser.add_argument("--skip-import", action="store_true", help="setup: don't import now")
    parser.add_argument("--skip-watcher", action="store_true", help="setup: don't offer the watcher")
    parser.add_argument("--skip-backup", action="store_true", help="setup: don't offer nightly backup")
    parser.add_argument("--backup-dest", default=None, metavar="PATH",
                        help="setup: schedule nightly backups to PATH without prompting")
    parser.add_argument("--skip-mcp", action="store_true", help="setup: don't offer MCP wiring")
    parser.add_argument("--version", action="version", version=f"thread-archive {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return print_status(args)
    if args.command == "setup":
        return run_setup(args)
    return print_status(args) if _setup_completed(args) else run_setup(args)


if __name__ == "__main__":
    sys.exit(main())
