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
        help="archive home dir (default: $THREAD_ARCHIVE_HOME or ~/.thread_archive)",
    )


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
    watcher = Watcher(interval=args.interval)

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

    available = [w.source_name for w in watcher.available()]
    logging.getLogger("thread_archive.watcher").info(
        "watching %d sources %s every %ss (maintenance every %.0fs)",
        len(available), available, args.interval, watcher.maintenance_interval,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
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
        include_thinking=args.thinking, include_tools=not args.no_tools,
    ))
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    from . import api

    paths = resolve_paths(args.home)
    print(f"reindexing {paths.index_path} from {paths.truth_dir} (vectors={args.vectors})")
    counts = api.reindex(home=args.home, vectors=args.vectors)
    for name, n in counts.items():
        print(f"  {name:16} {n:>9}")
    print("done")
    return 0


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

    p_import = sub.add_parser("import", help="import a transcript or provider store")
    _add_home_arg(p_import)
    p_import.add_argument("path", help="transcript file (claude-code/codex/grok/antigravity) or DB (cursor/opencode)")
    p_import.add_argument(
        "--provider",
        default=None,
        choices=["claude-code", "codex", "grok", "antigravity", "cursor", "opencode"],
        help="source provider (default: claude-code)",
    )
    p_import.set_defaults(func=cmd_import)

    p_watch = sub.add_parser("watch", help="watch local AI-tool stores and import incrementally")
    _add_home_arg(p_watch)
    p_watch.add_argument("--once", action="store_true", help="poll once and exit")
    p_watch.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    p_watch.set_defaults(func=cmd_watch)

    p_search = sub.add_parser("search", help="search conversations")
    _add_home_arg(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    p_read = sub.add_parser("read", help="read a conversation")
    _add_home_arg(p_read)
    p_read.add_argument("thread_id", type=int)
    p_read.add_argument("--thinking", action="store_true", help="include assistant thinking blocks")
    p_read.add_argument("--no-tools", action="store_true", help="omit tool calls and results")
    p_read.set_defaults(func=cmd_read)

    p_reindex = sub.add_parser("reindex", help="rebuild index.db from the JSONL truth directory")
    _add_home_arg(p_reindex)
    p_reindex.add_argument("--vectors", action="store_true", help="also rebuild local vectors")
    p_reindex.set_defaults(func=cmd_reindex)

    p_status = sub.add_parser("status", help="archive health / paths / counts")
    _add_home_arg(p_status)
    p_status.set_defaults(func=cmd_status)

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
