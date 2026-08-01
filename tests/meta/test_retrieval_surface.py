"""The retrieval-surface ratchet — one tool, and every door carries all of it.

``thread_search`` / ``thread_read`` are one implementation behind two front doors
(:mod:`thread_archive._tools`), and each tool's parameter list is written out in
more than one place: the tool's own signature, the CLI's argparse flags, and the
usage ledger's record of what a call ran with. Those copies are maintained by
hand, and every way they can disagree is silent:

- **a parameter with no CLI flag** is a filter an agent can use and a person
  cannot, from a verb documented as the same tool with a terminal in front of it;
- **a flag with no parameter** is a flag that parses, succeeds, and does nothing;
- **a parameter missing from the ledger** makes its rows unreplayable — that
  ledger is the observed ground truth the retrieval evals draw from, so a scope
  it doesn't record is a measured result nobody can reproduce, and the row still
  looks complete.

``tests/test_mcp.py`` holds the fourth leg (every parameter is named in the wire
description, so a caller can find it at all). This file holds the other three, by
deriving each from the tool signature rather than from a list kept here — a
parameter added to the tool is covered the moment it exists.

Exemptions are declared below with the reason each is not a leak, and asserted in
both directions, so one that stops being true reds the suite instead of going
quietly stale.
"""

from __future__ import annotations

import argparse
import ast
import inspect
from pathlib import Path

import pytest

from thread_archive import _tools
from thread_archive.cli import build_parser

SRC = Path(__file__).resolve().parents[2] / "src" / "thread_archive"

#: The verb each tool is served by.
DOORS = {"search": _tools.thread_search, "read": _tools.thread_read}

#: Argparse destinations that belong to the *door* rather than to the search:
#: where the archive lives, which function runs, and whether to ask the warm
#: server or answer in this process.
CLI_ONLY = {"home", "func", "local"}

#: The CLI's own spelling for a tool parameter, where they differ. `read` takes
#: its ref as a positional, which reads better at a terminal than a flag on a
#: verb that is useless without one.
CLI_ALIASES = {"id": "thread_id"}

#: Tool parameters with deliberately no CLI flag.
NO_FLAG = {
    "thread_search": set(),
    # A back-compat alias for `mode`, kept resolving for a caller that pasted it
    # into a config. The verb shipped `--mode` from the start, so there is no
    # terminal spelling to keep working and a flag now would publish a second way
    # to say one thing.
    "thread_read": {"user_only"},
}

#: Search parameters the usage ledger deliberately does not carry.
NOT_RECORDED = {
    # The row's own top-level field, not one of its params.
    "query",
    # Presentation, not retrieval: both shape what a hit is rendered *with* once
    # the ranking is settled, so two calls differing only in these ran the same
    # search and a replay off the row reproduces the same hits.
    "context_lines",
    "context_events",
}


def _tool_params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


def _verb_dests(verb: str) -> set[str]:
    """Every argparse destination the verb binds, positionals included."""
    verbs = next(
        a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    ).choices
    return {a.dest for a in verbs[verb]._actions if a.dest != "help"}


def _recorded_params() -> set[str]:
    """The keys of the params dict ``thread_search`` hands the usage ledger.

    Read statically off the source. The alternative is to drive a call and read
    the row back, but the ledger records only the parameters a call actually set
    (``record_search`` drops Nones), so proving a key *can* be recorded that way
    would mean seeding a resolvable commit, pull request and thread ref — a
    fixture whose upkeep is the very thing this file exists to make unnecessary.
    """
    tree = ast.parse((SRC / "_tools.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "record_search":
            continue
        params = next((k.value for k in node.keywords if k.arg == "params"), None)
        assert isinstance(params, ast.Dict), (
            "thread_search's usage record no longer passes a dict literal as "
            "params — this ratchet reads it statically (see _recorded_params)"
        )
        return {k.value for k in params.keys if isinstance(k, ast.Constant)}
    raise AssertionError("no record_search call found in _tools.py")


@pytest.mark.parametrize("verb", sorted(DOORS))
def test_every_tool_parameter_has_a_cli_flag(verb: str) -> None:
    """The verb is the tool with a terminal in front of it, so a person is never
    offered less than an agent."""
    tool = DOORS[verb]
    exempt = NO_FLAG[tool.__name__]
    params, dests = _tool_params(tool), _verb_dests(verb)
    aliased = dests | {CLI_ALIASES[d] for d in dests if d in CLI_ALIASES}

    missing = params - aliased - exempt
    assert not missing, (
        f"thread-archive {verb}: {sorted(missing)} exist on {tool.__name__} with no "
        f"flag. The verb serves the same tool, so add the flag — or declare it in "
        f"NO_FLAG with the reason it has no terminal spelling."
    )
    assert not (exempt - params), (
        f"stale NO_FLAG entry for {verb}: {sorted(exempt - params)} is not a parameter"
    )
    assert not (exempt & aliased), (
        f"stale NO_FLAG entry for {verb}: {sorted(exempt & aliased)} has a flag after all"
    )


@pytest.mark.parametrize("verb", sorted(DOORS))
def test_every_cli_flag_reaches_the_tool(verb: str) -> None:
    """The other direction: a flag parsing into no parameter is one a person can
    type, watch exit 0, and get an unfiltered answer from."""
    tool = DOORS[verb]
    dests = {CLI_ALIASES.get(d, d) for d in _verb_dests(verb)} - CLI_ONLY
    orphans = dests - _tool_params(tool)
    assert not orphans, (
        f"thread-archive {verb}: {sorted(orphans)} parse into no {tool.__name__} "
        f"parameter, so the flag is accepted and dropped"
    )


def test_the_search_ledger_records_every_scope() -> None:
    """A measured search must be replayable from its row."""
    recorded = _recorded_params()
    params = _tool_params(_tools.thread_search)

    missing = params - recorded - NOT_RECORDED
    assert not missing, (
        f"thread_search records no {sorted(missing)} in its usage row — an eval "
        f"replaying that row would run a different search than the one measured. "
        f"Add it to the params dict, or to NOT_RECORDED with the reason it cannot "
        f"change what a replay returns."
    )
    assert not (recorded & NOT_RECORDED), (
        f"stale NOT_RECORDED entry: {sorted(recorded & NOT_RECORDED)} is recorded "
        f"after all"
    )
    # `surface` is the door the call came through — the ledger's own field, not a
    # tool parameter — so it is the one recorded key with no signature behind it.
    assert recorded - params == {"surface"}, (
        f"the usage row carries {sorted(recorded - params - {'surface'})}, which "
        f"thread_search does not take — a recorded parameter that no call can set"
    )
