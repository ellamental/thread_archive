"""Taking the archive off a machine — the flow behind ``thread_archive uninstall``.

The inverse of :mod:`.wizard`, and only of the wizard. Setup is the one part of
the archive that acts on the machine *outside* the archive home: it schedules
the service agents, wires the MCP server into a client, and leaves behind a
family manifest, a monitor heartbeat and a record of what it did. This removes
exactly that set, so the machine stops running an archive.

**The conversations are never touched.** The truth log, the index, the source
policy in ``config.json``, the logs, the drop zone and the retained exports all
stay where they are, and ``thread_archive search`` / ``thread_archive read``
keep answering from them with nothing installed. Deleting an archive is a
deletion; it is the operator's to perform, on a directory this command names, and
it is not something a word like "uninstall" gets to do on their behalf.

An agent or a client entry pointing at a *different* archive home is left alone
and said so: the scheduled agents are per-user and one label each, so the one on
this box may belong to an archive other than the one being uninstalled — taking
it out would stop a second archive's capture on the way past.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .._config import config_path, load_config, resolve_paths, save_config
from .machine import Machine
from .wizard import _ask, _interactive, _say

# The scheduled agents, in report order, under the names a person knows them by.
AGENTS = (
    ("watcher", "the always-on watcher"),
    ("mcp", "the shared MCP server"),
    ("backup", "the nightly backup"),
)

# The word for a piece that is there, and the word for one that isn't.
_STATE_WORDS = {
    "agent": ("installed", "not installed"),
    "client": ("wired", "not wired"),
}


@dataclass
class Item:
    """One piece of machine footprint, and what is true of it right now.

    ``present`` is whether it exists at all. ``blocked`` names the reason it has
    to be left where it is — it belongs to another archive — which is a fact to
    report, never a failure.
    """

    kind: str  # "agent" | "client" | "manifest" | "heartbeat" | "record"
    name: str  # the logical id within the kind ("watcher", "claude", …)
    label: str  # what a person calls it
    detail: str  # where it lives
    present: bool = False
    blocked: Optional[str] = None

    @property
    def removable(self) -> bool:
        return self.present and not self.blocked

    @property
    def state(self) -> str:
        if self.blocked:
            return "kept"
        yes, no = _STATE_WORDS.get(self.kind, ("present", "absent"))
        return yes if self.present else no


# ── the survey ───────────────────────────────────────────────────────────────


def survey(home: Optional[str] = None, *, machine: Optional[Machine] = None) -> list[Item]:
    """Everything this machine carries for the archive at ``home``, read-only.

    Nothing here opens the archive or creates a directory: asking what an
    uninstall would remove must never be the thing that scaffolds a home.
    """
    from . import clients

    machine = machine if machine is not None else Machine()
    items: list[Item] = []

    for agent, label in AGENTS:
        item = Item("agent", agent, label, machine.agent_label(agent))
        if machine.agent_installed(agent):
            item.present = True
            if not machine.agent_covers_home(agent, home):
                item.blocked = (
                    f"it serves {machine.agent_home(agent) or 'the default archive'}, "
                    "not this archive"
                )
        items.append(item)

    cli = clients.claude_cli()
    entry = Item("client", "claude", "the claude MCP wiring", clients.SEARCH_SERVER)
    if cli is not None:
        entry.present, entry.blocked = clients.claude_removable(cli, home=home)
    items.append(entry)

    paths = resolve_paths(home)
    manifest = paths.home / "product.json"
    items.append(Item(
        "manifest", "product", "the family manifest", str(manifest),
        present=manifest.exists(),
    ))

    from .._ops.health import heartbeat_path

    beat = heartbeat_path()
    heartbeat = Item(
        "heartbeat", "nightly", "the monitor heartbeat", str(beat), present=beat.exists()
    )
    # One heartbeat per box, stamped by the nightly agent. When that agent belongs
    # to another archive the beat is its report, not ours — removing it would tell
    # the family monitor a running pipeline had never run.
    backup = next(i for i in items if i.name == "backup")
    if heartbeat.present and backup.blocked:
        heartbeat.blocked = "the backup agent that stamps it serves another archive"
    items.append(heartbeat)

    cfg = load_config(home) if config_path(home).exists() else None
    items.append(Item(
        "record", "setup", "the install record",
        f"{config_path(home)} (the setup block; source choices stay)",
        present=bool(cfg and cfg.get("setup")),
    ))
    return items


# ── the removals ─────────────────────────────────────────────────────────────


def _remove(item: Item, home: Optional[str], machine: Machine) -> list[str]:
    """Perform one removal. Returns error strings, empty on success."""
    from . import clients

    if item.kind == "agent":
        try:
            machine.uninstall_agent(item.name)
        except SystemExit as e:  # the service manager refused
            return [f"{item.label}: {e}"]
        return []
    if item.kind == "client":
        cli = clients.claude_cli()
        if cli is None:  # pragma: no cover — surveyed present, so it was there
            return [f"{item.label}: the claude CLI is no longer on PATH"]
        errors = clients.unwire_claude(cli)
        if errors:
            return errors
        # A removal claude reported as done, that left the entry answering, is the
        # one outcome this command must not print as "removed".
        present, _ = clients.claude_removable(cli, home=home)
        return (
            [f"{item.label}: claude still has an entry for this archive"]
            if present else []
        )
    if item.kind in ("manifest", "heartbeat"):
        try:
            Path(item.detail).unlink(missing_ok=True)
        except OSError as e:
            return [f"{item.label}: {e}"]
        return []
    # The install record: drop setup's block, keep everything else. The source
    # policy in the same file is a privacy choice, not install state — an
    # uninstall that dropped it would re-enable, on the next install, exactly the
    # sources someone had turned off.
    cfg = load_config(home)
    if not cfg.get("setup"):  # pragma: no cover — surveyed present
        return []
    cfg.pop("setup")
    try:
        save_config(cfg, home)
    except OSError as e:
        return [f"{item.label}: {e}"]
    return []


# ── the report ───────────────────────────────────────────────────────────────


def _report(items: list[Item]) -> None:
    width = max(len(i.label) for i in items)
    for item in items:
        _say(f"  {item.label:<{width}}  {item.state:<13}  {item.detail}")
        if item.blocked:
            _say(f"  {'':<{width}}  {'':<13}  left alone: {item.blocked}")


def _backup_mirror(home: Optional[str]) -> Optional[str]:
    """Where a backup of these conversations also lives, if one was ever made —
    so "delete the home" is never read as "delete the last copy". Best-effort:
    an unreadable health file costs this line and nothing else."""
    try:
        health = json.loads(
            (resolve_paths(home).home / "health.json").read_text(encoding="utf-8")
        )
        dest = health["backup_last"]["dest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return dest if isinstance(dest, str) else None


def _report_kept(home: Optional[str]) -> None:
    """The archive itself: where it is, what it costs, and who still reads it."""
    from .._ops.disk import disk_usage, format_bytes

    paths = resolve_paths(home)
    disk = disk_usage(home=home)
    _say("Kept — the archive itself:")
    if not disk["total_bytes"]:
        _say(f"  {paths.home}  (empty — there are no conversations here)")
        return
    _say(
        f"  {paths.home}  ({format_bytes(disk['total_bytes'])}: conversations, index, "
        "config, logs, exports)"
    )
    _say("  `thread_archive search` and `thread_archive read` keep answering from it "
         "with nothing installed.")
    mirror = _backup_mirror(home)
    if mirror:
        _say(f"  a backup mirror holds a copy too: {mirror}")


# ── the flow ─────────────────────────────────────────────────────────────────


def run_uninstall(
    args: argparse.Namespace,
    *,
    machine: Optional[Machine] = None,
    interactive: Optional[bool] = None,
    ask: Callable[..., str] = _ask,
) -> int:
    """Survey → consent → remove → say what stays. Returns exit code.

    Collaborators are keyword parameters for the same reason the wizard's are:
    ``machine`` is the host whose scheduled agents this reads and removes, and
    ``ask`` is where consent comes from — so a run can be driven end to end
    without reaching for this process's own terminal and service manager.
    """
    machine = machine if machine is not None else Machine()
    interactive = _interactive(args) if interactive is None else interactive

    _say("thread_archive uninstall — remove the archive's machinery from this machine.")
    _say("  Your conversations are not touched.")
    _say()
    items = survey(args.home, machine=machine)
    _report(items)
    _say()
    _report_kept(args.home)
    _say()

    targets = [i for i in items if i.removable]
    if not targets:
        _say("Nothing installed — this machine runs no archive machinery.")
        return 0
    if args.dry_run:
        _say(f"Dry run — {len(targets)} item(s) would be removed. Nothing was changed.")
        return 0
    if not interactive and not args.yes:
        # No terminal to confirm in. Unlike the wizard's greeting this is a
        # refusal to act, so it exits nonzero: a script that meant to uninstall
        # must not read "nothing happened" as success.
        _say("No terminal to confirm in — nothing was removed.")
        _say("  `thread_archive uninstall --yes` removes the items above without asking.")
        return 2
    answer = ask(
        f"Remove the {len(targets)} item(s) above?\n"
        "  [Enter] remove · s = cancel  > ",
        default="", interactive=interactive,
    )
    if answer in ("s", "n", "no"):
        _say("  Cancelled — nothing was removed.")
        return 0

    _say()
    failed = 0
    for item in targets:
        problems = _remove(item, args.home, machine)
        failed += bool(problems)
        _say(f"  {'FAILED' if problems else 'removed':<8}{item.label}")
        for err in problems:
            _say(f"    ! {err}")
    _say()

    if failed:
        _say(f"Uninstall incomplete — {failed} item(s) could not be removed "
             "(the rest are gone).")
        _say("  `thread_archive uninstall` again once the reason above is cleared.")
        return 1
    _say("Uninstalled. Nothing on this machine captures new conversations now.")
    _say("  to set it up again:      thread_archive setup")
    _say("  to remove the package:   pip uninstall thread-archive "
         "(the archive outlives it)")
    _say(f"  to remove the archive:   delete {resolve_paths(args.home).home} yourself — "
         "nothing here does that for you.")
    return 0
