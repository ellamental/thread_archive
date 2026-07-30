"""The viewer probe, and the surface that hangs off it.

The viewer is dev-only: ``thread_archive._web`` and its built bundle are
excluded from the wheel, so a checkout has them and an install does not. Every
place that would offer the viewer asks :func:`viewer_available` first. These
tests pin both answers — the checkout's, which is real here, and the install's,
which this tree can only reach through the seams the callers expose for it
(``build_parser(has_viewer=…)``, ``watcher_spec(has_viewer=…)``, and the
probe's own ``find_spec`` argument).

The install-shaped assertions matter because the failure they prevent is not a
missing feature. A CLI that registers ``watch --web`` without a viewer writes a
launchd/systemd unit carrying a flag its own argparse rejects, and the service
manager restart-loops on exit 2.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thread_archive._service.spec import watcher_spec
from thread_archive._viewer import viewer_available
from thread_archive.cli import build_parser


def _subcommands(parser) -> dict:
    (action,) = [a for a in parser._actions if a.choices and hasattr(a.choices, "keys")]
    return action.choices


# ── the probe ────────────────────────────────────────────────────────────────

@pytest.mark.viewer
def test_probe_finds_the_viewer_in_a_checkout() -> None:
    # No seam here on purpose: from the source tree, where _web is present, the
    # real probe must say so — the same tree this machine's watcher runs from.
    # (Marked, so the install lane stands it down rather than contradicting it;
    # the install-shaped answer is asserted in the package lane instead.)
    assert viewer_available() is True


def test_probe_asks_for_the_viewer_by_its_full_name() -> None:
    asked: list[str] = []

    def finder(name: str):
        asked.append(name)
        return object()

    assert viewer_available(finder) is True
    assert asked == ["thread_archive._web"]


def test_probe_says_no_when_the_module_is_absent() -> None:
    assert viewer_available(lambda name: None) is False


@pytest.mark.parametrize("boom", [ImportError("parent won't load"), ValueError("no spec")])
def test_probe_says_no_rather_than_raising(boom) -> None:
    """A parent package that won't import, or a spec-less namespace entry.

    Both mean "no viewer here", and neither should take the CLI down on start —
    the probe runs while the parser is being built, before argv is even read.
    """
    def explode(name: str):
        raise boom

    assert viewer_available(explode) is False


# ── what the probe gates: the command tree ───────────────────────────────────

def test_install_shaped_cli_drops_the_whole_viewer_surface() -> None:
    parser = build_parser(has_viewer=False)

    assert "web" not in _subcommands(parser)
    help_text = parser.format_help()
    assert "--web" not in help_text
    assert "watch --web" not in help_text  # nor the epilog line pointing at it

    # `watch` still exists and still runs; it just has no viewer flags to take.
    args = parser.parse_args(["watch"])
    assert not hasattr(args, "web")


def test_install_shaped_cli_still_lists_the_verbs_it_kept() -> None:
    # Dropping `web` must not disturb the sectioned listing around it — the
    # renderer walks a section table that still names it.
    help_text = build_parser(has_viewer=False).format_help()
    for kept in ("search", "read", "watch", "status", "service"):
        assert kept in help_text, kept
    assert "commands:" in help_text


def test_install_shaped_cli_keeps_service_install_runnable() -> None:
    # cmd_daemon reads .web/.web_port unconditionally, so they must resolve as
    # defaults when the flags aren't registered — an AttributeError here would
    # be a crash on the verb that sets the machine up.
    args = build_parser(has_viewer=False).parse_args(["service", "install"])
    assert args.web is False
    assert args.web_port == 8787


def test_checkout_cli_keeps_the_viewer_surface() -> None:
    parser = build_parser(has_viewer=True)
    assert "web" in _subcommands(parser)
    assert parser.parse_args(["watch", "--web"]).web is True
    assert "watch --web" in parser.format_help()


def test_default_parser_matches_this_tree() -> None:
    # The no-argument call is what `main()` makes, so the seam must not change
    # the answer a real run gets.
    assert ("web" in _subcommands(build_parser())) is viewer_available()


# ── what the probe gates: the unit it writes ─────────────────────────────────

def test_unit_argv_carries_no_web_flag_without_a_viewer() -> None:
    # Even when the caller asks for it: the request is the caller's, the
    # capability is the installation's.
    spec = watcher_spec(Path("/opt/bin/thread-archive"), Path("/tmp/logs"),
                        web=True, has_viewer=False)
    assert "--web" not in spec.argv
    assert spec.argv[-1] == "watch"


def test_unit_argv_carries_the_web_flag_where_the_viewer_is() -> None:
    spec = watcher_spec(Path("/opt/bin/thread-archive"), Path("/tmp/logs"),
                        web=True, web_port=8787, has_viewer=True)
    assert "--web" in spec.argv and "8787" in spec.argv


def test_a_caller_that_declines_the_viewer_still_gets_no_flag() -> None:
    spec = watcher_spec(Path("/opt/bin/thread-archive"), Path("/tmp/logs"),
                        web=False, has_viewer=True)
    assert "--web" not in spec.argv
