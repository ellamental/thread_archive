"""``python -m search_lab <command>`` — the lab's front door.

Three commands, because three things in here are about the bench *as a set*
rather than a single instrument, and are worth invoking by name:

* ``benchmark`` — run the bench as a set, skipping what is already measured at
  this configuration (:mod:`search_lab.benchmark`).
* ``gate`` — decide whether what it measured is releasable, against the frozen
  accepted numbers (:mod:`search_lab.quality_gate`).
* ``pins`` — check the corpora themselves against the bytes they were accepted
  on, which is what makes a number comparable to an older one at all
  (:mod:`search_lab.dataset_pins`).

Every other harness stays a script (``python search_lab/retrieval_eval.py``),
which is how they are documented and how they are run: one instrument, its own
flags, its own docstring as the manual.
"""

from __future__ import annotations

import sys

COMMANDS = ("benchmark", "gate", "pins")


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
    if command == "gate":
        from search_lab import quality_gate

        return quality_gate.main(rest)
    if command == "pins":
        from search_lab import dataset_pins

        return dataset_pins.main(rest)
    print(f"unknown command {command!r}; expected one of {', '.join(COMMANDS)}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
