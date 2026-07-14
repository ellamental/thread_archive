"""Dependency-tier ratchet — a product's declared dependency surface is its real one.

Every import under ``src/`` falls into a tier, and the tier decides how it may be
imported. The tiers are read out of the product's own ``pyproject.toml`` rather
than a list maintained in this file, so a deliberate change to the surface needs
no edit here.

- **Stdlib, and the product's own package** — import freely.

- **A distribution in ``project.dependencies``** — import freely. It is a hard
  requirement, and declaring it is what makes it one.

- **A distribution in ``project.optional-dependencies``** — import *fail-soft
  only*: under a ``try`` that handles ``ImportError``, degrading when the extra
  isn't installed. An extra is optional by construction; a hard import of one is
  a hard dependency wearing an optional label, and it breaks the base install.

- **A sibling thread product** — import *fail-soft only*, same shape. Composing
  processes may import a sibling's library, never require it (dependency tier 3).

- **Anything else** — not importable at all, hard or soft. A package that happens
  to be in the venv because some other dependency dragged it in is not a
  dependency; relying on it is relying on a transitive that a minor version bump
  is free to take away.

For a product declaring ``dependencies = []`` this is what makes "stdlib-only"
load-bearing rather than aspirational: the empty list *is* the policy.

Imports under ``if TYPE_CHECKING:`` are exempt — they cost nothing at runtime.

If this fails, the fix is exactly one of: declare the dependency, wrap the import
in a fail-soft guard, or drop it.
"""

from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions, requires
from pathlib import Path

import pytest


def _product_root() -> Path:
    """The nearest ancestor owning both a pyproject and a src tree.

    Discovered rather than hardcoded so this file is byte-identical in every
    product's meta section regardless of how deeply its tests nest.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src").is_dir():
            return parent
    raise RuntimeError(f"no product root above {__file__}")


PRODUCT_ROOT = _product_root()
SRC = PRODUCT_ROOT / "src"
PYPROJECT = PRODUCT_ROOT / "pyproject.toml"


def _normalize(dist: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", dist).lower()


def _dist_name(spec: str) -> str:
    """The distribution name from a requirement spec, minus extras/markers/pins."""
    return _normalize(re.split(r"[<>=!~;\[\s]", spec, maxsplit=1)[0])


def _modules_for(dist: str, installed: dict[str, list[str]]) -> set[str]:
    """The import names a declared distribution entitles us to.

    A distribution's name and its import name diverge often enough (python-dotenv
    ships ``dotenv``, PyYAML ships ``yaml``) that guessing is wrong — the installed
    metadata records the true mapping, so read it. A shim distribution ships no
    module of its own and exists only to pull in the real one under a different
    name (``python-igraph`` → ``igraph``); for those, and only those, follow the
    direct requirements one level. We deliberately do not walk the full transitive
    closure: declaring one fat dependency must not silently entitle us to
    everything underneath it.
    """
    provided = {
        mod for mod, dists in installed.items() if any(_normalize(d) == dist for d in dists)
    }
    if provided:
        return provided

    try:
        direct = requires(dist) or []
    except Exception:
        direct = []
    shimmed: set[str] = set()
    for req in direct:
        if ";" in req:  # a conditional/extra requirement — not what a shim does
            continue
        shimmed |= {
            mod
            for mod, dists in installed.items()
            if any(_normalize(d) == _dist_name(req) for d in dists)
        }
    # Nothing installed to learn from: fall back to the usual name-mangling.
    return shimmed or {dist.replace("-", "_")}


def _declared() -> tuple[set[str], set[str]]:
    """(hard-importable modules, fail-soft-only modules) for this product."""
    # Imported here, not at module scope: this file is byte-identical across
    # products, and isort sorts tomllib into a different block depending on whether
    # the product's requires-python floor predates its promotion to the stdlib.
    import tomllib

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    installed = packages_distributions()  # import name -> [distribution names]

    hard: set[str] = set()
    for spec in project.get("dependencies", []):
        if dist := _dist_name(spec):
            hard |= _modules_for(dist, installed)

    soft: set[str] = set()
    for extra, specs in project.get("optional-dependencies", {}).items():
        if extra == "dev":  # dev extras are for the test tree, which we don't scan
            continue
        for spec in specs:
            if dist := _dist_name(spec):
                soft |= _modules_for(dist, installed)

    return hard, soft - hard


def _own_packages() -> set[str]:
    return {d.name for d in SRC.iterdir() if d.is_dir() and (d / "__init__.py").exists()}


# Any of these actually catches a missing module. The invariant is that the code
# degrades when the import fails, not that the handler is spelled narrowly — a
# broad catch around an optional import is a legitimate shape (the probe that
# follows the import can raise more than ImportError).
CATCHES_MISSING_MODULE = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _handles_import_error(handler: ast.ExceptHandler) -> bool:
    """Does this ``except`` clause catch a missing module?"""
    if handler.type is None:  # bare `except:`
        return True
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        isinstance(exc, ast.Name) and exc.id in CATCHES_MISSING_MODULE for exc in caught
    )


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _exempt_import_ids(tree: ast.AST) -> tuple[set[int], set[int]]:
    """Import nodes that are (fail-soft, type-checking-only)."""
    failsoft: set[int] = set()
    typing_only: set[int] = set()

    def collect(body: list[ast.stmt], sink: set[int]) -> None:
        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    sink.add(id(sub))

    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_handles_import_error(h) for h in node.handlers):
            collect(node.body, failsoft)
        elif isinstance(node, ast.If) and _is_type_checking_guard(node):
            collect(node.body, typing_only)

    return failsoft, typing_only


def _imports(tree: ast.AST) -> list[tuple[ast.stmt, str]]:
    """Every import in the tree, paired with the top-level module it names."""
    found: list[tuple[ast.stmt, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node, alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative — in-package by construction
                continue
            if node.module:
                found.append((node, node.module.split(".")[0]))
    return found


def _source_files() -> list[Path]:
    """The product's shipped modules.

    Test trees are excluded even when they live under ``src/``: the invariant
    governs what the product *ships*, and test code may freely use the dev extra.
    """
    return sorted(p for p in SRC.rglob("*.py") if "tests" not in p.relative_to(SRC).parts)


def test_the_scan_is_not_vacuous() -> None:
    """A ratchet that scans nothing passes for free.

    An empty parametrize set is reported by pytest as a *skip*, not a failure, so
    a mis-anchored source root would silently disable everything below without
    reddening the suite. Assert we found the tree we think we found.
    """
    assert SRC.is_dir(), f"product source root not found at {SRC}"
    assert _source_files(), f"no modules found under {SRC} — the ratchet is scanning nothing"


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_imports_stay_within_the_declared_surface(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    failsoft, typing_only = _exempt_import_ids(tree)
    hard_ok, soft_only = _declared()
    freely = sys.stdlib_module_names | _own_packages() | hard_ok

    undeclared: list[str] = []
    hard_optional: list[str] = []
    hard_siblings: list[str] = []

    for node, mod in _imports(tree):
        if id(node) in typing_only or mod in freely:
            continue
        guarded = id(node) in failsoft
        if mod in soft_only:
            if not guarded:
                hard_optional.append(mod)
        elif mod.startswith("thread_"):
            if not guarded:
                hard_siblings.append(mod)
        else:
            undeclared.append(mod)

    rel = path.relative_to(SRC)
    assert not undeclared, (
        f"{rel} imports {sorted(set(undeclared))}, which pyproject.toml does not declare. "
        f"Declare it, or drop the import — a package that is merely importable in this "
        f"venv is a transitive, not a dependency."
    )
    assert not hard_optional, (
        f"{rel} imports {sorted(set(hard_optional))} hard, but pyproject.toml declares it "
        f"only as an optional extra. Put the import under a `try` that handles ImportError "
        f"and degrade when the extra is absent — otherwise the base install is broken."
    )
    assert not hard_siblings, (
        f"{rel} imports the sibling product(s) {sorted(set(hard_siblings))} hard. "
        f"Sibling libraries are dependency tier 3: put the import under a `try` that "
        f"handles ImportError and degrade when the sibling is absent."
    )
