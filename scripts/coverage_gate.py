"""Per-package coverage floors over a pytest-cov JSON report.

A single global threshold would be noise here: packages differ in how much of
their surface a suite can reach, and a lone aggregate lets a sharp drop in one
package hide behind slack in another — the packages that guard the
memory-of-record (truth, store, importers) must never quietly lose coverage. So
each top-level package gets its own floor, set well under its measured branch
coverage (roughly five to ten points) — a ratchet against real regression, not
an aspiration, with enough slack that ordinary edits don't red the row. When
real tests push a package up, the floor may follow, keeping that slack.

The floors ratchet *behavioral* coverage. A floor set so high that holding it
means enumerating presentation branches — every warning line of every operator
report, every formatter unit — buys test bulk, not protection, and the no-mock
rule makes that bulk expensive. Packages whose uncovered remainder is
presentation or an OS boundary (cli's report functions, launchd/systemd edges)
carry deliberately looser floors than the memory-of-record packages.

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
    "_api": 85.0,  # thin dispatch layer over the private machinery
    # CLI→shared-server delegation: the wire, every fallback shape, eligibility.
    # Small and fully driven by real loopback servers; measures 100.
    "_delegate": 95.0,
    # The shipped manual's resolver: both locations, both readers, and the
    # rejections of slugs that would reach outside the docs directory.
    "_docs": 90.0,
    "_importers": 87.0,
    "_knowledge": 85.0,
    # The MCP transport shim: bind plan, cohosted ingest, tool registration. What
    # it does NOT hold is the tools themselves (`_tools`, floored below), and the
    # small remainder here is mostly `main()` — the blocking serve loop, proved by
    # real subprocesses in the package lane, where nothing is measured.
    "_mcp": 77.0,
    "_ops": 85.0,  # the durability kit
    "_providers": 85.0,
    "_repair": 85.0,
    "_retrieval": 89.0,
    "_service": 83.0,  # the daemon service backends (launchd + systemd) behind one registry
    "_setup": 89.0,
    "_store": 90.0,
    "_thread_import": 87.0,  # vendored provider parsers, exercised end-to-end by the parser + golden suites
    "_tools": 92.0,  # thread_search / thread_read themselves — driven from both doors (MCP + CLI)
    "_truth": 88.0,
    "_update": 75.0,
    "_viewer": 90.0,  # one probe; both answers are driven
    "_watcher": 89.0,
    # The viewer is dev-only (excluded from the wheel), and stays floored because
    # a checkout is where it runs — this machine's watcher cohosts it.
    "_web": 87.0,
    # Verb→api dispatch plus one representative failure shape per operator
    # report. The uncovered remainder is report-formatter branches — warning
    # lines, samples, unit formatters — which the floor deliberately does not
    # demand (see the module docstring).
    "cli": 83.0,
    "provider": 71.0,  # public plugin API + the pytest harness (dogfooded by the golden suite)
    "TOTAL": 88.0,
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
