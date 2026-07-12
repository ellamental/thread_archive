"""The ``archive`` command — a thin CLI over the :mod:`thread_archive._api` surface.

**Private operational tooling, not public API.** The package's public surface
is the retrieval MCP tools plus the truth format (see the package docstring);
this CLI is the process seam launchd, cron, and operators use to run the
private machinery — ingest (``import``, ``import-export``, ``watch``,
``embed``), the durability kit (``backup``, ``verify``, ``restore-drill``,
``reindex``, ``repair``, ``status``, ``nightly``, ``coverage``), and the
LaunchAgent lifecycle (``daemon``). Verbs may change without
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


def _self_throttle() -> None:
    """Drop this process to background priority so a full rebuild never starves the
    interactive machine. CPU via ``nice``; on macOS also throttle disk I/O (the FTS
    rebuild is I/O-bound) — the in-process equivalent of ``taskpolicy -d throttle``.
    Best-effort: on an idle box it still runs full speed, and a failure to throttle
    must never stop the work. Renice up if you want a rebuild to go flat-out."""
    try:
        import os

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
    from ._importers import DB_SCANNERS, LINE_STREAM_IMPORTERS

    provider = args.provider or "claude-code"
    if provider not in LINE_STREAM_IMPORTERS and provider not in DB_SCANNERS:
        raise SystemExit(f"archive import: unknown provider '{provider}'")

    result = api.import_path(args.path, home=args.home, provider=provider)  # checkpoints internally

    if provider in LINE_STREAM_IMPORTERS:
        summary = (
            f"thread={result.thread_id} events={result.events_created} new={result.is_new_thread}"
        )
    else:  # DB scanner (cursor / opencode): scans many sessions in one file
        summary = " ".join(f"{k}={v}" for k, v in vars(result).items())
    print(f"imported {args.path} ({provider}): {summary}")
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


def cmd_embed(args: argparse.Namespace) -> int:
    from . import _api as api

    print("embedding missing vectors (rebuild=%s)..." % args.rebuild, flush=True)
    res = api.embed(home=args.home, rebuild=args.rebuild, max_events=args.limit,
                    newest_first=args.newest_first)
    print(f"  embedded {res['embedded']}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from . import _api as api

    res = api.backup(
        args.dest, home=args.home,
        allow_shrink=args.allow_shrink, verify_first=args.verify,
    )
    mb = res["bytes_copied"] / (1024 * 1024)
    print(f"backed up {res['truth_dir']} → {res['dest']}: {res['files_copied']} files ({mb:.1f} MB copied)")
    if res.get("generation_created"):
        print(
            f"generation: pre-run state preserved as .generations/{res['generation_created']} "
            f"({res.get('generations_kept', '?')} kept, {res.get('generations_pruned', 0)} pruned)"
        )
    if res.get("generation_error"):
        print(f"WARNING: generation snapshot failed ({res['generation_error']}) — "
              "this run had no pre-overwrite recovery margin")
    if res.get("same_device"):
        print(
            "WARNING: the backup destination is on the SAME filesystem as the "
            "archive — one disk failure takes both copies (and every generation). "
            "Point it at a different disk/machine, or add an off-machine leg."
        )
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
    return 0 if res["verify_ok"] and not res["deletions_skipped"] else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from . import _api as api

    res = api.verify(home=args.home, deep=args.deep, hashes=args.hashes, backup=args.backup)
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
    if args.deep:
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
    if args.hashes:
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
    if args.backup:
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
    res = api.restore_drill(args.dest, home=args.home, keep_home=args.keep_home)
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
    if res.get("drill_home"):
        print(f"drill home kept: {res['drill_home']}")
    print(f"{'OK' if res.get('ok') else 'RESTORE DRILL FAILED'} ({res.get('seconds', '?')}s)")
    return 0 if res.get("ok") else 1


def cmd_nightly(args: argparse.Namespace) -> int:
    from . import _api as api

    print(f"nightly pipeline: backup → verify → restore drill ({args.dest})", flush=True)
    res = api.nightly(
        args.dest, home=args.home, notify_url=args.notify_url,
        allow_shrink=args.allow_shrink, drill=args.drill,
    )
    b = res["backup"]
    if "error" in b:
        print(f"backup: ERROR {b['error']}")
    else:
        mb = b["bytes_copied"] / (1024 * 1024)
        print(f"backup: {b['files_copied']} files ({mb:.1f} MB copied)"
              + (" [SAME DEVICE as archive]" if b.get("same_device") else ""))
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

    res = api.repair(home=args.home, dry_run=args.dry_run)
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
    if b and b.get("same_device"):
        print("         WARNING: backup destination is on the same filesystem as the archive")
    print(
        f"drill:   {'ok' if d['ok'] else 'FAILED'} coverage={d.get('coverage')} "
        f"{d['at']} ({_age(d['at'])})"
        if d else "drill:   never recorded"
    )
    c = st.get("last_coverage")
    if c and c["ok"]:
        print(
            f"coverage: ok ({c.get('sources_checked', '?')} sources) "
            f"{c['at']} ({_age(c['at'])})"
        )
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
    w = st.get("last_watch_errors")
    if w:
        print(
            f"watch:   poll errors seen, last at {w['at']} ({_age(w['at'])}), "
            f"{w.get('count_since_start', '?')} since daemon start"
        )
        for err in w.get("errors", [])[:3]:
            print(f"         {err}")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from . import _api as api

    r = api.check_coverage(home=args.home)
    for name, s in sorted(r["sources"].items()):
        state = s.get("failed") or s.get("warning") or "ok"
        print(
            f"{name:<16} {state:<16} store_latest={s['store_latest'] or '-'} "
            f"newest_event={s['newest_event_at'] or '-'} history={s['history']}"
        )
    for name, s in sorted(r["disabled"].items()):
        print(f"{name:<16} {'disabled':<16} history={s['history']}")
    for name, s in sorted(r["unwatched"].items()):
        print(f"{name:<16} {'unwatched':<16} newest_event={s['newest_event_at'] or '-'}")
    sk = r["skips"]
    if sk["total"]:
        print(
            f"skips: {sk['total']} ledger records, {sk['recent']} in last "
            f"{sk['days']:.0f}d ({sk['recent_lines']} lines) — capture-skips.jsonl"
        )
    for msg in r["warnings"]:
        print(f"warning: {msg}")
    if r["ok"]:
        print("OK")
    else:
        print("FAILED:")
        for msg in r["failed"]:
            print(f"  {msg}")
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

    from ._importers import PROVIDERS  # registry is the single source of truth for choices

    p_import = sub.add_parser("import", help="import a transcript or provider store")
    _add_home_arg(p_import)
    p_import.add_argument("path", help="transcript file (claude-code/codex/grok/antigravity/cloth) or DB (cursor/opencode)")
    p_import.add_argument(
        "--provider",
        default=None,
        choices=PROVIDERS,
        help="source provider (default: claude-code)",
    )
    p_import.set_defaults(func=cmd_import)

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
    p_watch.add_argument("--web-host", default="127.0.0.1", help="cohosted viewer bind host")
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

    p_status = sub.add_parser("status", help="archive health / paths / counts")
    _add_home_arg(p_status)
    p_status.set_defaults(func=cmd_status)

    p_backup = sub.add_parser("backup", help="mirror the JSONL truth dir to a backup destination")
    _add_home_arg(p_backup)
    p_backup.add_argument("dest", help="backup destination dir (ideally a different disk/machine)")
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

    p_nightly = sub.add_parser(
        "nightly",
        help="scheduled pipeline: backup → verify (age-gated deep/hashes "
             "escalation) → restore drill, with per-stage health records",
    )
    _add_home_arg(p_nightly)
    p_nightly.add_argument("dest", help="backup destination dir (a different disk/machine)")
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

    p_daemon = sub.add_parser(
        "daemon",
        help="manage the always-on watcher LaunchAgent (macOS): the upgrade "
             "from lazy MCP-cohosted ingest to always-fresh",
    )
    _add_home_arg(p_daemon)
    p_daemon.add_argument(
        "action", choices=["install", "uninstall", "restart", "status"],
        help="install writes the plist (pointing at this environment's `archive`) "
             "and (re)loads the agent; restart applies a code edit to the running agent",
    )
    p_daemon.add_argument(
        "--no-web", dest="web", action="store_false",
        help="don't cohost the web viewer in the watcher process",
    )
    p_daemon.add_argument(
        "--web-port", type=int, default=8787, help="cohosted viewer port (default 8787)"
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
