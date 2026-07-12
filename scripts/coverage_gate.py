"""Per-package coverage floors over a pytest-cov JSON report.

A single global threshold would be noise here: the vendored parser surface
(``_thread_import``) carries large dormant provider paths that drag the
aggregate, while the packages that guard the memory-of-record (truth, store,
importers) must never quietly lose coverage. So each top-level package gets its
own floor, set a couple of points under its measured branch coverage — a
ratchet against regression, not an aspiration. When real tests push a package
up, raise its floor to follow.

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
    "_api": 81.0,  # thin dispatch layer since the _ops extraction (its old ops mass measured with it)
    "_importers": 74.0,
    "_knowledge": 85.0,
    "_mcp": 90.0,
    "_ops": 86.0,  # the durability kit (extracted from _api)
    "_retrieval": 76.0,
    "_scripts": 70.0,
    "_store": 94.0,
    "_thread_import": 49.0,  # dormant provider surfaces; kept from decaying further
    "_truth": 87.0,
    "_watcher": 72.0,
    "_web": 90.0,
    "cli": 61.0,  # verb→api dispatch tests cover the arg mapping; heavy verbs run via smoke
    "TOTAL": 71.0,
}


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

    unfloored = sorted(set(agg) - set(FLOORS) - {"__init__", "_config"})
    if unfloored:
        print(f"  (unfloored packages: {', '.join(unfloored)})")

    if breaches:
        print("\ncoverage regression:")
        print("\n".join(breaches))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
