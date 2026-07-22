"""Per-package coverage floors over a pytest-cov JSON report.

A single global threshold would be noise here: packages differ in how much of
their surface a suite can reach, and a lone aggregate lets a sharp drop in one
package hide behind slack in another — the packages that guard the
memory-of-record (truth, store, importers) must never quietly lose coverage. So
each top-level package gets its own floor, set a few points under its measured
branch coverage — a ratchet against regression, not an aspiration. When real
tests push a package up, raise its floor to follow.

Coverage percent = (covered_lines + covered_branches) / (statements + branches),
i.e. branch coverage, matching ``--cov-branch``.

Usage: python scripts/coverage_gate.py [coverage.json]
Exit 0 when every floor holds; exit 1 listing each breach.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# package (top-level dir/module under src/thread_archive/) -> minimum percent
FLOORS = {
    "_api": 90.0,  # thin dispatch layer over the private machinery
    "_eval": 95.0,  # search-quality scoring core behind `thread_archive eval` + the evals/ bench
    "_importers": 92.0,
    "_knowledge": 90.0,
    "_mcp": 92.0,
    "_ops": 90.0,  # the durability kit
    "_patterns": 80.0,  # explicit sequence-mining batch + persisted viewer report
    "_providers": 90.0,
    "_repair": 90.0,
    "_retrieval": 94.0,
    "_scripts": 97.0,
    "_service": 88.0,  # the daemon service backends (launchd + systemd) behind one registry
    "_setup": 94.0,
    "_store": 95.0,
    "_thread_import": 92.0,  # vendored provider parsers, exercised end-to-end by the parser + golden suites
    "_truth": 93.0,
    "_update": 80.0,
    "_watcher": 94.0,
    "_web": 92.0,
    "cli": 98.0,  # full verb→api dispatch coverage; heavy verbs stubbed at the api seam
    "provider": 76.0,  # public plugin API + the pytest harness (dogfooded by the golden suite)
    "TOTAL": 94.0,
}

# `__init__` re-export shims, `_config` (path/env resolution only), and
# `__main__` (the `python -m thread_archive` shim: one delegating import to
# cli.main, with the uncoverable `if __name__` guard body).
EXEMPT_PACKAGES = {"__init__", "_config", "__main__"}


def _package(path: str) -> str | None:
    rel = path.replace("\\", "/")
    marker = "src/thread_archive/"
    if marker not in rel:
        return None
    rel = rel.split(marker, 1)[1]
    return rel.split("/", 1)[0] if "/" in rel else rel.removesuffix(".py")


def main(argv: list[str]) -> int:
    report = Path(argv[1] if len(argv) > 1 else "coverage.json")
    cov = json.loads(report.read_text())

    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for path, data in cov["files"].items():
        pkg = _package(path)
        if pkg is None:
            continue
        s = data["summary"]
        agg[pkg][0] += s["covered_lines"] + s.get("covered_branches", 0)
        agg[pkg][1] += s["num_statements"] + s.get("num_branches", 0)
    totals = cov["totals"]
    agg["TOTAL"] = [
        totals["covered_lines"] + totals.get("covered_branches", 0),
        totals["num_statements"] + totals.get("num_branches", 0),
    ]

    breaches = []
    for pkg, floor in sorted(FLOORS.items()):
        covered, total = agg.get(pkg, [0, 0])
        if total == 0:
            breaches.append(f"  {pkg}: no measured lines (package renamed? update FLOORS)")
            continue
        pct = 100.0 * covered / total
        status = "ok" if pct >= floor else "BELOW FLOOR"
        print(f"  {pkg:20s} {pct:5.1f}%  (floor {floor:.0f}%)  {status}")
        if pct < floor:
            breaches.append(f"  {pkg}: {pct:.1f}% < floor {floor:.0f}%")

    unfloored = sorted(set(agg) - set(FLOORS) - EXEMPT_PACKAGES)
    if unfloored:
        breaches.append(
            f"  unfloored production packages: {', '.join(unfloored)}"
        )

    if breaches:
        print("\ncoverage regression:")
        print("\n".join(breaches))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
