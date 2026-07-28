"""``python -m search_lab <command>`` — the lab's front door.

One command, because one thing in here is a *set* rather than a single
instrument and is worth invoking by name:

* ``benchmark`` — run the bench as a set, skipping what is already measured at
  this configuration (:mod:`search_lab.benchmark`).

Every other harness stays a script (``python search_lab/retrieval_eval.py``),
which is how they are documented and how they are run: one instrument, its own
flags, its own docstring as the manual.
"""

from __future__ import annotations

import sys

COMMANDS = ("benchmark",)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        print(f"\nusage: python -m search_lab {{{'|'.join(COMMANDS)}}} [options]")
        return 0 if args else 1
    command, rest = args[0], args[1:]
    if command == "benchmark":
        from search_lab import benchmark

        return benchmark.main(rest)
    print(f"unknown command {command!r}; expected one of {', '.join(COMMANDS)}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
