"""The ``archive`` command — a thin CLI over the :mod:`thread_archive._api` surface.

Two kinds of verb live here.

**Retrieval — ``search`` and ``read`` — is supported surface.** They are the
``thread_search`` / ``thread_read`` tools with a terminal in front of them: one
implementation (:mod:`thread_archive._tools`), served to agents over MCP and to
a person here, so what you get at a prompt is what the agent would have gotten,
notes and all. Their flags mirror the tool parameters one for one.

**Every other verb is private operational tooling.** They are the process seam
the service manager, cron, and operators use to run the private machinery —
ingest (``import``, ``import-export``, ``watch``, ``embed``), the backup kit
(``backup``, ``verify``, ``restore-drill``, ``restore``, ``reindex``,
``migrate``, ``repair``, ``status``, ``nightly``, ``coverage``), and the
service-agent lifecycle (``daemon``). Search quality is not a verb here at all —
the scoring surface is the repo-only ``search_lab/``, which no install carries.
Those verbs
may change without external notice, but they are *wired into* the service
manifests, lab's cron script, the /ci skill, and the monitor's heartbeat
contract — renaming one means updating those in the same change
(``tests/test_public_api.py`` pins the set so the change is deliberate).

The web viewer is the same archive through a browser, cohosted by the always-on
watcher (``thread_archive watch --web``); ``web`` is not a third read surface —
it hands that viewer's URL to a browser and serves nothing itself.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import __version__
from ._config import resolve_paths


def _add_home_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--home",
        default=None,
        help="archive home dir (default: $THREAD_ARCHIVE_HOME or ~/.thread/archive)",
    )


def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse an ``HH:MM`` schedule string into ``(hour, minute)``."""
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise SystemExit(f"thread_archive daemon: --at must be HH:MM (got {s!r})")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise SystemExit(f"thread_archive daemon: --at must be HH:MM (got {s!r})")
    return h, m


def _self_throttle() -> None:
    """Drop this process to background priority so a full rebuild never starves the
    interactive machine. CPU via ``nice``; on macOS also throttle disk I/O (the FTS
    rebuild is I/O-bound) — the in-process equivalent of ``taskpolicy -d throttle``.
    Best-effort: on an idle box it still runs full speed, and a failure to throttle
    must never stop the work. ``THREAD_ARCHIVE_NO_THROTTLE`` (non-empty) skips it —
    a rebuild you want flat-out, or a process the renice must not deprioritize
    (nice() is one-way; the test suite runs with it set)."""
    import os

    if os.environ.get("THREAD_ARCHIVE_NO_THROTTLE"):
        return
    try:
        os.nice(10)
    except OSError:  # pragma: no cover — nice() can be restricted
        pass
    if sys.platform == "darwin":
        try:  # pragma: no cover — darwin best-effort; the subprocess throttle test proves it, and it can't run in-process without renicing the test runner
            import ctypes

            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            # setiopolicy_np(IOPOL_TYPE_DISK=0, IOPOL_SCOPE_PROCESS=1, IOPOL_THROTTLE=5)
            libc.setiopolicy_np(0, 1, 5)
        except Exception:  # pragma: no cover — platform best-effort
            pass


def cmd_search(args: argparse.Namespace) -> int:
    """``thread_archive search`` — the ``thread_search`` tool, rendered to stdout.

    Every flag is passed through unchanged: the clamping, the default scope, the
    degradation notice, and the usage-ledger record are the tool's, so a query
    typed here and the same query asked over MCP return the same text.
    """
    from . import _api as api
    from . import _tools

    api.open_archive(args.home)
    try:
        with _tools.serving("cli"):
            out = _tools.thread_search(
                args.query,
                limit=args.limit,
                thread_id=args.thread_id,
                topic_id=args.topic_id,
                content_type=args.content_type,
                exclude_content_type=args.exclude_content_type,
                since=args.since,
                until=args.until,
                tool_name=args.tool_name,
                source=args.source,
                types=args.types,
                agents=args.agents,
                startswith=args.startswith,
                path=args.path,
                path_ops=args.path_ops,
                commit=args.commit,
                repo=args.repo,
                sort=args.sort,
                group=args.group,
                output=args.output,
                context_lines=args.context_lines,
                context_events=args.context_events,
                rerank=args.rerank,
                match=args.match,
                page=args.page,
            )
    except ValueError as e:
        # The engine rejects an out-of-contract scope by name (--agents, --group,
        # --sort) and its message names the values that do work. Print that rather
        # than restating the set here as argparse choices: a second copy of the
        # valid values is a copy that drifts from the engine's.
        print(f"search: {e}", file=sys.stderr)
        return 2
    print(out)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    """``thread_archive read`` — the ``thread_read`` tool, rendered to stdout."""
    from . import _api as api
    from . import _tools

    api.open_archive(args.home)
    with _tools.serving("cli"):
        print(_tools.thread_read(
            args.id,
            limit=args.limit,
            offset=args.offset,
            summary=args.summary,
            mode=args.mode,
            tool_results=args.tool_results,
            max_chars=args.max_chars,
            after_event=args.after_event,
            around_event=args.around_event,
            context_turns=args.context_turns,
        ))
    # A ref naming nothing readable is an operator error, not a short thread. The
    # rendered text already says so in prose (right for the agent surface, invisible
    # to a shell), so the exit code is decided by resolving the ref rather than by
    # reading that prose back — `read_thread` has more than one way to say no.
    return 0 if _tools._resolve_ref(args.id) is not None else 1


def cmd_import(args: argparse.Namespace) -> int:
    from . import _api as api
    from ._importers import db_scanners, line_stream_importers

    line_streams = line_stream_importers(args.home)
    scanners = db_scanners(args.home)
    provider = args.provider or "claude-code"
    if provider not in line_streams and provider not in scanners:
        known = ", ".join(sorted({**line_streams, **scanners}))
        raise SystemExit(
            f"thread_archive import: unknown provider '{provider}' (known: {known})"
        )

    result = api.import_path(args.path, home=args.home, provider=provider)  # checkpoints internally

    if provider in line_streams:
        summary = (
            f"thread={result.thread_id} events={result.events_created} new={result.is_new_thread}"
        )
    else:  # DB scanner (cursor / opencode): scans many sessions in one file
        summary = " ".join(f"{k}={v}" for k, v in vars(result).items())
    print(f"imported {args.path} ({provider}): {summary}")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    """Every registered provider — the answer to "did my plugin load?"."""
    from ._config import load_config, source_enabled
    from ._providers import registry

    cfg = load_config(args.home)
    rows = []
    for p in registry(cfg).values():
        if p.mechanism and not args.all:
            continue
        traits: list[str] = []
        if p.kind != "none":
            traits.append(p.kind)
        if p.export is not None:
            traits.append(f"export:{p.export.label}")
        if p.watcher is None:
            traits.append("no live store")
        if p.mechanism:
            traits.append("mechanism")
        if p.follows:
            traits.append(f"follows {p.follows}")
        plugin_entry = (cfg.get("providers") or {}).get(p.name)
        if isinstance(plugin_entry, dict) and isinstance(plugin_entry.get("patch"), dict):
            if plugin_entry.get("enabled"):
                pinned = plugin_entry["patch"].get("pinned")
                traits.append("patched (pinned)" if pinned else "patched")
            elif plugin_entry["patch"].get("retired"):
                traits.append("patch retired")
        disabled = not source_enabled(cfg, p.name) or (
            bool(p.follows) and not source_enabled(cfg, p.follows or "")
        )
        rows.append((p.name, p.label, "off" if disabled else "on", ", ".join(traits)))

    width = max((len(r[0]) for r in rows), default=0)
    for name, label, state, notes in rows:
        print(f"{name:<{width}}  {state:<3}  {label}" + (f"  ({notes})" if notes else ""))
    if not args.all:
        print("\n(--all also lists archive's own machinery)")
    return 0


def cmd_import_export(args: argparse.Namespace) -> int:
    from . import _api as api
    from ._importers.exports import import_export
    from ._truth import shared_ingest_lock

    api.open_archive(args.home)
    # Shared across the truth appends AND the SQLite commits (same coverage as
    # api.import_path): an unlocked export import racing a reindex can land in the
    # truth after the rebuild's read point and commit into the inode the swap
    # replaces. Blocks (bounded by one rebuild) — a one-shot import has no retry.
    with shared_ingest_lock():
        result = import_export(args.path, force=args.force)
        api.checkpoint(home=args.home)  # snapshot the new threads' metadata to truth
    print(
        f"imported export {args.path}: processed={result.processed} "
        f"imported={result.imported} skipped={result.skipped} events={result.events_created}"
    )
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Open the cohosted viewer in a browser — the URL, and nothing else.

    The viewer runs inside the always-on watcher process (``watch --web``), so
    there is one read URL over one SQLite engine and this verb only points at it.

    ``web dev`` opens it with the dev pages advertised in the navigation — the
    retrieval report, whose subject is the search pipeline rather than the
    archive. The viewer remembers the choice, so this is a switch rather than a
    different address to keep using; ``web --no-dev`` puts it back.
    """
    import webbrowser

    url = f"http://127.0.0.1:{args.port}"
    if args.mode == "dev":
        url += "/?dev=1"
    elif args.no_dev:
        url += "/?dev=0"
    print(url)
    webbrowser.open(url)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    import logging

    from . import _api as api
    from ._watcher import Watcher

    # Configure logging so the daemon's import/maintenance activity actually lands
    # in the log files (launchd routes stderr to watcher-stderr.log). Without this
    # the long-running process is silent.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api.open_archive(args.home)
    watcher = Watcher(
        home=args.home,
        interval=args.interval,
        embed=args.embed,
        embed_interval=args.embed_interval,
        embed_batch=args.embed_batch,
    )

    if args.once:
        from ._ops.load_runs import load_run
        from ._truth import shared_ingest_lock

        # Same coverage as api.watch(once=True): the poll appends truth and
        # commits, so it must hold the reindex lock shared — an unlocked
        # one-shot racing a reindex can land truth after the rebuild's read
        # point and commit into the inode the swap replaces. Blocking (bounded
        # by one rebuild): a one-shot has no later pass to retry on.
        # The one-shot catch-up is a tracked load — it's the bulk import that makes
        # search usable, so its progress/rate/ETA publish to load-state.json and a
        # summary lands in the ledger, exactly like `embed`.
        quiet = getattr(args, "quiet", False) or not sys.stdout.isatty()
        with load_run("import", home=resolve_paths(args.home).home,
                      reporter=None if quiet else _progress_line) as run:
            with run.phase("import") as ph, shared_ingest_lock():
                result = watcher.poll_once(phase=ph)
                if result.events_created > 0:
                    watcher.maintain()  # one upkeep pass (rebalance/manifest) for the one-shot
        if not quiet:
            sys.stdout.write("\r" + " " * 80 + "\r")
        print(
            f"watch: checked {result.sources_checked} sources, "
            f"imported {result.items_imported} items ({result.events_created} events)",
            flush=True,
        )
        for err in result.errors:
            print(f"  ! {err}", flush=True)
        return 0

    # Optionally cohost the read viewer in this always-on process (one process, one
    # engine) so there's a persistent URL — WAL lets the web reader run concurrent
    # with the watcher's writes (see store._base).
    httpd = None
    if args.web:
        from ._retrieval import start_warm_models
        from ._web import serve_in_thread

        httpd = serve_in_thread(host=args.web_host, port=args.web_port)
        logging.getLogger("thread_archive._watcher").info(
            "cohosting web viewer on http://%s:%s", args.web_host, args.web_port
        )
        # Warm the model stack for the viewer's searches, exactly as the shared MCP
        # server does for its clients. Nobody typing into the search box knows a model
        # is loading, so the tens-of-seconds cold load reads as a broken product on the
        # first search anyone ever runs — and every search after it is sub-second, so
        # the archive makes its worst impression on the one query that forms it. Only
        # the cohosting process warms: the watcher's own indexing loads the embedder
        # when it has work, and a headless watcher answers no queries.
        start_warm_models()

    available = [w.source_name for w in watcher.available()]
    logging.getLogger("thread_archive._watcher").info(
        "watching %d sources %s every %ss (maintenance every %.0fs)",
        len(available), available, args.interval, watcher.maintenance_interval,
    )
    try:
        # The cohosted status endpoint runs before a new daemon can finish its
        # first potentially long provider sweep. Mark this process as the
        # persistent capture owner so the trust center can distinguish
        # "actively catching up" from a dead watcher whose completed-pass
        # record merely looks recent.
        api._set_watch_process_active(True)
        watcher.run()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
    finally:
        api._set_watch_process_active(False)
        if httpd is not None:
            # shutdown() must precede server_close(): closing the socket alone
            # doesn't wake the serve_forever poller on Linux, which keeps the
            # kernel-side socket alive — and accepting — for the rest of the
            # poll window.
            httpd.shutdown()
            httpd.server_close()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from . import _service

    if args.mcp:
        # The shared MCP server: one always-on streamable-HTTP server all clients
        # connect to (point each client's MCP config at the URL below), instead of
        # a per-client stdio subprocess each loading its own retrieval model.
        if args.action == "install":
            path = _service.install_mcp(
                args.home,
                host=args.http_host,
                port=args.http_port,
                ingest=args.mcp_ingest,
            )
            print(f"installed {_service.label('mcp')} ({path})")
            print(f"shared MCP server: http://{args.http_host}:{args.http_port}/mcp")
            print(
                "catch-up ingest: "
                + ("enabled (explicit --mcp-ingest opt-in)" if args.mcp_ingest else "disabled")
            )
            print("point every client's MCP config at that URL "
                  '(type "http") instead of the archive-mcp stdio command.')
        elif args.action == "uninstall":
            _service.uninstall_mcp()
            print(f"uninstalled {_service.label('mcp')}")
        elif args.action == "restart":
            _service.restart_mcp()
            print(f"restarted {_service.label('mcp')}")
        else:  # status
            print(_service.mcp_status())
        return 0

    if args.backup:
        # The scheduled backup pipeline (backup → verify → restore drill) as a
        # scheduled service — the productized form of what host/ wires by hand.
        # install needs --dest (a directory the scheduler can reach unattended).
        if args.action == "install":
            if not args.dest:
                print(
                    "thread_archive daemon install --backup needs --dest <path>",
                    file=sys.stderr,
                )
                return 2
            hour, minute = _parse_hhmm(args.at or "04:00")
            path = _service.install_backup(
                args.dest, args.home, hour=hour, minute=minute,
                notify_url=args.notify_url,
            )
            print(f"installed {_service.label('backup')} ({path})")
            print(
                f"nightly at {hour:02d}:{minute:02d} → {args.dest}: "
                "backup, verify, restore drill."
            )
        elif args.action == "uninstall":
            _service.uninstall_backup()
            print(f"uninstalled {_service.label('backup')}")
        elif args.action == "restart":
            _service.restart_backup()
            print(f"restarted {_service.label('backup')}")
        else:  # status
            print(_service.backup_status())
        return 0

    if args.action == "install":
        path = _service.install_watcher(args.home, web=args.web, web_port=args.web_port)
        print(f"installed {_service.label('watcher')} ({path})")
        print("the watcher is always-on (starts at login); `thread_archive daemon status` to check,")
        print("`thread_archive daemon restart` to apply a code edit.")
        if args.web:
            print(f"web viewer: http://127.0.0.1:{args.web_port}")
    elif args.action == "uninstall":
        _service.uninstall_watcher()
        print(f"uninstalled {_service.label('watcher')}")
    elif args.action == "restart":
        _service.restart_watcher()
        print(f"restarted {_service.label('watcher')}")
    else:  # status
        print(_service.watcher_status())
    return 0


def cmd_fix_import(args: argparse.Namespace) -> int:
    import logging

    from . import _repair

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    try:
        if args.pin or args.unpin:
            _repair.set_pinned(args.provider, bool(args.pin), args.home)
            return 0
        if args.activate:
            summary = _repair.activate(
                args.provider, args.home, reimport=not args.no_reimport
            )
            re = summary.get("reimport") or {}
            if re:
                print(
                    f"re-import: {re['watermarks_reset']} watermark(s) reset, "
                    f"{re['poll_events']} event(s) from the live store, "
                    f"{re['snapshot_events']} from quarantine snapshots"
                )
            print("patch active")
            return 0
        target = _repair.scaffold(args.provider, args.home)
    except (_repair.ActivationError, ValueError) as e:
        print(e)
        return 1
    print(target)
    print(
        f"read {target}/PROTOCOL.md, write the fix, then "
        f"`thread_archive fix-import {args.provider} --activate`"
    )
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    from . import _api as api

    _self_throttle()  # a rebuild is background work — don't bog the interactive machine
    paths = resolve_paths(args.home)
    print(f"reindexing {paths.index_path} from {paths.truth_dir} (vectors={args.vectors}, throttled)")
    try:
        counts = api.reindex(home=args.home, vectors=args.vectors, salvage=args.salvage)
    except RuntimeError as e:
        # A refused publication (corrupt truth lines, failed quick_check, no disk
        # room) — operator guidance, not a stack trace. The old index is intact.
        print(f"reindex refused: {e}", file=sys.stderr)
        return 1
    for name, n in counts.items():
        print(f"  {name:16} {n:>9}")
    print("done")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from . import _api as api

    _self_throttle()
    try:
        result = api.migrate(home=args.home, dry_run=args.dry_run)
    except (OSError, RuntimeError, ValueError) as e:
        print(f"migration failed: {e}", file=sys.stderr)
        return 1
    if result.get("changed"):
        print(
            f"migration complete: truth format v{result['version']}, "
            f"threads={result['threads']} events={result['events']}"
        )
    elif result.get("dry_run"):
        print("migration dry run complete; truth was not changed")
    return 0


def _fmt_duration(seconds: Optional[float]) -> str:
    """``4h12m`` / ``7m30s`` / ``12s`` — a wait stated in the units a person waits in."""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def _progress_line(snap: dict) -> None:
    """Rewrite one console line with the run's current phase, progress, and ETA."""
    phases = snap.get("phases") or []
    if not phases:
        return
    ph = phases[-1]
    done, total = ph.get("done", 0), ph.get("total")
    bits = [f"{ph.get('name', '?')}: {done:,}" + (f"/{total:,}" if total else "")]
    if total:
        bits.append(f"{100.0 * done / max(total, 1):.1f}%")
    if (rate := ph.get("rate_per_s")):
        bits.append(f"{rate:,.1f}/s")
    if (eta := ph.get("eta_s")) is not None:
        bits.append(f"ETA {_fmt_duration(eta)}")
    sys.stdout.write("\r  " + "  ".join(bits).ljust(78))
    sys.stdout.flush()


def cmd_embed(args: argparse.Namespace) -> int:
    from . import _api as api

    print("embedding missing vectors (rebuild=%s)..." % args.rebuild, flush=True)
    quiet = getattr(args, "quiet", False) or not sys.stdout.isatty()
    res = api.embed(home=args.home, rebuild=args.rebuild, max_events=args.limit,
                    newest_first=args.newest_first,
                    progress=None if quiet else _progress_line)
    if not quiet:
        sys.stdout.write("\r" + " " * 80 + "\r")
    print(f"  embedded {res['embedded']}")
    return 0


def cmd_loads(args: argparse.Namespace) -> int:
    """Show how loading this archive is going, and how it has gone."""
    from . import _api as api

    res = api.load_status(home=args.home, limit=args.limit)
    cur = res.get("current") or {}
    if cur:
        status = cur.get("status", "?")
        print(f"current: {cur.get('kind', '?')} — {status} "
              f"({_fmt_duration(cur.get('elapsed_s'))} elapsed, pid {cur.get('pid', '?')})")
        for ph in cur.get("phases") or []:
            print("  " + _phase_line(ph))
        if status == "stalled":
            print("  (the process writing this state is gone — the load died mid-phase)")
    else:
        print("current: no load in flight")
    runs = res.get("recent") or []
    if not runs:
        print("no recorded runs")
        return 0
    print(f"\nrecent runs ({len(runs)}):")
    for r in runs:
        print(f"  {r.get('at', '?')[:19]}  {r.get('kind', '?'):<8} {r.get('status', '?'):<7} "
              f"{_fmt_duration(r.get('duration_s'))}")
        for ph in r.get("phases") or []:
            print("      " + _phase_line(ph))
    return 0


def _phase_line(ph: dict) -> str:
    """One phase as a line: its wall time, what it moved, and its internal split."""
    bits = [f"{ph.get('name', '?'):<14} {_fmt_duration(ph.get('elapsed_s')):>7}"]
    if (done := ph.get("done")):
        bits.append(f"{done:,}" + (f"/{ph['total']:,}" if ph.get("total") else "") + " done")
    if (rate := ph.get("rate_per_s")):
        bits.append(f"{rate:,.1f}/s")
    # Only worth printing once it means something: a phase whose tail runs
    # materially slower than its head is paying a cost that grows with its own
    # output, which the mean rate beside it hides.
    if (slow := ph.get("slowdown")) and slow >= 1.5:
        unit = f" {u}" if (u := ph.get("trend_unit")) else ""
        bits.append(f"slowing {slow:,.1f}x ({ph.get('rate_first_s')}{unit}/s -> "
                    f"{ph.get('rate_last_s')}{unit}/s)")
    if (detail := ph.get("detail_s")):
        bits.append("[" + " ".join(f"{k} {_fmt_duration(v)}" for k, v in detail.items()) + "]")
    if (counts := ph.get("counts")):
        bits.append("(" + " ".join(f"{k}={v:,}" for k, v in counts.items()) + ")")
    return "  ".join(bits)


def cmd_backup(args: argparse.Namespace) -> int:
    from . import _api as api

    return report_backup(api.backup(
        args.dest, home=args.home,
        allow_shrink=args.allow_shrink, verify_first=args.verify,
    ))


def report_backup(res: dict) -> int:
    """Print the operator report for an ``_api.backup`` result; return its exit code."""
    mb = res["bytes_copied"] / (1024 * 1024)
    print(f"backed up {res['truth_dir']} → {res['dest']}: {res['files_copied']} files ({mb:.1f} MB copied)")
    if res.get("bundle_error"):
        print(
            f"WARNING: recovery bundle sync failed ({res['bundle_error']}) — the "
            "destination's .recovery/ (config, retained exports) is stale"
        )
    elif "bundle_files" in res:
        print(
            f"recovery bundle: {res['bundle_files']} file(s) "
            f"({res['bundle_copied']} copied, {res['bundle_deleted']} removed)"
        )
    if res.get("generation_created"):
        print(
            f"generation: pre-run state preserved as .generations/{res['generation_created']} "
            f"({res.get('generations_kept', '?')} kept, {res.get('generations_pruned', 0)} pruned)"
        )
    if res.get("generation_error"):
        print(f"WARNING: generation snapshot failed ({res['generation_error']}) — "
              "this run had no pre-overwrite recovery margin")
    if not res["verify_ok"]:
        print(
            "WARNING: pre-backup verify FAILED — the source truth has integrity "
            "problems; mirror ran additively (no deletions). Run `thread_archive verify`."
        )
    if res.get("rehomed_twins_deleted"):
        print(
            f"rebalance twins: {res['rehomed_twins_deleted']} superseded old-layout "
            "copies removed from the backup"
        )
    if res.get("renamed_twins_deleted"):
        print(
            f"migration twins: {res['renamed_twins_deleted']} superseded legacy-id "
            "copies removed from the backup"
        )
    if res["deletions_skipped"]:
        print(
            f"WARNING: {res['deletions_skipped']} stale destination files kept — "
            "planned deletions exceeded the safety bound (gutted source?); mirror ran additively"
        )
    if res["shrinks_skipped"]:
        print(
            f"SHRINK GUARD: {res['shrinks_skipped']} append-only truth files are SMALLER at "
            f"the source than in the backup — the source lost data; their backup copies were "
            f"kept. Sample: {res['shrink_sample']}. Investigate before rerunning; "
            "--allow-shrink overrides after a deliberate truth re-emit."
        )
    if not res["mirror_complete"]:
        print(
            f"MIRROR INCOMPLETE: {res['dest_missing_files']} source files missing at dest, "
            f"{res['dest_divergent_files']} divergent"
        )
        return 1
    # Skipped deletions fail the run too: the condition is either a gutted source
    # (page-worthy) or a persistently additive backup accumulating stale records —
    # both need eyes, and the scheduled wrapper only notifies on a nonzero exit.
    # So does a failed bundle sync: a mirror whose .recovery/ has quietly stopped
    # updating restores yesterday's config and retained exports.
    return (
        0
        if res["verify_ok"] and not res["deletions_skipped"] and not res.get("bundle_error")
        else 1
    )


def cmd_verify(args: argparse.Namespace) -> int:
    from . import _api as api

    return report_verify(
        api.verify(home=args.home, deep=args.deep, hashes=args.hashes, backup=args.backup),
        deep=args.deep, hashes=args.hashes, backup=args.backup,
    )


def report_verify(
    res: dict, *, deep: bool = False, hashes: bool = False, backup: str | None = None
) -> int:
    """Print the operator report for an ``_api.verify`` result; return its exit code.

    The three flags say which tiers were asked for, and so which of the result's
    optional sections the report walks."""
    t = res["truth"]
    print(
        f"truth: threads={t['threads']} events={t['events']} "
        f"effective={t['events_effective']} "
        f"superseded={t['duplicate_id_lines'] + t['duplicate_content_lines']} "
        f"parse_errors={t['parse_errors']}"
    )
    if t["parse_errors"]:
        print(
            f"       torn tails={t['parse_errors_torn_tail']} "
            f"interior={t['parse_errors_interior']} — `thread_archive repair` quarantines "
            "these and restores any committed content they shadow"
        )
        print(f"       parse error sample: {t['parse_error_sample']}")
    print(
        f"index: threads={res['index']['threads']} events={res['index']['events']} "
        f"kg_events={res['index']['kg_events']} "
        f"{res['index'].get('check', 'quick_check')}={res['index']['quick_check']}"
    )
    print(
        f"drift: threads={res['drift']['threads']:+d} events={res['drift']['events']:+d} "
        f"kg_events={res['drift']['kg_events']:+d}"
    )
    fts = res["fts"]
    if fts["shadow_rows"] != fts["fts5_rows"] or fts["orphan_rows"]:
        print(
            f"fts:   shadow={fts['shadow_rows']} fts5={fts['fts5_rows']} "
            f"orphans={fts['orphan_rows']} — `thread_archive reindex` rebuilds the search surface"
        )
    if deep:
        dp = res["deep"]
        print(
            f"deep:  index_only={dp['events_index_only']} "
            f"missing={dp['events_missing_from_index']} "
            f"key_mismatch={dp['events_key_mismatch']} "
            f"superseded_twins={dp['events_superseded_twins']} "
            f"(watermark {dp['watermark']})"
        )
        if dp["thread_meta_mismatch"]:
            print(
                f"       thread metadata drift (report-only): "
                f"{dp['thread_meta_mismatch']} threads, sample {dp['thread_meta_sample']}"
            )
        print(
            f"       kg index_only={dp['kg']['index_only']} truth_only={dp['kg']['truth_only']} "
            f"content_mismatch={dp['kg']['content_mismatch']}; "
            f"dangling links={dp['dangling']['link_endpoints']} "
            f"citations={dp['dangling']['citation_events']} "
            f"citation_thread_mismatch={dp['dangling']['citation_thread_mismatch']} "
            f"events={dp['dangling']['event_threads']}; "
            f"dup_pairs={dp['duplicate_content_pairs_index']}"
        )
        f = dp["fts"]
        print(
            f"       fts orphans={f['orphan_rows']} "
            f"shadow={f['shadow_rows']} fts5={f['fts5_rows']} "
            f"unindexed={f['unindexed_events']} "
            f"empty_extract={f['empty_extract_events']}"
        )
        if f["unindexed_events"]:
            print(f"       unindexed sample: {f['unindexed_sample']}")
        if dp["events_missing_from_index"]:
            print(f"       missing sample: {dp['missing_sample']}")
        if dp["events_index_only"]:
            print(f"       index-only sample: {dp['index_only_sample']}")
        if dp["events_key_mismatch"]:
            print(f"       key-mismatch sample: {dp['key_mismatch_sample']}")
    if hashes:
        h = res["hashes"]
        for side in ("truth", "index"):
            hs = h[side]
            print(
                f"hashes[{side}]: checked={hs['checked']} mismatched={hs['mismatched']} "
                f"unhashed_keys={hs['unhashed_keys']} no_key={hs['no_key']}"
            )
            if hs["mismatched"]:
                print(f"       mismatch sample: {hs['mismatch_sample']}")
        c = h["cross"]
        print(f"hashes[cross]: compared={c['compared']} mismatched={c['mismatched']}")
        if c["mismatched"]:
            print(f"       mismatch sample: {c['mismatch_sample']}")
        if "delta" in h:
            d = h["delta"]
            print(
                f"hashes delta vs {h['previous']['at']}: "
                f"truth {d['truth_mismatched']:+d} index {d['index_mismatched']:+d} "
                f"cross {d['cross_mismatched']:+d}"
            )
    if backup:
        b = res["backup"]
        if "error" in b:
            print(f"backup[{b['dest']}]: {b['error']}")
        else:
            bs = b["scan"]
            print(
                f"backup[{b['dest']}]: threads={bs['threads']} "
                f"effective={bs['events_effective']} parse_errors={bs['parse_errors']} "
                f"coverage={b['coverage']:.4f}"
            )
            if "effective_drop" in b:
                ed = b["effective_drop"]
                print(
                    f"       MIRROR SHRANK: {ed['previous']} → {ed['current']} effective "
                    f"events since {ed['previous_at']} — the backup lost content "
                    "between looks; check the mirror before the next run overwrites it"
                )
            if "hashes" in b:
                bh = b["hashes"]
                print(
                    f"backup hashes: checked={bh['checked']} mismatched={bh['mismatched']} "
                    f"unhashed_keys={bh['unhashed_keys']} no_key={bh['no_key']}"
                )
                if bh["mismatched"]:
                    print(f"       mismatch sample: {bh['mismatch_sample']}")
    if res["ok"]:
        print("OK")
    else:
        print(f"FAILED: {', '.join(res['failed_components'])}")
        if res.get("failure_log"):
            print(f"       full result appended to {res['failure_log']}")
    return 0 if res["ok"] else 1


def cmd_restore_drill(args: argparse.Namespace) -> int:
    from . import _api as api

    print(f"restore drill: rebuilding an index from {args.dest} in a throwaway home...", flush=True)
    return report_restore_drill(
        api.restore_drill(args.dest, home=args.home, keep_home=args.keep_home)
    )


def report_restore_drill(res: dict) -> int:
    """Print the operator report for an ``_api.restore_drill`` result; return its exit code."""
    if "error" in res:
        print(f"FAILED: {res['error']}")
    if "mirror" in res:
        m = res["mirror"]
        print(
            f"mirror: threads={m['threads']} effective={m['events_effective']} "
            f"parse_errors={m['parse_errors']}"
        )
    if "rebuilt" in res:
        r = res["rebuilt"]
        print(
            f"rebuilt: threads={r['threads']} events={r['events']} fts={r.get('fts')} "
            f"coverage={res.get('coverage', 0):.4f} of live"
        )
    if "smoke" in res:
        sm = res["smoke"]
        if sm.get("skipped"):
            print(f"smoke:  skipped ({sm['skipped']})")
        else:
            print(
                f"smoke:  read={'ok' if sm.get('read_ok') else 'FAILED'} "
                f"search={'ok' if sm.get('search_ok') else 'FAILED'}"
                + (f" (token {sm['token']!r})" if sm.get("token") else "")
                + (f" error: {sm['error']}" if sm.get("error") else "")
            )
    b = res.get("bundle")
    if b:
        if not b["present"]:
            print("bundle: ABSENT — this mirror restores conversations only (no config/exports)")
        else:
            print(
                f"bundle: config={'yes' if b['config'] else 'no'} "
                f"retained exports={b['retained_exports']}"
            )
    if res.get("drill_home"):
        print(f"drill home kept: {res['drill_home']}")
    print(f"{'OK' if res.get('ok') else 'RESTORE DRILL FAILED'} ({res.get('seconds', '?')}s)")
    return 0 if res.get("ok") else 1


def cmd_restore(args: argparse.Namespace) -> int:
    from . import _api as api

    if args.list_generations:
        gens = api.list_generations(args.dest)
        if not gens:
            print("no generations retained at this mirror (the head is the only restore point)")
        for g in gens:
            print(g)
        return 0
    if not args.to:
        print("restore: --to <home> is required (or --list-generations)")
        return 2
    src = f"{args.dest} (generation {args.generation})" if args.generation else args.dest
    print(f"restore: rebuilding {args.to} from {src}...", flush=True)
    return report_restore(api.restore(
        args.dest, args.to, generation=args.generation,
        replace=args.replace, allow_parse_errors=args.allow_parse_errors,
    ), to=args.to)


def report_restore(res: dict, *, to: str) -> int:
    """Print the operator report for an ``_api.restore`` result; return its exit code.

    ``to`` is the home that was restored into — the result describes the rebuild,
    not where it landed."""
    if "mirror" in res:
        m = res["mirror"]
        print(
            f"mirror: threads={m['threads']} effective={m['events_effective']} "
            f"parse_errors={m['parse_errors']}"
        )
    if "rebuilt" in res:
        r = res["rebuilt"]
        print(f"rebuilt: threads={r['threads']} events={r['events']} fts={r.get('fts')}")
    sm = res.get("smoke")
    if sm:
        if sm.get("skipped"):
            print(f"smoke:  skipped ({sm['skipped']})")
        else:
            print(
                f"smoke:  read={'ok' if sm.get('read_ok') else 'FAILED'} "
                f"search={'ok' if sm.get('search_ok') else 'FAILED'}"
                + (f" error: {sm['error']}" if sm.get("error") else "")
            )
    b = res.get("bundle")
    if b:
        installed = ["config"] if b.get("config") else []
        if b.get("retained_exports"):
            installed.append(f"{b['retained_exports']} retained export(s)")
        print(
            "bundle: installed " + (", ".join(installed) if installed else "nothing")
            + (f" (ERROR: {b['error']})" if b.get("error") else "")
        )
    if res.get("damaged_home"):
        print(f"previous home set aside (preserved): {res['damaged_home']}")
    if res.get("error"):
        print(f"FAILED: {res['error']}")
    print(f"{'OK — restored to ' + str(to) if res.get('ok') else 'RESTORE FAILED'} "
          f"({res.get('seconds', '?')}s)")
    return 0 if res.get("ok") else 1


def cmd_nightly(args: argparse.Namespace) -> int:
    from . import _api as api

    print(f"nightly pipeline: backup → verify → restore drill ({args.dest})", flush=True)
    return report_nightly(api.nightly(
        args.dest, home=args.home, notify_url=args.notify_url,
        allow_shrink=args.allow_shrink, drill=args.drill,
    ))


def report_nightly(res: dict) -> int:
    """Print the operator report for an ``_api.nightly`` result; return its exit code."""
    b = res["backup"]
    if "error" in b:
        print(f"backup: ERROR {b['error']}")
    else:
        mb = b["bytes_copied"] / (1024 * 1024)
        print(f"backup: {b['files_copied']} files ({mb:.1f} MB copied)")
    esc = res["escalations"]
    v = res["verify"]
    flags = "+".join(k for k in ("deep", "hashes") if esc[k]) or "shallow"
    if "error" in v:
        print(f"verify [{flags}]: ERROR {v['error']}")
    elif v["ok"]:
        print(f"verify [{flags}]: ok "
              f"drift={v['drift']['events']:+d} parse_errors={v['truth']['parse_errors']}")
    else:
        print(f"verify [{flags}]: FAILED ({', '.join(v['failed_components'])}) "
              f"drift={v['drift']['events']:+d} parse_errors={v['truth']['parse_errors']}")
        if v.get("failure_log"):
            print(f"       full result appended to {v['failure_log']}")
    if "drill" in res:
        d = res["drill"]
        if "error" in d:
            print(f"restore drill: ERROR {d['error']}")
        else:
            print(f"restore drill: {'ok' if d.get('ok') else 'FAILED'} "
                  f"coverage={d.get('coverage', 0):.4f} ({d.get('seconds', '?')}s)")
    if res.get("notify_error"):
        print(f"notify: could not deliver failure notification ({res['notify_error']})")
    print("NIGHTLY OK" if res["ok"]
          else f"NIGHTLY FAILED: {', '.join(res['failed_stages'])}")
    return 0 if res["ok"] else 1


def cmd_repair(args: argparse.Namespace) -> int:
    from . import _api as api

    return report_repair(api.repair(home=args.home, dry_run=args.dry_run))


def report_repair(res: dict) -> int:
    """Print the operator report for an ``_api.repair`` result; return its exit code."""
    verb = "would quarantine" if res["dry_run"] else "quarantined"
    print(
        f"{verb} {res['fragments_quarantined']} unparseable line(s) "
        f"across {res['files_damaged']} file(s)"
    )
    if res.get("damaged_sample"):
        print(f"       sample: {res['damaged_sample']}")
    if res.get("quarantine_file"):
        print(f"       ledger: {res['quarantine_file']}")
    verb = "would restore" if res["dry_run"] else "restored"
    print(
        f"{verb} from index: {res['events_restored_from_index']} event(s), "
        f"{res['kg_events_restored']} kg event(s), "
        f"{res['thread_records_restored']} thread record(s)"
    )
    if not res["dry_run"] and res["fragments_quarantined"]:
        print("note: the repaired files shrank — the next `thread_archive backup` may need --allow-shrink")
    if not res["dry_run"]:
        print("run `thread_archive verify` to confirm the archive is clean")
    return 0


def _age(iso: str) -> str:
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return f"{hours / 24:.1f}d ago" if hours >= 48 else f"{hours:.1f}h ago"


def cmd_status(args: argparse.Namespace) -> int:
    from . import _api as api

    st = api.status(home=args.home)
    # Merged in rather than folded into `status` itself: the walk is this
    # command's to pay for, not the viewer's on every poll of the same API.
    st["disk"] = api.disk_usage(home=args.home)
    return report_status(st)


def _report_libraries(rows: list[dict]) -> None:
    """One ``libs:`` line for the capability matrix, plus a loud line per degraded
    entry. A library this install has no use for rides the summary line and nothing
    more — ``off`` is a shape of the product, not a fault. ``degraded`` means a feature
    is running on a lesser substitute, which nothing else would show, so it gets its own
    line with the remedy on it."""
    if not rows:
        return
    summary = ", ".join(f"{r['name'].split(' ')[0]} {r['state']}" for r in rows)
    print(f"libs:    {summary}")
    for row in rows:
        if row["state"] == "degraded":
            print(f"         DEGRADED — {row['capability'].lower()}: {row['detail']}")


def _report_disk(d: dict) -> None:
    """The ``disk:`` block: what the home costs, split by what a reader can do
    about each part.

    The split line is the whole point — a bare total says an archive is large
    without saying whether that is the conversations or the projection over them.
    The follow-up names the biggest entries that are *neither* truth nor index,
    because those two are already accounted for by name above it and everything
    else is an unlabelled pile that grows quietly: migration payloads, drift
    quarantine, bench caches, logs.
    """
    from ._ops.disk import format_bytes as fmt

    kinds = d.get("kinds") or {}
    split = " · ".join((
        f"truth {fmt(kinds.get('truth'))}",
        f"index {fmt(kinds.get('index'))} (rebuildable)",
        f"sources {fmt(kinds.get('sources'))}",
        f"other {fmt(kinds.get('other'))}",
    ))
    print(f"disk:    {fmt(d.get('total_bytes'))} in {d.get('files', 0):,} files — {split}")
    rest = [e for e in d.get("entries") or [] if e["kind"] not in ("truth", "index")][:3]
    if rest:
        print("         largest: " + " · ".join(f"{e['name']} {fmt(e['bytes'])}" for e in rest))
    for path in d.get("external") or []:
        print(f"         (outside the home: {path})")


def report_status(st: dict) -> int:
    """Print the operator report for an ``_api.status`` result; return its exit code."""
    print(f"home:    {st['home']}")
    print(f"truth:   {st['truth_dir']}")
    print(f"index:   {st['index_path']}")
    print(f"threads: {st['threads']}")
    print(f"events:  {st['events']}")
    print(f"indexed: {st['fts_indexed']}")
    _report_libraries(st.get("libraries") or [])
    # The code axis. The pending count is the fold's trailing edge — normally the
    # handful of events that landed since the last maintenance pass, and a large
    # number only while an existing archive's history is still being walked.
    pending = st.get("code_pending", 0)
    print(f"code:    {st.get('code_files', 0)} files, {st.get('code_commits', 0)} "
          f"commits, {st.get('code_paths_indexed', 0)} touches"
          + (f" ({pending} events pending)" if pending else ""))
    if st.get("disk"):
        _report_disk(st["disk"])
    v, b = st.get("last_verify"), st.get("last_backup")
    d = st.get("last_restore_drill")
    if v and v["ok"]:
        print(f"verify:  ok {v['at']} ({_age(v['at'])})")
    elif v:
        components = ", ".join(v.get("failed", [])) or "see verify-failures.jsonl"
        print(f"verify:  FAILED ({components}) {v['at']} ({_age(v['at'])})")
    else:
        print("verify:  never recorded")
    print(
        f"backup:  {'ok' if b['ok'] else 'FAILED'} → {b['dest']} {b['at']} ({_age(b['at'])})"
        if b else "backup:  never recorded"
    )
    print(
        f"drill:   {'ok' if d['ok'] else 'FAILED'} coverage={d.get('coverage')} "
        f"{d['at']} ({_age(d['at'])})"
        if d else "drill:   never recorded"
    )
    n = st.get("last_nightly")
    if n:
        # The nightly is the scheduled protection pipeline (opt-in; only ever
        # printed once one has run). Its verdict is the headline: a green
        # ad-hoc backup line above must not mask a red pipeline.
        if n.get("ok"):
            print(f"nightly: ok → {n.get('dest')} {n['at']} ({_age(n['at'])})")
        else:
            stages = ", ".join(n.get("failed_stages", [])) or "see logs/backup-stdout.log"
            print(f"nightly: FAILED ({stages}) → {n.get('dest')} {n['at']} ({_age(n['at'])})")
    m = st.get("last_source_mirror")
    if m:
        state = "ok" if m.get("ok") else "FAILED"
        detail = f"{m.get('copied', '?')} copied / {m.get('files', '?')} files"
        if m.get("errors"):
            detail += f", {m['errors']} error(s)"
        print(f"source mirror: {state} ({detail}) {m['at']} ({_age(m['at'])})")
    c = st.get("last_coverage")
    if c and c["ok"]:
        # Warnings (stale exports, never-ingested stores) are capture holes in the
        # making — a green check must not swallow them.
        warns = c.get("warnings") or []
        qualifier = f", {len(warns)} warning(s)" if warns else ""
        print(
            f"coverage: ok ({c.get('sources_checked', '?')} sources{qualifier}) "
            f"{c['at']} ({_age(c['at'])})"
        )
        for msg in warns[:3]:
            print(f"         {msg}")
    elif c:
        print(f"coverage: FAILED {c['at']} ({_age(c['at'])})")
        for msg in (c.get("failed") or [])[:3]:
            print(f"         {msg}")
    else:
        print("coverage: never recorded")
    p = st.get("last_watch_pass")
    if p:
        srcs = p.get("sources") or {}
        events = sum(t.get("events", 0) for t in srcs.values())
        parse_errors = sum(t.get("parse_errors", 0) for t in srcs.values())
        line = (
            f"ingest:  last pass {p['at']} ({_age(p['at'])}), "
            f"{events} events since pass-owner start"
        )
        if parse_errors:
            line += f", {parse_errors} PARSE ERRORS (see capture-skips.jsonl + logs)"
        print(line)
    else:
        print("ingest:  no pass recorded")
    u = st.get("last_self_update")
    if u:
        # Only ever printed once the operator has run a release check; nothing
        # checks on its own. The line names an available release so the next
        # `thread_archive self-update` is an informed choice.
        action = u.get("action", "?")
        if action == "updated":
            print(f"update:  {u.get('reason')} {u['at']} ({_age(u['at'])})")
        elif action == "update":
            print(
                f"update:  {u.get('tag')} available — run `thread_archive self-update` "
                f"to apply; checked {u['at']} ({_age(u['at'])})"
            )
        elif u.get("ok"):
            print(f"update:  {action} (v{u.get('current')}) checked {u['at']} ({_age(u['at'])})")
        else:
            print(f"update:  {action.upper()}: {u.get('reason')} {u['at']} ({_age(u['at'])})")
    w = st.get("last_watch_errors")
    if w:
        print(
            f"watch:   poll errors seen, last at {w['at']} ({_age(w['at'])}), "
            f"{w.get('count_since_start', '?')} since daemon start"
        )
        for err in w.get("errors", [])[:3]:
            print(f"         {err}")
    _report_silenced(st)
    return 0


def _report_silenced(st: dict) -> None:
    """Name what the health page is holding back, if anything.

    A silence made in the viewer would otherwise be invisible here, and a
    terminal that quietly omits a warning someone put aside is exactly the kind
    of half-truth this report exists to avoid. Fail-soft: the silence store is a
    convenience over the records printed above, so an unreadable one costs this
    line and nothing else."""
    try:
        from ._ops.notices import notice_board

        silenced = notice_board(st)["silenced"]
    except Exception:  # noqa: BLE001 — a status report must always print
        return
    if not silenced:
        return
    print(f"silenced: {len(silenced)} notice(s) held aside on the health page")
    for notice in silenced[:3]:
        print(f"         {notice['title']}")


def cmd_self_update(args: argparse.Namespace) -> int:
    from . import _update

    return report_self_update(_update.self_update(
        home=args.home, check_only=args.check,
        allow_format_bump=args.allow_format_bump,
    ))


def report_self_update(res: dict) -> int:
    """Print the operator report for an ``_update.self_update`` result; return its exit code."""
    action = res.get("action")
    if action == "updated":
        print(f"self-update: {res['reason']}")
    elif action == "update":  # --check found one
        print(f"self-update: {res['tag']} available ({res['reason']}) — "
              "run `thread_archive self-update` to apply")
    elif action == "up-to-date":
        print(f"self-update: up to date (v{res['current']}) — {res['reason']}")
    else:
        print(f"self-update: {action.upper() if action else '?'}: {res.get('reason')}")
    return 0 if res.get("ok") else 1


def cmd_coverage(args: argparse.Namespace) -> int:
    from . import _api as api

    return report_coverage(api.check_coverage(home=args.home))


def report_coverage(r: dict) -> int:
    """Print the operator report for an ``_api.check_coverage`` result; return its exit code."""
    for name, s in sorted(r["sources"].items()):
        state = s.get("failed") or s.get("warning") or "ok"
        print(
            f"{name:<16} {state:<16} store_latest={s['store_latest'] or '-'} "
            f"newest_event={s['newest_event_at'] or '-'} history={s['history']}"
        )
    for name, s in sorted(r["disabled"].items()):
        print(f"{name:<16} {'disabled':<16} history={s['history']}")
    for name, s in sorted(r["unwatched"].items()):
        state = s.get("warning") or "unwatched"
        print(f"{name:<16} {state:<16} newest_event={s['newest_event_at'] or '-'}")
    sk = r["skips"]
    if sk["total"]:
        print(
            f"skips: {sk['total']} ledger records, {sk['recent']} in last "
            f"{sk['days']:.0f}d ({sk['recent_lines']} lines) — capture-skips.jsonl"
        )
    dr = r["drift"]
    if dr["total"]:
        print(
            f"validation drift: {dr['total']} ledger records, {dr['recent']} in last "
            f"{dr['days']:.0f}d ({dr['recent_findings']} findings) — validation-drift.jsonl"
        )
    for name, v in sorted((r.get("degraded") or {}).items()):
        since = f" since {str(v.get('since'))[:10]}" if v.get("since") else ""
        print(
            f"degraded: {name} ({v.get('reason')}{since}) — "
            f"remedy: thread_archive fix-import {name}"
        )
    for name, gen in sorted((r.get("drift_snapshots") or {}).items()):
        print(f"quarantined: {name} raw store snapshot → {gen}")
    for msg in r["warnings"]:
        print(f"warning: {msg}")
    if r["ok"]:
        print("OK")
    else:
        print("FAILED:")
        for msg in r["failed"]:
            print(f"  {msg}")
    return 0 if r["ok"] else 1


def cmd_mirror(args: argparse.Namespace) -> int:
    from . import _api as api

    return report_mirror(api.mirror_sources(home=args.home))


def report_mirror(r: dict) -> int:
    """Print the operator report for an ``_api.mirror_sources`` result; return its exit code."""
    for name, p in sorted(r["providers"].items()):
        extras = ""
        if p.get("generations"):
            extras += f" generations={p['generations']}"
        if p.get("sidecars_capped"):
            extras += f" capped={p['sidecars_capped']}"
        if p.get("error_count"):
            extras += f" errors={p['error_count']}"
        print(
            f"{name:<16} {'ok' if p['ok'] else 'FAILED':<8} "
            f"files={p['files']} copied={p['copied']} unchanged={p['unchanged']} "
            f"bytes={p['bytes_in']}→{p['bytes_out']}{extras}"
        )
        for err in p.get("errors", []):
            print(f"    {err}")
    for name in r["unsupported"]:
        print(f"{name:<16} unsupported (watcher shape has no mirror path)")
    print(f"{'OK' if r['ok'] else 'FAILED'} → {r['root']} ({r['duration_s']}s)")
    return 0 if r["ok"] else 1


def cmd_setup(args: argparse.Namespace) -> int:
    """The `thread_archive setup` front door — delegate to the wizard flow."""
    from ._setup.wizard import run_setup

    return run_setup(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thread_archive",
        description="Serverless-native local archive for AI conversations (JSONL truth + SQLite index).",
        epilog="`search` and `read` are the archive-mcp tools at a terminal — same "
               "implementation, same results. The web viewer is the third door, "
               "cohosted by `thread_archive watch --web` (`thread_archive web` opens it).",
    )
    parser.add_argument("--version", action="version", version=f"thread-archive {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # The human front door: first-run discovery → consent → import → watcher /
    # backup / MCP wiring. Delegates to the wizard (_setup.wizard.run_setup).
    p_setup = sub.add_parser(
        "setup",
        help="first-run setup: discover local AI-tool stores, import with consent, "
             "then offer the watcher, nightly backup, and MCP wiring",
    )
    _add_home_arg(p_setup)
    p_setup.add_argument("-y", "--yes", action="store_true",
                         help="accept every default; never prompt (agent/script mode)")
    p_setup.add_argument("--skip-import", action="store_true", help="don't import now")
    p_setup.add_argument("--skip-watcher", action="store_true", help="don't offer the watcher")
    p_setup.add_argument("--skip-backup", action="store_true", help="don't offer nightly backup")
    p_setup.add_argument("--backup-dest", default=None, metavar="PATH",
                         help="schedule nightly backups to PATH without prompting")
    p_setup.add_argument("--skip-mcp", action="store_true", help="don't offer MCP wiring")
    p_setup.set_defaults(func=cmd_setup)

    # Retrieval. The flags mirror the thread_search / thread_read tool parameters;
    # the long-form contract for each (what a scope means, when to reach for it)
    # lives in the tools' own docstrings, in _tools.py.
    p_search = sub.add_parser(
        "search",
        help="search the archive — the thread_search tool, at a terminal",
        description=(
            "Search the local conversation archive: lexical FTS5 + optional semantic\n"
            "vectors, fused, ranked, optionally re-ranked. This is the thread_search\n"
            "MCP tool with a terminal in front of it — same results, same notes.\n"
            "\n"
            "An empty query is a browse: one row per thread, newest activity first,\n"
            "honoring the structural filters. Results are one page — the header names\n"
            "the page and the total, --page walks them, and --group browse is the\n"
            "shape that enumerates every matched thread completely.\n"
            "\n"
            "Read the quality signal in the header before trusting a hit: weak, or a\n"
            "0-of-N term count, means these are nearest-neighbour guesses."
        ),
        epilog=(
            "examples:\n"
            "  thread_archive search 'retry backoff' --since 7d\n"
            "  thread_archive search --source cursor --limit 20   # browse: recent cursor sessions\n"
            "  thread_archive search --path _retrieval/rank.py --path-ops edit,write\n"
            "  thread_archive search auth --group browse --page 2\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_home_arg(p_search)
    p_search.add_argument(
        "query", nargs="?", default="",
        help="natural language, \"quoted phrases\", AND/OR/NOT, pipe-OR (a|b), or code "
             "identifiers; omit it to browse threads by last activity",
    )
    p_search.add_argument("--limit", type=int, default=10,
                          help="results per page — threads for the list shapes (default 10)")
    p_search.add_argument("--page", type=int, default=1,
                          help="1-based page of the same ordering (default 1)")
    p_search.add_argument("--thread-id", default=None, metavar="REF",
                          help="scope to one conversation (ULID, legacy integer id, or "
                               "provider session id)")
    p_search.add_argument("--topic-id", default=None, metavar="REF",
                          help="scope to a topic's member conversations")
    p_search.add_argument("--content-type", default=None, metavar="TYPE",
                          help="one of user/text/thinking/tool/title/summary/... "
                               "(default: everything but derived summaries; 'all' folds those in)")
    p_search.add_argument("--exclude-content-type", default=None, metavar="TYPES",
                          help="comma-separated content types to drop")
    p_search.add_argument("--since", default=None, metavar="WHEN",
                          help="lower bound — ISO timestamp or a relative age like 7d")
    p_search.add_argument("--until", default=None, metavar="WHEN",
                          help="upper bound — ISO timestamp or a relative age")
    p_search.add_argument("--tool-name", default=None, metavar="NAME",
                          help="only events that ran this tool (e.g. Bash)")
    p_search.add_argument("--source", default=None, metavar="PROVIDERS",
                          help="comma-separated providers, e.g. claude-code,cursor")
    p_search.add_argument("--types", default=None, metavar="TYPES",
                          help="comma-separated thread_type values: conversation, topic, system")
    p_search.add_argument("--agents", default=None, metavar="MODE",
                          help="agent-run (subagent/machinery) threads: exclude (default), "
                               "include, or only")
    p_search.add_argument("--startswith", default=None, metavar="PREFIX",
                          help="structural prefix scan over content (the query text is unused)")
    p_search.add_argument("--path", default=None, metavar="PATH",
                          help="the code axis: conversations that touched this file — a bare "
                               "name, a partial or absolute path, a directory (its whole "
                               "subtree), or a glob")
    p_search.add_argument("--path-ops", default=None, metavar="OPS",
                          help="narrow --path to these verbs: edit,write,delete (changes), "
                               "read (a look), search,run (incidental mentions)")
    p_search.add_argument("--commit", default=None, metavar="SHA",
                          help="the loop back from git blame: the conversations this commit "
                               "is made of")
    p_search.add_argument("--repo", default=None, metavar="PATH",
                          help="with --commit: the repository, when it isn't one the archive "
                               "has seen sessions run in")
    p_search.add_argument("--sort", default=None, metavar="ORDER",
                          help="'oldest' for chronological order (when was this first "
                               "discussed); the default is relevance")
    p_search.add_argument("--group", default=None, metavar="MODE",
                          help="how hits relate to threads: the default folds to one row per "
                               "thread; 'none' is every hit, 'browse' lists matched threads, "
                               "'nested' clusters hits under theirs")
    p_search.add_argument("--output", default=None, metavar="SHAPE",
                          help="'count' for a per-thread tally, 'linkable' for JSON event/thread ids")
    p_search.add_argument("--context-lines", type=int, default=2, metavar="N",
                          help="±N numbered lines around each match (0 = the raw FTS snippet)")
    p_search.add_argument("--context-events", default=None, metavar="SPEC",
                          help="append neighbouring events: 'N', 'before:after', or "
                               "'before:after:types'")
    p_search.add_argument("--match", default=None, metavar="MODE",
                          help="'token' (default, indexed) or 'substring' (uncapped infix "
                               "scan — finds p4 inside mp4)")
    rerank = p_search.add_mutually_exclusive_group()
    rerank.add_argument("--rerank", dest="rerank", action="store_true", default=None,
                        help="force the cross-encoder re-rank on (needs the [embeddings] extra)")
    rerank.add_argument("--no-rerank", dest="rerank", action="store_false",
                        help="force it off (the default auto-gates on the query's shape)")
    p_search.set_defaults(func=cmd_search)

    p_read = sub.add_parser(
        "read",
        help="read a conversation — the thread_read tool, at a terminal",
        description=(
            "Replay a thread from the event log. The id is whatever you have: the\n"
            "archive's ULID (what search results carry), a legacy integer id, or the\n"
            "session uuid the harness knows the conversation by — all three resolve.\n"
            "A topic id reads as that topic's page.\n"
            "\n"
            "The read is size-budgeted, so a long thread comes back in chunks with a\n"
            "footer naming the offset to continue from. Exits nonzero when the id\n"
            "names nothing readable."
        ),
        epilog=(
            "examples:\n"
            "  thread_archive read 01JQ8ZK4X0000000000000000 --mode chat\n"
            "  thread_archive read 4d1c9f2e-… --summary files    # what this session changed\n"
            "  thread_archive read 812 --around-event 90210      # open a search hit in context\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_home_arg(p_read)
    p_read.add_argument(
        "id", metavar="ID",
        help="ULID thread id, legacy integer id, or a provider session uuid",
    )
    p_read.add_argument(
        "--mode", default=None, choices=["user", "chat", "full", "last", "ends"],
        help="view: user (default — the questions asked), chat (readable conversation), "
             "full (every tool call too), last (the closing assistant text), "
             "ends (first + last turns)",
    )
    p_read.add_argument(
        "--summary", nargs="?", const="toc", default=False,
        choices=["toc", "short", "indexed", "files"],
        help="a summary instead of the transcript: toc (per-message contents; the "
             "default when the flag is bare), short / indexed (the stored summaries, "
             "where a thread has one), files (what this session touched)",
    )
    p_read.add_argument("--limit", type=int, default=200, metavar="N",
                        help="max turns per chunk (default 200; the char budget usually bites first)")
    p_read.add_argument("--offset", type=int, default=0, metavar="N",
                        help="skip N turns — the CHUNKED footer names the next one; "
                             "negative counts from the end (-20 = last 20 turns)")
    p_read.add_argument("--max-chars", type=int, default=0, metavar="N",
                        help="per-chunk character budget (default ~48k); lower it for a "
                             "cheap skim of a long thread")
    p_read.add_argument("--tool-results", action="store_true",
                        help="fold each tool's output under its call (needs --mode full)")
    p_read.add_argument("--around-event", type=int, default=None, metavar="EVENT_ID",
                        help="open a search hit: its whole turn, plus --context-turns "
                             "either side")
    p_read.add_argument("--after-event", type=int, default=None, metavar="EVENT_ID",
                        help="resume from the turn after this event (a robust --offset)")
    p_read.add_argument("--context-turns", type=int, default=1, metavar="N",
                        help="turns either side of --around-event, or per end for "
                             "--mode ends (default 1)")
    p_read.set_defaults(func=cmd_read)

    p_import = sub.add_parser("import", help="import a transcript or provider store")
    _add_home_arg(p_import)
    p_import.add_argument("path", help="a provider's session transcript, or its whole store")
    # Not an argparse `choices`: the provider set depends on --home (a plugin can be
    # declared in that home's config), and choices are fixed before any argument is
    # parsed. cmd_import validates against the registry for the home actually given.
    p_import.add_argument(
        "--provider",
        default=None,
        help="source provider (default: claude-code; `thread_archive providers` lists them)",
    )
    p_import.set_defaults(func=cmd_import)

    p_providers = sub.add_parser("providers", help="list registered providers (built-in + plugins)")
    _add_home_arg(p_providers)
    p_providers.add_argument(
        "--all", action="store_true", help="include archive's own machinery sources"
    )
    p_providers.set_defaults(func=cmd_providers)

    p_import_export = sub.add_parser(
        "import-export",
        help="import a downloaded claude.ai / ChatGPT / xAI account export (ZIP or dir)",
    )
    _add_home_arg(p_import_export)
    p_import_export.add_argument("path", help="export ZIP file or unzipped directory")
    p_import_export.add_argument(
        "--force", action="store_true", help="reimport conversations already present"
    )
    p_import_export.set_defaults(func=cmd_import_export)

    p_watch = sub.add_parser("watch", help="watch local AI-tool stores and import incrementally")
    _add_home_arg(p_watch)
    p_watch.add_argument("--once", action="store_true", help="poll once and exit")
    p_watch.add_argument("--quiet", action="store_true",
                         help="suppress the live progress line (--once)")
    p_watch.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    p_watch.add_argument("--web", action="store_true", help="cohost the web viewer (persistent URL)")
    p_watch.add_argument("--web-host", default="127.0.0.1", help="cohosted viewer bind host (non-loopback refused unless THREAD_ARCHIVE_WEB_NONLOCAL=1)")
    p_watch.add_argument("--web-port", type=int, default=8787, help="cohosted viewer bind port")
    p_watch.add_argument("--no-embed", dest="embed", action="store_false",
                         help="disable the live vector cohost (no semantic-index upkeep)")
    p_watch.add_argument("--embed-interval", type=float, default=300.0,
                         help="seconds between vector-cohost passes (default 300)")
    p_watch.add_argument("--embed-batch", type=int, default=512,
                         help="max events embedded per cohost pass (default 512)")
    p_watch.set_defaults(func=cmd_watch)

    # Opens the cohosted viewer; never serves it (see cmd_web).
    p_web = sub.add_parser(
        "web", help="open the cohosted web viewer in a browser (the watcher serves it)"
    )
    p_web.add_argument("--port", type=int, default=8787, help="viewer port (default 8787)")
    # `web dev` reads as a mode, not a flag, which is what it is — and leaves
    # room for other dev pages to join the same switch. The default is None
    # rather than False so a plain `web` opens the viewer without restating a
    # preference the browser is already remembering.
    p_web.add_argument(
        "mode", nargs="?", choices=["dev"], default=None,
        help="'dev' shows the dev pages (the retrieval report) in the navigation",
    )
    p_web.add_argument(
        "--no-dev", dest="no_dev", action="store_true",
        help="hide the dev pages again",
    )
    p_web.set_defaults(func=cmd_web)

    p_reindex = sub.add_parser("reindex", help="rebuild index.db from the JSONL truth directory")
    _add_home_arg(p_reindex)
    p_reindex.add_argument("--vectors", action="store_true", help="also rebuild local vectors")
    p_reindex.add_argument(
        "--salvage", action="store_true",
        help="publish the rebuild even if it loses committed records the current "
             "index holds (the default refuses and keeps the old index)",
    )
    p_reindex.set_defaults(func=cmd_reindex)

    p_migrate = sub.add_parser(
        "migrate", help="migrate older truth to the current format, reindex, and verify"
    )
    _add_home_arg(p_migrate)
    p_migrate.add_argument(
        "--dry-run", action="store_true",
        help="build and inspect migrated truth without swapping or rebuilding the index",
    )
    p_migrate.set_defaults(func=cmd_migrate)

    p_embed = sub.add_parser("embed", help="embed user/text events missing a vector (incremental catch-up)")
    _add_home_arg(p_embed)
    p_embed.add_argument("--rebuild", action="store_true", help="re-embed everything, not just the gap")
    p_embed.add_argument("--limit", type=int, default=None, help="cap events embedded this run")
    p_embed.add_argument("--newest-first", action="store_true",
                         help="embed the freshest gap first (recent threads findable soonest)")
    p_embed.add_argument("--quiet", action="store_true",
                         help="suppress the live progress line")
    p_embed.set_defaults(func=cmd_embed)

    p_loads = sub.add_parser(
        "loads", help="load progress: the in-flight load and recent runs, by phase")
    _add_home_arg(p_loads)
    p_loads.add_argument("--limit", type=int, default=10, help="recent runs to show")
    p_loads.set_defaults(func=cmd_loads)

    p_coverage = sub.add_parser(
        "coverage",
        help="capture-coverage check: source stores reconciled against the archive",
    )
    _add_home_arg(p_coverage)
    p_coverage.set_defaults(func=cmd_coverage)

    p_mirror = sub.add_parser(
        "mirror",
        help="mirror raw harness source stores into <home>/source-mirror "
        "(verbatim, gzip; nothing ever deleted)",
    )
    _add_home_arg(p_mirror)
    p_mirror.set_defaults(func=cmd_mirror)

    p_self_update = sub.add_parser(
        "self-update",
        help="update this clone to the newest released tag "
             "(fetch, checkout, reinstall, restart the daemons)",
    )
    _add_home_arg(p_self_update)
    p_self_update.add_argument(
        "--check", action="store_true",
        help="fetch release tags and report availability; do not change the clone",
    )
    p_self_update.add_argument(
        "--allow-format-bump", action="store_true",
        help="permit an update whose truth-format version is newer than this "
             "install reads — one-way: after the new code touches the store, "
             "rolling back leaves a reader that refuses it",
    )
    p_self_update.set_defaults(func=cmd_self_update)

    p_status = sub.add_parser("status", help="archive health / paths / counts")
    _add_home_arg(p_status)
    p_status.set_defaults(func=cmd_status)

    p_backup = sub.add_parser("backup", help="mirror the JSONL truth dir to a backup destination")
    _add_home_arg(p_backup)
    p_backup.add_argument("dest", help="backup destination dir")
    p_backup.add_argument(
        "--allow-shrink", action="store_true",
        help="let a smaller source file overwrite its larger backup copy (only after "
             "a deliberate truth re-emit; the default keeps the backup copy)",
    )
    p_backup.add_argument(
        "--no-verify", dest="verify", action="store_false",
        help="skip the pre-backup integrity verify of the source truth",
    )
    p_backup.set_defaults(func=cmd_backup)

    p_verify = sub.add_parser("verify", help="integrity check: truth parses + matches the index")
    _add_home_arg(p_verify)
    p_verify.add_argument(
        "--deep", action="store_true",
        help="id-level truth↔index diff + dedup_key parity + knowledge-layer and "
             "dangling-reference checks (slower)",
    )
    p_verify.add_argument(
        "--hashes", action="store_true",
        help="re-hash every payload against its dedup_key's content hash, both stores "
             "(rot detection; report-only, CPU-heavy)",
    )
    p_verify.add_argument(
        "--backup", default=None, metavar="DEST",
        help="also parse-and-count a backup mirror at DEST and report its coverage "
             "against the live truth",
    )
    p_verify.set_defaults(func=cmd_verify)

    p_drill = sub.add_parser(
        "restore-drill",
        help="prove a backup restores: rebuild a full index from the mirror in "
             "a throwaway home and compare counts",
    )
    _add_home_arg(p_drill)
    p_drill.add_argument("dest", help="backup mirror to restore from")
    p_drill.add_argument(
        "--keep-home", action="store_true",
        help="keep the throwaway home (inspect the restored index) instead of deleting it",
    )
    p_drill.set_defaults(func=cmd_restore_drill)

    p_restore = sub.add_parser(
        "restore",
        help="restore a real archive home from a backup mirror: staged rebuild, "
             "verify, then atomic publish (the drill proves; this restores)",
    )
    p_restore.add_argument("dest", help="backup mirror to restore from")
    p_restore.add_argument("--to", default=None, metavar="HOME",
                           help="home directory to restore into")
    p_restore.add_argument(
        "--generation", default=None, metavar="STAMP",
        help="restore this retained pre-run snapshot instead of the mirror head",
    )
    p_restore.add_argument(
        "--list-generations", action="store_true",
        help="list the mirror's retained restore points and exit",
    )
    p_restore.add_argument(
        "--replace", action="store_true",
        help="set aside a non-empty target home (preserved as <home>.damaged-<stamp>)",
    )
    p_restore.add_argument(
        "--allow-parse-errors", action="store_true",
        help="restore a mirror whose scan has parse errors (a flawed copy beats none)",
    )
    p_restore.set_defaults(func=cmd_restore)

    p_nightly = sub.add_parser(
        "nightly",
        help="scheduled pipeline: backup → verify (age-gated deep/hashes "
             "escalation) → restore drill, with per-stage health records",
    )
    _add_home_arg(p_nightly)
    p_nightly.add_argument("dest", help="backup destination dir")
    p_nightly.add_argument(
        "--notify-url", default=None, metavar="URL",
        help="POST {title, message} here when any stage fails "
             "(lab's /api/notify shape); silence still needs a staleness watcher",
    )
    p_nightly.add_argument(
        "--allow-shrink", action="store_true",
        help="pass through to the backup stage (after a deliberate truth re-emit)",
    )
    p_nightly.add_argument(
        "--no-drill", dest="drill", action="store_false",
        help="skip the restore drill stage",
    )
    p_nightly.set_defaults(func=cmd_nightly)

    p_repair = sub.add_parser(
        "repair",
        help="quarantine unparseable truth lines and restore committed content "
             "the truth lacks from the index",
    )
    _add_home_arg(p_repair)
    p_repair.add_argument(
        "--dry-run", action="store_true",
        help="report what would be quarantined/restored without touching anything",
    )
    p_repair.set_defaults(func=cmd_repair)

    p_fix = sub.add_parser(
        "fix-import",
        help="repair a drifted provider import on this machine: scaffold an "
             "override patch under <home>/plugins/ (module, tests, samples, "
             "evidence, protocol) for you or your own agent to write the parse "
             "fix in, then gate it through tests, activation, and a "
             "ledger-driven re-import with --activate. Patches retire on the "
             "next self-update unless pinned",
    )
    _add_home_arg(p_fix)
    p_fix.add_argument(
        "provider", help="the drifted provider (`thread_archive providers` lists them)"
    )
    p_fix.add_argument(
        "--activate", action="store_true",
        help="the deterministic gate: load the patch, run its test suite, and "
             "only on green enable the override and re-import what the broken "
             "parser consumed",
    )
    p_fix.add_argument(
        "--no-reimport", action="store_true",
        help="with --activate: skip the ledger-driven re-import",
    )
    pin_group = p_fix.add_mutually_exclusive_group()
    pin_group.add_argument(
        "--pin", action="store_true",
        help="keep this patch across self-updates (\"I always want mine\")",
    )
    pin_group.add_argument(
        "--unpin", action="store_true",
        help="return the patch to the default retire-on-update lifecycle",
    )
    p_fix.set_defaults(func=cmd_fix_import)

    p_daemon = sub.add_parser(
        "daemon",
        help="manage the archive service agents (launchd on macOS, systemd on "
             "Linux): the watcher (the upgrade from lazy MCP-cohosted ingest to "
             "always-fresh), or with --mcp the shared MCP server (one HTTP server "
             "for all clients), --backup the scheduled nightly backup pipeline",
    )
    _add_home_arg(p_daemon)
    p_daemon.add_argument(
        "action", choices=["install", "uninstall", "restart", "status"],
        help="install writes the service manifest (pointing at this environment's "
             "console script) and (re)loads the agent; restart applies a code edit "
             "to the running agent",
    )
    p_daemon.add_argument(
        "--mcp", action="store_true",
        help="target the shared MCP server agent instead of the watcher",
    )
    p_daemon.add_argument(
        "--backup", action="store_true",
        help="target the nightly-backup agent: the scheduled backup → verify → "
             "restore-drill pipeline (`thread_archive nightly`)",
    )
    p_daemon.add_argument(
        "--dest", default=None, metavar="PATH",
        help="--backup install only: backup destination dir",
    )
    p_daemon.add_argument(
        "--at", default=None, metavar="HH:MM",
        help="--backup install only: daily fire time, local (default 04:00)",
    )
    p_daemon.add_argument(
        "--notify-url", default=None, metavar="URL",
        help="--backup install only: POST {title, message} on any stage failure "
             "(lab's /api/notify shape)",
    )
    p_daemon.add_argument(
        "--no-web", dest="web", action="store_false",
        help="watcher only: don't cohost the web viewer in the watcher process",
    )
    p_daemon.add_argument(
        "--web-port", type=int, default=8787, help="cohosted viewer port (default 8787)"
    )
    from ._service import MCP_DEFAULT_HOST, MCP_DEFAULT_PORT
    p_daemon.add_argument(
        "--http-host", default=MCP_DEFAULT_HOST,
        help="--mcp only: shared MCP server bind host (default 127.0.0.1)",
    )
    p_daemon.add_argument(
        "--http-port", type=int, default=MCP_DEFAULT_PORT,
        help="--mcp only: shared MCP server bind port (default 8788)",
    )
    p_daemon.add_argument(
        "--mcp-ingest", action="store_true",
        help="--mcp install only: explicitly let the shared MCP process run local "
             "catch-up ingest (off by default; unnecessary when the watcher runs)",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
