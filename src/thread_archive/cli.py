"""The ``archive`` command — a thin CLI over the :mod:`thread_archive.api` surface."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import resolve_paths


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
    from . import api
    from .importers import DB_SCANNERS, LINE_STREAM_IMPORTERS

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
    from . import api
    from .importers.exports import import_export
    from .truth import shared_ingest_lock

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

    from . import api
    from .watcher import Watcher

    # Configure logging so the daemon's import/maintenance activity actually lands
    # in the log files (launchd routes stderr to watcher-stderr.log). Without this
    # the long-running process is silent.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api.open_archive(args.home)
    watcher = Watcher(
        interval=args.interval,
        embed=args.embed,
        embed_interval=args.embed_interval,
        embed_batch=args.embed_batch,
    )

    if args.once:
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
        from .web import serve_in_thread

        httpd = serve_in_thread(host=args.web_host, port=args.web_port)
        logging.getLogger("thread_archive.watcher").info(
            "cohosting web viewer on http://%s:%s", args.web_host, args.web_port
        )

    available = [w.source_name for w in watcher.available()]
    logging.getLogger("thread_archive.watcher").info(
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


def cmd_search(args: argparse.Namespace) -> int:
    from . import api
    from .retrieval import format_results

    hits = api.search(args.query, home=args.home, limit=args.limit)
    print(format_results(hits, args.query))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    from . import api

    print(api.read_thread(
        args.thread_id, home=args.home,
        limit=args.limit, offset=args.offset, summary=args.summary,
        mode=args.mode, tool_results=args.tool_results,
        max_chars=args.max_chars, after_event=args.after_event,
    ))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    serve(host=args.host, port=args.port, open_browser=not args.no_open, home=args.home)
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    from . import api

    _self_throttle()  # a rebuild is background work — don't bog the interactive machine
    paths = resolve_paths(args.home)
    print(f"reindexing {paths.index_path} from {paths.truth_dir} (vectors={args.vectors}, throttled)")
    counts = api.reindex(home=args.home, vectors=args.vectors)
    for name, n in counts.items():
        print(f"  {name:16} {n:>9}")
    print("done")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    from . import api

    print("embedding missing vectors (rebuild=%s)..." % args.rebuild, flush=True)
    res = api.embed(home=args.home, rebuild=args.rebuild, max_events=args.limit,
                    newest_first=args.newest_first)
    print(f"  embedded {res['embedded']}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from . import api

    res = api.backup(
        args.dest, home=args.home,
        allow_shrink=args.allow_shrink, verify_first=args.verify,
    )
    mb = res["bytes_copied"] / (1024 * 1024)
    print(f"backed up {res['truth_dir']} → {res['dest']}: {res['files_copied']} files ({mb:.1f} MB copied)")
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
    from . import api

    res = api.verify(home=args.home, deep=args.deep, hashes=args.hashes, backup=args.backup)
    t = res["truth"]
    print(
        f"truth: threads={t['threads']} events={t['events']} "
        f"effective={t['events_effective']} "
        f"superseded={t['duplicate_id_lines'] + t['duplicate_content_lines']} "
        f"parse_errors={t['parse_errors']}"
    )
    if t["parse_errors"]:
        print(f"       parse error sample: {t['parse_error_sample']}")
    print(
        f"index: threads={res['index']['threads']} events={res['index']['events']} "
        f"quick_check={res['index']['quick_check']}"
    )
    print(f"drift: threads={res['drift']['threads']:+d} events={res['drift']['events']:+d}")
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
            f"       kg index_only={dp['kg']['index_only']} truth_only={dp['kg']['truth_only']}; "
            f"dangling links={dp['dangling']['link_endpoints']} "
            f"citations={dp['dangling']['citation_events']} "
            f"events={dp['dangling']['event_threads']}; "
            f"dup_pairs={dp['duplicate_content_pairs_index']}"
        )
        f = dp["fts"]
        print(
            f"       fts orphans={f['orphan_rows']} "
            f"shadow={f['shadow_rows']} fts5={f['fts5_rows']} "
            f"uncovered={f['uncovered_indexable_events']}"
        )
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
                f"unhashed_keys={hs['unhashed_keys']}"
            )
            if hs["mismatched"]:
                print(f"       mismatch sample: {hs['mismatch_sample']}")
        if "delta" in h:
            d = h["delta"]
            print(
                f"hashes delta vs {h['previous']['at']}: "
                f"truth {d['truth_mismatched']:+d} index {d['index_mismatched']:+d}"
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
    print("OK" if res["ok"] else "DRIFT DETECTED")
    return 0 if res["ok"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    from . import api

    st = api.status(home=args.home)
    print(f"home:    {st['home']}")
    print(f"truth:   {st['truth_dir']}")
    print(f"index:   {st['index_path']}")
    print(f"threads: {st['threads']}")
    print(f"events:  {st['events']}")
    print(f"indexed: {st['fts_indexed']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive",
        description="Serverless-native local archive for AI conversations (JSONL truth + SQLite index).",
    )
    parser.add_argument("--version", action="version", version=f"thread-archive {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    from .importers import PROVIDERS  # registry is the single source of truth for choices

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
        "import-export", help="import a downloaded claude.ai / xAI account export (ZIP or dir)"
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

    p_search = sub.add_parser("search", help="search conversations")
    _add_home_arg(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    p_read = sub.add_parser("read", help="read a conversation")
    _add_home_arg(p_read)
    p_read.add_argument("thread_id", help="integer thread id, or a provider session uuid (source_id)")
    p_read.add_argument("--mode", choices=["user", "chat", "full"], default=None,
                        help="view: user (default) = user turns only; chat = + assistant text; full = + tool calls")
    p_read.add_argument("--summary", nargs="?", const=True, default=False,
                        help="summary view instead of full content: bare/'toc' = compact TOC "
                             "with previews; 'short' / 'indexed' = the stored thread summary")
    p_read.add_argument("--tool-results", dest="tool_results", action="store_true",
                        help="include tool output under each call (needs --mode full)")
    p_read.add_argument("--limit", type=int, default=200, help="max turns per chunk (default: 200)")
    p_read.add_argument("--offset", type=int, default=0, help="skip first N turns (negative = from end)")
    p_read.add_argument("--max-chars", dest="max_chars", type=int, default=0,
                        help="per-chunk character budget (default: ~48k)")
    p_read.add_argument("--after-event", dest="after_event", type=int, default=None,
                        help="resume from the turn after this event id")
    p_read.set_defaults(func=cmd_read)

    p_web = sub.add_parser("web", help="serve the local search + reader web UI (Ctrl-C to stop)")
    _add_home_arg(p_web)
    p_web.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    p_web.add_argument("--port", type=int, default=8787, help="bind port (default: 8787)")
    p_web.add_argument("--no-open", action="store_true", help="don't open a browser on start")
    p_web.set_defaults(func=cmd_web)

    p_reindex = sub.add_parser("reindex", help="rebuild index.db from the JSONL truth directory")
    _add_home_arg(p_reindex)
    p_reindex.add_argument("--vectors", action="store_true", help="also rebuild local vectors")
    p_reindex.set_defaults(func=cmd_reindex)

    p_embed = sub.add_parser("embed", help="embed user/text events missing a vector (incremental catch-up)")
    _add_home_arg(p_embed)
    p_embed.add_argument("--rebuild", action="store_true", help="re-embed everything, not just the gap")
    p_embed.add_argument("--limit", type=int, default=None, help="cap events embedded this run")
    p_embed.add_argument("--newest-first", action="store_true",
                         help="embed the freshest gap first (recent threads findable soonest)")
    p_embed.set_defaults(func=cmd_embed)

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
