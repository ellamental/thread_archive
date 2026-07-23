"""``thread_archive mine`` — the operator front door to the gold miners.

Three shapes, all driven off the registry so there is no second catalog to keep
in sync:

- ``thread_archive mine`` — the list view: every miner, what it measures, its
  unit and cost, and whether ``mine all`` can drive it.
- ``thread_archive mine <miner> [args]`` — run one miner; ``--help`` shows its
  options.
- ``thread_archive mine all [N]`` — run every per-case miner that needs only a
  count, with target N; miners that need an argument (topic) are skipped, named.

Every path guards the same two preconditions once — the ``claude`` CLI is on
PATH, and the home is a frozen snapshot — because a miner that got half-run
against the live archive would write cases bound to a corpus that keeps moving.
"""

from __future__ import annotations

import argparse
import sys

from . import _framework as fw


def _miner_parser(miner: fw.Miner) -> argparse.ArgumentParser:
    """A miner's full argument parser: the framework's common args plus the miner's
    own. Built the same way for the run path and for ``mine all``'s defaulting."""
    p = argparse.ArgumentParser(prog=f"thread_archive mine {miner.name}",
                                description=miner.summary)
    fw.add_common_arguments(p, miner)
    miner.add_arguments(p)
    return p


def list_miners_text(registry: list[fw.Miner]) -> str:
    """The list view — one aligned row per miner, then a per-miner detail block."""
    name_w = max((len(m.name) for m in registry), default=4)
    meas_w = max((len(m.measures) for m in registry), default=8)
    lines = ["Gold miners — mint snapshot-bound eval cases (thread_archive mine <miner> ...)", ""]
    for m in registry:
        target = f"{m.unit}×N" if m.target_kind == "per-case" else "batch"
        flag = "● mine all" if (m.runnable_in_all and m.target_kind == "per-case") else "○ direct"
        lines.append(
            f"  {m.name:<{name_w}}  {m.measures:<{meas_w}}  {target:<8}  {flag}")
    lines.append("")
    for m in registry:
        lines.append(f"  {m.name}: {m.summary}")
        lines.append(f"      cost {m.cost}; target: {m.target_help}")
    lines += [
        "",
        "  ● = `thread_archive mine all [N]` runs it with target N (default 5);",
        "  ○ = run it directly (it needs an argument or sizes itself).",
        "  Each run spends real `claude` tokens against a frozen snapshot",
        "  (`thread_archive snapshot <dir>`; point THREAD_ARCHIVE_HOME at it).",
        "  `thread_archive mine <miner> --help` for a miner's own options.",
    ]
    return "\n".join(lines)


def _guarded_open() -> str:
    """Open the archive, assert the preconditions, and return the snapshot id.
    Shared by the single-miner and ``all`` paths so both fail the same way."""
    from .. import _api as api

    if not fw.claude_available():
        raise SystemExit("mining needs the `claude` CLI on PATH")
    api.open_archive()
    return fw.require_snapshot()


def _execute(miner: fw.Miner, args: argparse.Namespace,
             snapshot_id: str) -> fw.MineResult:
    """Run one miner against an already-opened, already-verified snapshot."""
    ctx = fw.MineContext(
        snapshot_id=snapshot_id, target=getattr(args, "target", 0),
        model=args.model, jobs=args.jobs, tool_cmd=fw.tool_cmd(), args=args)
    return miner.run(ctx)


def _print_result(miner: fw.Miner, result: fw.MineResult) -> None:
    print(f"done: {result.written} case(s) written, {result.failed} failed "
          f"({miner.name} -> {result.cases_path})")
    if result.detail_path:
        print(f"  detail: {result.detail_path}")
    for note in result.notes:
        print(f"  note: {note}")


def _run_all(registry: list[fw.Miner], argv: list[str], open_fn=_guarded_open) -> int:
    """``mine all [N]`` — every per-case miner that needs only a count, in turn.
    Batch/arg-required miners are named and skipped rather than silently dropped.
    ``open_fn`` is the precondition/snapshot seam (default :func:`_guarded_open`)."""
    ap = argparse.ArgumentParser(prog="thread_archive mine all")
    ap.add_argument("target", nargs="?", type=int, default=5,
                    help="per-miner target (default 5)")
    ap.add_argument("--model", default=None, help="override every miner's model")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    top = ap.parse_args(argv)

    runnable = [m for m in registry
                if m.runnable_in_all and m.target_kind == "per-case"]
    skipped = [m for m in registry if m not in runnable]

    print(f"mine all: {len(runnable)} miner(s) at target={top.target}; "
          f"{len(skipped)} skipped")
    for m in skipped:
        why = ("needs an argument (run it directly)" if not m.runnable_in_all
               else f"{m.target_kind} miner")
        print(f"  skip {m.name}: {why}")

    snapshot_id = open_fn()
    total_written = total_failed = 0
    for miner in runnable:
        print(f"\n── {miner.name} ──")
        sub_argv = ["--target", str(top.target), "--jobs", str(top.jobs),
                    "--seed", str(top.seed)]
        if top.model:
            sub_argv += ["--model", top.model]
        args = _miner_parser(miner).parse_args(sub_argv)
        try:
            result = _execute(miner, args, snapshot_id)
        except SystemExit as exc:
            # One miner having nothing to mine must not abort the sweep.
            print(f"  {miner.name}: {exc}")
            continue
        _print_result(miner, result)
        total_written += result.written
        total_failed += result.failed
    print(f"\nmine all done: {total_written} case(s) written across "
          f"{len(runnable)} miner(s), {total_failed} failed")
    return 0


def dispatch(argv: list[str], *, registry: list[fw.Miner] | None = None,
             open_fn=_guarded_open) -> int:
    """Run the ``mine`` command line. ``registry`` and ``open_fn`` are injection
    seams (default the real registry and the claude/snapshot guard), so a test can
    drive the run paths with fake miners and a stub snapshot."""
    if registry is None:
        from . import load_registry

        registry = load_registry()
    by_name = {m.name: m for m in registry}

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(list_miners_text(registry))
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd == "all":
        return _run_all(registry, rest, open_fn)
    if cmd not in by_name:
        print(f"unknown miner {cmd!r}. Available: {', '.join(by_name)} (or 'all').",
              file=sys.stderr)
        print("\n" + list_miners_text(registry), file=sys.stderr)
        return 2

    miner = by_name[cmd]
    args = _miner_parser(miner).parse_args(rest)
    snapshot_id = open_fn()
    result = _execute(miner, args, snapshot_id)
    _print_result(miner, result)
    return 0
