"""The internal-layering ratchet — the package graph is a layered DAG, and stays one.

``test_dependency_tiers`` governs what the product imports from *outside* itself.
This one governs the inside: which of thread-archive's own packages may import
which. Nothing else checks it, and without a check the graph drifts in one
direction only — every package eventually imports every other, and no layer can
be read, tested, or replaced on its own.

Each package under ``src/thread_archive`` sits in a **tier**, and may import only
its own tier or a lower one. The tiers are the shape the product already has when
it is working:

- **0 — leaf policy.** Paths, config, docs, the viewer probe, the delegate. They
  answer questions; they call nothing of ours.
- **1 — the stores.** The SQLite store and the vendored parser island. They know
  rows and formats, never who is asking.
- **2 — the corpus.** Truth, retrieval, the knowledge layer: what the archive
  *holds*, over the stores.
- **3 — sources.** Providers, importers, the watcher, the ops kit — everything
  that gets conversations in and keeps the instance healthy.
- **4 — composition.** ``_api`` and ``_tools``, where a whole operation is
  assembled out of the layers below.
- **5 — install and maintenance.** Setup, update, repair, service manifests.
- **6 — front doors.** The CLI, the MCP server, the web viewer. Everything may be
  imported by these; they are imported by nothing.

Same-tier imports are allowed — peers compose — but the graph as a whole must be
**acyclic**, which is the invariant the tiers exist to serve. A cycle means two
packages that cannot be understood apart, and it is what a function-local import
buys: deferring an import to call time hides a cycle from the module loader
without removing it, so the scan below reads every import wherever it sits.
``if TYPE_CHECKING:`` imports are exempt — they do not exist at runtime and cannot
cycle.

Both baselines below freeze what exists today and may only **shrink**. They are
asserted exactly, in both directions, so they cannot go stale: an edge that gets
paid down reds the suite until its entry is lowered to match.

Paying one down means moving the shared thing to where both sides can reach it —
down a tier, not sideways. Two worked examples, both already landed: the store
needed provider session-id shapes, so the *caller* now supplies them
(``_providers.resolve_session_ref``) and the store reads no registry; retrieval
needed the front-door label for a telemetry row, so the label moved down to the
ledger that carries the field (``_retrieval.usage``) instead of retrieval
reaching up to the tool surface.

If this fails on a new import, the fix is one of: import a lower tier instead,
move the shared thing down, or invert the call so the higher layer passes what
the lower one needs.
"""

from __future__ import annotations

import ast
import collections
import sys
from pathlib import Path

import pytest


def _product_root() -> Path:
    """The nearest ancestor owning both a pyproject and a src tree."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src").is_dir():
            return parent
    raise RuntimeError(f"no product root above {__file__}")


PACKAGE = _product_root() / "src" / "thread_archive"

#: Every package under ``src/thread_archive``, and the tier it sits in. A package
#: absent from this table fails the completeness check below rather than being
#: skipped — a new one is a layering decision, and this is where it is made.
TIERS: dict[str, int] = {
    # 0 — leaf policy: answers questions, calls nothing of ours.
    "_config": 0,
    "_docs": 0,
    "_viewer": 0,
    "_delegate": 0,
    # 1 — the stores: rows and formats, never who is asking.
    "_store": 1,
    "_thread_import": 1,
    # 2 — the corpus: what the archive holds, over the stores.
    "_truth": 2,
    "_knowledge": 2,
    "_retrieval": 2,
    # 3 — sources: getting conversations in, and keeping the instance healthy.
    "provider": 3,
    "_providers": 3,
    "_importers": 3,
    "_watcher": 3,
    "_ops": 3,
    # 4 — composition: a whole operation assembled out of the layers below.
    "_api": 4,
    "_tools": 4,
    # 5 — install and maintenance.
    "_repair": 5,
    "_update": 5,
    "_service": 5,
    "_setup": 5,
    # 6 — front doors: imported by nothing.
    "cli": 6,
    "_web": 6,
    "_mcp": 6,
}

#: Imports of a *higher* tier, by edge, with the number of import sites. Frozen
#: today; may only shrink.
#:
#: What each one is, so the next reader knows which are shallow and which are
#: structural: ``_ops -> _api`` is the ops kit re-entering the composition layer to
#: open an archive it is already inside — the largest and the most mechanical.
#: ``_retrieval -> _ops`` is telemetry and ledger writes; ``_retrieval -> _api`` is
#: the same re-entry as ops'. ``_retrieval -> _providers`` / ``-> provider`` is
#: render policy, which is genuinely provider knowledge the read path needs, and
#: is the one that argues for a lower home for the policy rather than for a
#: different caller. ``_setup -> cli`` is the wizard re-running a verb.
UPWARD_BASELINE: dict[str, int] = {
    "_ops -> _api": 7,
    "_retrieval -> _ops": 4,
    "_retrieval -> _api": 2,
    "_retrieval -> _providers": 2,
    "_api -> _update": 1,
    "_retrieval -> provider": 1,
    "_setup -> cli": 1,
    "_truth -> _ops": 1,
    "_watcher -> _api": 1,
}

#: Mutually-importing package clusters. Frozen today; may only shrink — in count,
#: and in the membership of each cluster.
#:
#: The large one spans tiers 2–5 and is held together by the upward edges above:
#: clear those nine and it falls into two smaller knots, which is the order to
#: attack it in. The knots underneath are the real work. One is the source layer
#: around its own registry — the registry builds the importers and watchers, which
#: import the registry back; ``provider`` sits inside because the public plugin
#: surface both *defines* the ``Provider`` type the registry needs and *offers
#: builders* that need the importers, and splitting those two halves is what
#: unpicks it. The other is truth's rebuild reaching into retrieval to reproject.
#:
#: ``_setup``/``cli`` is separate and shallow: the wizard re-runs a CLI verb.
CYCLE_BASELINE: set[frozenset[str]] = {
    frozenset({"_api", "_importers", "_ops", "_providers", "_repair", "_retrieval",
               "_truth", "_update", "_watcher", "provider"}),
    frozenset({"_setup", "cli"}),
}


def _package_of(path: Path) -> str:
    """The tiered package a module belongs to.

    A module directly under ``thread_archive/`` (``cli.py``, ``_api.py``) is its
    own package for tiering purposes — it is a unit that can be imported, and the
    graph is about units.
    """
    rel = path.relative_to(PACKAGE)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _type_checking_imports(tree: ast.AST) -> set[int]:
    """Import nodes under ``if TYPE_CHECKING:`` — exempt, they never run."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        named = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not named:
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    exempt.add(id(sub))
    return exempt


def _targets(node: ast.stmt, module_path: Path) -> list[str]:
    """The tiered package(s) an import statement names, ours only.

    Both spellings reach the same graph: an absolute ``thread_archive.x`` import,
    and the relative form the package actually uses. For a relative import, the
    package is the first component of the module for ``from ..x import y``, or the
    imported names themselves for a bare ``from .. import x``.
    """
    if isinstance(node, ast.Import):
        return [
            a.name.split(".")[1]
            for a in node.names
            if a.name.startswith("thread_archive.") and len(a.name.split(".")) > 1
        ]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        # Relative. Level 1 from a submodule is intra-package (no edge); level 1
        # from a top-level module, or level 2+, crosses into a sibling package.
        if node.level == 1 and module_path.parent != PACKAGE:
            return []
        if node.module:
            return [node.module.split(".")[0]]
        return [a.name for a in node.names]
    if node.module and node.module.startswith("thread_archive"):
        parts = node.module.split(".")
        return [parts[1]] if len(parts) > 1 else []
    return []


def _graph() -> collections.Counter:
    """Cross-package import edges, with a count of import sites each."""
    edges: collections.Counter = collections.Counter()
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        here = _package_of(path)
        if here not in TIERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt = _type_checking_imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) in exempt:
                continue
            for target in _targets(node, path):
                if target in TIERS and target != here:
                    edges[(here, target)] += 1
    return edges


def _clusters(edges: collections.Counter) -> set[frozenset[str]]:
    """Mutually-importing package groups (Tarjan strongly-connected components).

    Every group of size > 1 is a cycle: each of its members can reach every other
    and come back.
    """
    graph: dict[str, set[str]] = collections.defaultdict(set)
    for source, target in edges:
        graph[source].add(target)

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    found: set[frozenset[str]] = set()
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack[node] = True
        for nxt in graph.get(node, ()):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif on_stack.get(nxt):
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = []
            while True:
                popped = stack.pop()
                on_stack[popped] = False
                component.append(popped)
                if popped == node:
                    break
            if len(component) > 1:
                found.add(frozenset(component))

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 10_000))
    try:
        for node in list(graph):
            if node not in index:
                visit(node)
    finally:
        sys.setrecursionlimit(old_limit)
    return found


def test_the_scan_is_not_vacuous() -> None:
    """A ratchet that scans nothing passes for free."""
    assert PACKAGE.is_dir(), f"package root not found at {PACKAGE}"
    edges = _graph()
    assert edges, f"no internal imports found under {PACKAGE} — the ratchet is scanning nothing"


def test_every_package_is_tiered() -> None:
    """A new package is a layering decision, made in ``TIERS`` or not at all."""
    on_disk = {
        _package_of(p)
        for p in PACKAGE.rglob("*.py")
        if "__pycache__" not in p.parts and p.name != "__init__.py"
    } | {
        d.name
        for d in PACKAGE.iterdir()
        if d.is_dir() and (d / "__init__.py").exists() and d.name != "__pycache__"
    }
    on_disk -= {"__main__", "__init__"}
    untiered = sorted(on_disk - set(TIERS))
    assert not untiered, (
        f"packages with no tier: {untiered}. Add each to TIERS — which layer it "
        f"belongs to is the decision, and this table is where it is recorded."
    )
    stale = sorted(set(TIERS) - on_disk)
    assert not stale, f"TIERS names packages that no longer exist: {stale}"


def test_no_new_upward_imports() -> None:
    """A package imports its own tier or a lower one. The exceptions may only shrink."""
    edges = _graph()
    upward = {
        f"{source} -> {target}": count
        for (source, target), count in edges.items()
        if TIERS[source] < TIERS[target]
    }

    new = {e: n for e, n in upward.items() if e not in UPWARD_BASELINE}
    assert not new, (
        f"new upward imports: {new}. A package may import its own tier or a lower "
        f"one. Import a lower tier, move the shared thing down, or invert the call "
        f"so the higher layer passes what the lower one needs."
    )

    grown = {e: (UPWARD_BASELINE[e], n) for e, n in upward.items() if n > UPWARD_BASELINE[e]}
    assert not grown, (
        f"upward imports grew (baseline, now): {grown}. These edges may only shrink."
    )

    shrunk = {
        e: (n, upward.get(e, 0)) for e, n in UPWARD_BASELINE.items() if upward.get(e, 0) < n
    }
    assert not shrunk, (
        f"upward imports shrank (baseline, now): {shrunk} — lower UPWARD_BASELINE to "
        f"match (drop the entry at zero) so the ratchet holds the ground you took."
    )


def test_no_new_import_cycles() -> None:
    """No new mutually-importing cluster, and no existing one grows."""
    found = _clusters(_graph())

    new = sorted(sorted(c) for c in found - CYCLE_BASELINE)
    assert not new, (
        f"new or grown import cycles: {new}. Two packages that import each other "
        f"cannot be read or tested apart; a function-local import defers the cycle "
        f"to call time without removing it. Move the shared thing down a tier."
    )

    gone = sorted(sorted(c) for c in CYCLE_BASELINE - found)
    assert not gone, (
        f"import cycles broken: {gone} — remove them from CYCLE_BASELINE so the "
        f"ratchet holds the ground you took."
    )


@pytest.mark.parametrize("edge", sorted(UPWARD_BASELINE))
def test_baseline_edges_are_real(edge: str) -> None:
    """Every frozen exception still names a real package pair.

    A baseline naming a package that was renamed or removed would silently permit
    an edge nobody reviewed.
    """
    source, target = edge.split(" -> ")
    assert source in TIERS, f"{edge}: {source} is not a tiered package"
    assert target in TIERS, f"{edge}: {target} is not a tiered package"
    assert TIERS[source] < TIERS[target], (
        f"{edge} is no longer upward (tiers {TIERS[source]} -> {TIERS[target]}) — "
        f"drop it from UPWARD_BASELINE."
    )
