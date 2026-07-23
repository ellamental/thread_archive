"""``python -m thread_archive._mine`` — the mining entry point.

Two paths, kept apart so the hot one stays cheap. ``tool ...`` is the corpus seam
the mining agents shell into on every search/read; it imports only
:mod:`._corpus`, never the miner registry. Anything else is the operator command,
delegated to :mod:`._cli`.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "tool":
        from ._corpus import main as tool_main

        return tool_main(argv[1:])
    from ._cli import dispatch

    return dispatch(argv)


if __name__ == "__main__":
    sys.exit(main())
