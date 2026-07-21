"""The ``archive`` command — a thin CLI over the :mod:`thread_archive._api` surface.

**Private operational tooling, not public API.** The package's public surface
is the retrieval MCP tools plus the truth format (see the package docstring);
this CLI is the process seam launchd, cron, and operators use to run the
private machinery — ingest (``import``, ``import-export``, ``watch``,
``embed``), the backup kit (``backup``, ``verify``, ``restore-drill``,
``restore``, ``reindex``, ``migrate``, ``repair``, ``status``, ``nightly``, ``coverage``),
and the LaunchAgent lifecycle (``daemon``). Verbs may change without
external notice, but they are *wired into* the LaunchAgent plists, lab's cron
script, the /ci skill, and the monitor's heartbeat contract — renaming one
means updating those in the same change (``tests/test_public_api.py`` pins the
set so the change is deliberate).

Retrieval deliberately has no CLI verbs: search and read are the public MCP
tools, and the web viewer is cohosted by the always-on watcher
(``archive watch --web``). One retrieval surface, not three.
"""

from __future__ import annotations

import argparse
import sys

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
        raise SystemExit(f"archive daemon: --at must be HH:MM (got {s!r})")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise SystemExit(f"archive daemon: --at must be HH:MM (got {s!r})")
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
        try:
            import ctypes

            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            # setiopolicy_np(IOPOL_TYPE_DISK=0, IOPOL_SCOPE_PROCESS=1, IOPOL_THROTTLE=5)
            libc.setiopolicy_np(0, 1, 5)
        except Exception:  # pragma: no cover — platform best-effort
            pass


def cmd_import(args: argparse.Namespace) -> int:
    from . import _api as api
    from ._importers import db_scanners, line_stream_importers

    line_streams = line_stream_importers(args.home)
    scanners = db_scanners(args.home)
    provider = args.provider or "claude-code"
    if provider not in line_streams and provider not in scanners:
        known = ", ".join(sorted({**line_streams, **scanners}))
        raise SystemExit(
            f"archive import: unknown provider '{provider}' (known: {known})"
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
        from ._truth import shared_ingest_lock

        # Same coverage as api.watch(once=True): the poll appends truth and
        # commits, so it must hold the reindex lock shared — an unlocked
        # one-shot racing a reindex can land truth after the rebuild's read
        # point and commit into the inode the swap replaces. Blocking (bounded
        # by one rebuild): a one-shot has no later pass to retry on.
        with shared_ingest_lock():
            result = watcher.poll_once()
            if result.events_created > 0:
                watcher.maintain()  # one upkeep pass (rebalance/manifest) for the one-shot
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
        from ._web import serve_in_thread

        httpd = serve_in_thread(host=args.web_host, port=args.web_port)
        logging.getLogger("thread_archive._watcher").info(
            "cohosting web viewer on http://%s:%s", args.web_host, args.web_port
        )

    available = [w.source_name for w in watcher.available()]
    logging.getLogger("thread_archive._watcher").info(
        "watching %d sources %s every %ss (maintenance every %.0fs)",
        len(available), available, args.interval, watcher.maintenance_interval,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
    finally:
        if httpd is not None:
            httpd.server_close()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from . import _launchd

    if args.mcp:
        # The shared MCP server: one always-on streamable-HTTP server all clients
        # connect to (point each client's MCP config at the URL below), instead of
        # a per-client stdio subprocess each loading its own retrieval model.
        if args.action == "install":
            plist = _launchd.install_mcp(
                args.home,
                host=args.http_host,
                port=args.http_port,
                ingest=args.mcp_ingest,
            )
            print(f"installed {_launchd.MCP_LABEL} ({plist})")
            print(f"shared MCP server: http://{args.http_host}:{args.http_port}/mcp")
            print(
                "catch-up ingest: "
                + ("enabled (explicit --mcp-ingest opt-in)" if args.mcp_ingest else "disabled")
            )
            print("point every client's MCP config at that URL "
                  '(type "http") instead of the archive-mcp stdio command.')
        elif args.action == "uninstall":
            _launchd.uninstall_mcp()
            print(f"uninstalled {_launchd.MCP_LABEL}")
        elif args.action == "restart":
            _launchd.restart_mcp()
            print(f"restarted {_launchd.MCP_LABEL}")
        else:  # status
            print(_launchd.mcp_status())
        return 0

    if args.backup:
        # The scheduled backup pipeline (backup → verify → restore drill) as
        # a launchd agent — the productized form of what host/ wires by hand.
        # install needs --dest (a directory launchd can reach unattended).
        if args.action == "install":
            if not args.dest:
                print(
                    "archive daemon install --backup needs --dest <path>",
                    file=sys.stderr,
                )
                return 2
            hour, minute = _parse_hhmm(args.at or "04:00")
            plist = _launchd.install_backup(
                args.dest, args.home, hour=hour, minute=minute,
                notify_url=args.notify_url,
            )
            print(f"installed {_launchd.BACKUP_LABEL} ({plist})")
            print(
                f"nightly at {hour:02d}:{minute:02d} → {args.dest}: "
                "backup, verify, restore drill."
            )
        elif args.action == "uninstall":
            _launchd.uninstall_backup()
            print(f"uninstalled {_launchd.BACKUP_LABEL}")
        elif args.action == "restart":
            _launchd.restart_backup()
            print(f"restarted {_launchd.BACKUP_LABEL}")
        else:  # status
            print(_launchd.backup_status())
        return 0

    if args.action == "install":
        plist = _launchd.install_watcher(args.home, web=args.web, web_port=args.web_port)
        print(f"installed {_launchd.WATCHER_LABEL} ({plist})")
        print("the watcher is always-on (RunAtLoad); `archive daemon status` to check,")
        print("`archive daemon restart` to apply a code edit.")
        if args.web:
            print(f"web viewer: http://127.0.0.1:{args.web_port}")
    elif args.action == "uninstall":
        _launchd.uninstall_watcher()
        print(f"uninstalled {_launchd.WATCHER_LABEL}")
    elif args.action == "restart":
        _launchd.restart_watcher()
        print(f"restarted {_launchd.WATCHER_LABEL}")
    else:  # status
        print(_launchd.watcher_status())
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
        f"`archive fix-import {args.provider} --activate`"
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


def cmd_embed(args: argparse.Namespace) -> int:
    from . import _api as api

    print("embedding missing vectors (rebuild=%s)..." % args.rebuild, flush=True)
    res = api.embed(home=args.home, rebuild=args.rebuild, max_events=args.limit,
                    newest_first=args.newest_first)
    print(f"  embedded {res['embedded']}")
    return 0


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
            "destination's .recovery/ (config, keyring, retained exports) is stale"
        )
    elif "bundle_files" in res:
        if res["keyring_in_bundle"]:
            keyring = "keyring included"
        elif res.get("keyring_opted_out"):
            keyring = "keyring EXCLUDED — config opt-out"
        else:
            keyring = "no keyring at the home"
        print(
            f"recovery bundle: {res['bundle_files']} file(s) "
            f"({res['bundle_copied']} copied, {res['bundle_deleted']} removed; {keyring})"
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
            "problems; mirror ran additively (no deletions). Run `archive verify`."
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
    # updating restores yesterday's keyring and config.
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
            f"interior={t['parse_errors_interior']} — `archive repair` quarantines "
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
            f"orphans={fts['orphan_rows']} — `archive reindex` rebuilds the search surface"
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
            print("bundle: ABSENT — this mirror restores conversations only (no config/keyring/exports)")
        else:
            keys = "none" if b["keyring_keys"] is None else str(b["keyring_keys"])
            print(
                f"bundle: config={'yes' if b['config'] else 'no'} "
                f"keyring keys={keys} retained exports={b['retained_exports']}"
                + (" (keyring UNREADABLE)" if b.get("keyring_unreadable") else "")
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
        installed = [
            name
            for name, on in (("config", b.get("config")), ("keyring", b.get("keyring")))
            if on
        ]
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
        print("note: the repaired files shrank — the next `archive backup` may need --allow-shrink")
    if not res["dry_run"]:
        print("run `archive verify` to confirm the archive is clean")
    return 0


def cmd_redact(args: argparse.Namespace) -> int:
    from . import _api as api

    if args.list_:
        return report_redactions(api.redactions(home=args.home))
    if args.show_key:
        print(api.redact_show_key(args.show_key, home=args.home))
        print(
            "escrow this somewhere off this machine, then `archive redact "
            f"--forget {args.show_key} --yes` removes it from the keyring",
            file=sys.stderr,
        )
        return 0
    if args.forget:
        if not args.yes:
            print(
                "refusing: --forget removes the key — if it was never escrowed "
                "(--show-key) the content is unrecoverable forever. Add --yes to proceed."
            )
            return 2
        api.redact_forget_key(args.forget, home=args.home)
        print(f"key {args.forget} removed from the keyring")
        return 0
    if args.restore_key:
        kid, key_b64 = args.restore_key
        api.redact_restore_key(kid, key_b64, home=args.home)
        print(f"key {kid} restored to the keyring — `archive unredact {kid}` will now work")
        return 0
    if args.thread is None:
        print("usage: archive redact <thread_id> [--events IDS] [--reason ...] "
              "(or --list / --show-key / --forget / --restore-key)")
        return 2
    event_ids = [int(e) for e in args.events.split(",")] if args.events else None
    # args.thread is a ref — ULID id, legacy integer alias, or provider session
    # id — passed through raw; the API layer resolves it.
    return report_redact(
        api.redact(args.thread, event_ids, reason=args.reason, home=args.home)
    )


def report_redact(res: dict) -> int:
    """Print the operator report for an ``_api.redact`` result; return its exit code."""
    print(
        f"redacted {res['events_redacted']} event(s) in thread {res['thread_id']}"
        + (f" under key {res['key_id']}" if res.get("key_id") else "")
    )
    if res.get("topic_quotes_scrubbed") or res.get("kg_quotes_scrubbed"):
        print(
            f"scrubbed {res.get('topic_quotes_scrubbed', 0)} topic quote(s), "
            f"{res.get('kg_quotes_scrubbed', 0)} kg quote(s)"
        )
    for note in res.get("notes", []):
        print(f"note: {note}")
    if res.get("key_id"):
        print(f"reverse with `archive unredact {res['key_id']}`; "
              f"escrow with `archive redact --show-key {res['key_id']}`")
    return 0


def report_redactions(rows: list[dict]) -> int:
    """Print the redaction ledger (``archive redact --list``); return its exit code."""
    if not rows:
        print("no redactions")
        return 0
    for r in rows:
        scope = f"{len(r['event_ids'])} event(s)" if r["event_ids"] else "whole thread"
        reason = f"  reason: {r['reason']}" if r.get("reason") else ""
        print(
            f"{r['key_id']}  thread {r['thread_id']}  {scope}  "
            f"{r['status']}, key {r['key']}  {r.get('redacted_at', '?')}{reason}"
        )
    return 0


def cmd_unredact(args: argparse.Namespace) -> int:
    from . import _api as api

    res = api.unredact(args.key_id, home=args.home)
    print(f"restored {res['events_restored']} event(s) in thread {res['thread_id']}")
    for note in res.get("notes", []):
        print(f"note: {note}")
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

    return report_status(api.status(home=args.home))


def report_status(st: dict) -> int:
    """Print the operator report for an ``_api.status`` result; return its exit code."""
    print(f"home:    {st['home']}")
    print(f"truth:   {st['truth_dir']}")
    print(f"index:   {st['index_path']}")
    print(f"threads: {st['threads']}")
    print(f"events:  {st['events']}")
    print(f"indexed: {st['fts_indexed']}")
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
        # Only ever printed once a release check has run. Scheduled checks are
        # non-mutating by default; the line names an available release so the
        # operator can choose when to apply it.
        action = u.get("action", "?")
        if action == "updated":
            print(f"update:  {u.get('reason')} {u['at']} ({_age(u['at'])})")
        elif action == "update":
            print(
                f"update:  {u.get('tag')} available — run `archive self-update` "
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
    return 0


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
              "run `archive self-update` to apply")
    elif action == "up-to-date":
        print(f"self-update: up to date (v{res['current']}) — {res['reason']}")
    else:
        print(f"self-update: {action.upper() if action else '?'}: {res.get('reason')}")
    for line in res.get("skipped") or []:
        print(f"  · {line}")
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
            f"remedy: archive fix-import {name}"
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive",
        description="Serverless-native local archive for AI conversations (JSONL truth + SQLite index).",
        epilog="Retrieval has no CLI verbs by design: search/read are the archive-mcp "
               "tools, and the web viewer is cohosted by `archive watch --web`.",
    )
    parser.add_argument("--version", action="version", version=f"thread-archive {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_import = sub.add_parser("import", help="import a transcript or provider store")
    _add_home_arg(p_import)
    p_import.add_argument("path", help="a provider's session transcript, or its whole store")
    # Not an argparse `choices`: the provider set depends on --home (a plugin can be
    # declared in that home's config), and choices are fixed before any argument is
    # parsed. cmd_import validates against the registry for the home actually given.
    p_import.add_argument(
        "--provider",
        default=None,
        help="source provider (default: claude-code; `archive providers` lists them)",
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
    p_embed.set_defaults(func=cmd_embed)

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

    p_redact = sub.add_parser(
        "redact",
        help="crypto-shred events: content replaced by a marker everywhere it "
             "lives, the original encrypted into truth/redactions.jsonl under a "
             "revocable key in <home>/keyring.json",
    )
    _add_home_arg(p_redact)
    p_redact.add_argument(
        "thread", nargs="?",
        help="thread ref: ULID id, legacy integer id, or provider session id",
    )
    p_redact.add_argument(
        "--events", help="comma-separated event ids (default: the whole thread)"
    )
    p_redact.add_argument("--reason", help="recorded on the redaction record")
    p_redact.add_argument(
        "--list", dest="list_", action="store_true",
        help="list redactions with their lifecycle state",
    )
    p_redact.add_argument(
        "--show-key", metavar="KEY_ID",
        help="print a key's base64 material for escrow off this machine",
    )
    p_redact.add_argument(
        "--forget", metavar="KEY_ID",
        help="remove a key from the keyring (with --yes); crypto-erasure if never escrowed",
    )
    p_redact.add_argument(
        "--restore-key", nargs=2, metavar=("KEY_ID", "KEY_B64"),
        help="put an escrowed key back so `archive unredact` can use it",
    )
    p_redact.add_argument("--yes", action="store_true", help="confirm --forget")
    p_redact.set_defaults(func=cmd_redact)

    p_unredact = sub.add_parser(
        "unredact", help="restore redacted events from their encrypted bundle"
    )
    _add_home_arg(p_unredact)
    p_unredact.add_argument("key_id", help="the redaction's key id (see `archive redact --list`)")
    p_unredact.set_defaults(func=cmd_unredact)

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
        "provider", help="the drifted provider (`archive providers` lists them)"
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
        help="manage the archive LaunchAgents (macOS): the watcher (the upgrade "
             "from lazy MCP-cohosted ingest to always-fresh), or with --mcp the "
             "shared MCP server (one HTTP server for all clients), --backup the "
             "scheduled nightly backup pipeline",
    )
    _add_home_arg(p_daemon)
    p_daemon.add_argument(
        "action", choices=["install", "uninstall", "restart", "status"],
        help="install writes the plist (pointing at this environment's console "
             "script) and (re)loads the agent; restart applies a code edit to the "
             "running agent",
    )
    p_daemon.add_argument(
        "--mcp", action="store_true",
        help="target the shared MCP server agent (com.thread-archive.mcp) instead "
             "of the watcher",
    )
    p_daemon.add_argument(
        "--backup", action="store_true",
        help="target the nightly-backup agent (com.thread-archive.backup): the "
             "scheduled backup → verify → restore-drill pipeline (`archive nightly`)",
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
    from ._launchd import MCP_DEFAULT_HOST, MCP_DEFAULT_PORT
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
