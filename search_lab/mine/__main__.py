"""The mining entry point, reachable two ways::

    .venv/bin/python -m search_lab.mine <miner> ...        # from the checkout root
    .venv/bin/python <abs>/search_lab/mine/__main__.py …   # cwd-independent

Two paths through it, kept apart so the hot one stays cheap. ``tool ...`` is the
corpus seam the mining agents shell into on every search/read; it imports only
:mod:`._corpus`, never the miner registry. Anything else is the operator command,
delegated to :mod:`._cli`.

The imports below are absolute, and the checkout root goes on ``sys.path`` first,
because the second form runs this file as a script — no package context, so a
relative import has nothing to resolve against. That form is what
:func:`search_lab.mine._framework.tool_cmd` hands the agents: they run it through
Bash from whatever directory their session started in, and an absolute path is
the only spelling that does not depend on that.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])  # <checkout>/search_lab/mine
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "tool":
        from search_lab.mine._corpus import main as tool_main

        return tool_main(argv[1:])
    from search_lab.mine._cli import dispatch

    return dispatch(argv)


if __name__ == "__main__":
    sys.exit(main())
