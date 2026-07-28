"""Taking the archive off a machine — the flow behind ``thread-archive uninstall``.

The inverse of :mod:`.wizard`, and only of the wizard. Setup is the one part of
the archive that acts on the machine *outside* the archive home: it schedules
the service agents, wires the MCP server into a client, and leaves behind a
family manifest, a monitor heartbeat and a record of what it did. This removes
exactly that set, so the machine stops running an archive.

**The conversations are never touched.** The truth log, the index, the source
policy in ``config.json``, the logs, the drop zone and the retained exports all
stay where they are, and ``thread-archive search`` / ``thread-archive read``
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


@dataclass(frozen=True)
class Location:
    """One place this archive's data sits, after the machinery is gone.

    The closing report is a list of these because a single "the archive is at
    <home>" line is a half-truth on most installs: a backup mirror is a full copy
    of the conversations, a truth dir can be pointed outside the home, and a
    ``restore --replace`` sets the previous home aside under a new name. Someone
    deleting the home believing that was all of it would be wrong on any of the
    three.
    """

    label: str
    path: str
    note: str = ""


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


def _health(home: Optional[str]) -> dict:
    """This home's health records, or ``{}`` — read straight off disk rather than
    through ``_ops.health``, whose reader resolves the *env's* home and would
    answer about a different archive than the one being uninstalled."""
    try:
        health = json.loads(
            (resolve_paths(home).home / "health.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    return health if isinstance(health, dict) else {}


def _backup_dests(home: Optional[str]) -> list[str]:
    """Every backup destination this archive has recorded, deduplicated.

    Every record that names one, not just the last backup's: a box can mirror to
    more than one place (a working copy and an external disk), and the one that
    ran least recently is exactly the copy someone forgets they have.
    """
    dests: dict[str, None] = {}
    for _key, record in sorted(_health(home).items()):
        dest = record.get("dest") if isinstance(record, dict) else None
        if isinstance(dest, str) and dest:
            dests.setdefault(dest, None)
    return list(dests)


def leftovers(
    home: Optional[str] = None, *, extra_dests: tuple[Optional[str], ...] = ()
) -> list[Location]:
    """Every place this archive's data is, once the machinery is gone.

    ``extra_dests`` are destinations known to the caller but not to the records —
    the scheduled backup agent's, read off its manifest before the removal takes
    it, which is the only evidence a backup that was scheduled but has not yet
    run leaves anywhere.

    Existence is probed, sizes are not: a mirror can be on a disk that is slow,
    unmounted, or across a network, and a closing report must not walk one.
    """
    from .._ops.disk import disk_usage, format_bytes

    paths = resolve_paths(home)
    disk = disk_usage(home=home)
    found: list[Location] = [Location(
        "the archive", str(paths.home),
        f"{format_bytes(disk['total_bytes'])} — conversations, index, config, "
        "logs, exports" if disk["total_bytes"] else "empty",
    )]
    # A truth dir or index pointed outside the home is part of this archive and
    # would survive deleting it.
    for path in disk["external"]:
        found.append(Location("  ↳ outside the home", path))

    for dest in (*extra_dests, *_backup_dests(home)):
        if not dest or any(loc.path == dest for loc in found):
            continue
        present = Path(dest).expanduser().exists()
        found.append(Location(
            "a backup mirror", dest,
            "" if present else "not present right now — an unmounted disk?",
        ))

    # `restore --replace` preserves the home it displaced rather than deleting it.
    for damaged in sorted(paths.home.parent.glob(paths.home.name + ".damaged-*")):
        found.append(Location("a set-aside home", str(damaged), "kept by a restore"))

    compat = Path.home() / ".thread_archive"
    if compat.is_symlink():
        # Another name for an archive, not another copy of one — said plainly, so
        # nobody reads it as data to delete or as a home they still have.
        found.append(Location(
            "a compat path", str(compat), f"a symlink to {compat.readlink()}"
        ))
    elif compat.exists():
        found.append(Location("a compat path", str(compat)))
    return found


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


def _report_leftovers(home: Optional[str], extra_dests: tuple[Optional[str], ...]) -> None:
    """Where the data is — the list someone finishing the removal by hand works
    from, and the list that makes "untouched" checkable rather than a claim."""
    found = leftovers(home, extra_dests=extra_dests)
    width = max(len(loc.label) for loc in found)
    _say("Your conversations are untouched. They are still on this machine, here:")
    for loc in found:
        _say(f"  {loc.label:<{width}}  {loc.path}" + (f"  ({loc.note})" if loc.note else ""))
    _say("  Deleting any of it is yours to do — nothing here does it for you.")


def _report_finish(home: Optional[str], blocked: list[Item]) -> None:
    """What is left to do to be rid of the archive entirely: the code, and
    anything the run reported but would not touch."""
    from .._update import install_repo

    _say("To finish the removal:")
    repo = install_repo()
    if repo is not None:
        _say(f"  the code:   this install runs from the clone {repo} — delete that "
             "directory (its venv goes with it)")
    else:
        _say("  the code:   pip uninstall thread-archive  (the archive above outlives it)")
    for item in blocked:
        _say(f"  {item.label}: left in place — {item.blocked}")
    _say("  to return:  thread-archive setup, any time — it picks the archive above "
         "up as it is.")


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
    # Read before anything is removed: a backup that is scheduled but has not yet
    # run is recorded nowhere except the agent this run is about to take away.
    scheduled_dest = (machine.backup_dest(),)

    _say("thread-archive uninstall — remove the archive's machinery from this machine.")
    _say("  Your conversations are not touched.")
    _say()
    items = survey(args.home, machine=machine)
    _report(items)
    _say()

    targets = [i for i in items if i.removable]
    blocked = [i for i in items if i.blocked]
    if not targets:
        _say("Nothing installed — this machine runs no archive machinery.")
        _say()
        _report_leftovers(args.home, scheduled_dest)
        _say()
        _report_finish(args.home, blocked)
        return 0
    if args.dry_run:
        _report_leftovers(args.home, scheduled_dest)
        _say()
        _say(f"Dry run — {len(targets)} item(s) would be removed. Nothing was changed.")
        return 0
    if not interactive and not args.yes:
        # No terminal to confirm in. Unlike the wizard's greeting this is a
        # refusal to act, so it exits nonzero: a script that meant to uninstall
        # must not read "nothing happened" as success.
        _say("No terminal to confirm in — nothing was removed.")
        _say("  `thread-archive uninstall --yes` removes the items above without asking.")
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
        _say("  `thread-archive uninstall` again once the reason above is cleared.")
        return 1
    _say("Uninstalled. Nothing on this machine captures new conversations now.")
    _say()
    _report_leftovers(args.home, scheduled_dest)
    _say()
    _report_finish(args.home, blocked)
    return 0
