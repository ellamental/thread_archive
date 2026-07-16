"""No patching of private internals — the implementation-coupled form of mock.

The house style is to avoid mocks and patching at all, at (almost) any cost: drive
the real code over real data — a seeded store, a real file, a real subprocess.
When one of the rare justified patches is needed (a boundary the test genuinely
cannot cross), it targets a public seam. Patching an underscore-private —
``monkeypatch.setattr(mod, "_helper", ...)`` or ``patch.object(mod, "_helper",
...)`` — couples the test to the module's internal shape instead: the test fakes
the very code it claims to exercise, so it keeps passing while the real helper
rots, and an internal refactor reds the test without breaking the product. A
private that tests need to replace is a seam asking to be public — a parameter, a
constructor argument, a setting — so the fake can go in through the front door.

``BASELINE`` freezes the private-target patches that exist today. They may only
shrink. A file not listed may not introduce one at all.

Paying it down: exercise the public surface over real data (seed the store, write
the real file), or promote the seam to an explicit public knob and inject through
it — then lower the count here (drop the entry at zero). The counts are asserted
exactly, in both directions, so the baseline cannot go stale — a file that gets
*better* than its entry reds the suite until the entry is lowered to match.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent

# Known private-target patches, by path relative to the test root.
# This mapping can only shrink, never grow.
BASELINE: dict[str, int] = {
    # Clean: this product has no private-target patches. Keep it that way.
}


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_private_target_patch(node: ast.AST) -> bool:
    """A patch call whose target attribute is underscore-private.

    Matches ``<x>.setattr(obj, "_name", ...)`` and ``<x>.setattr("mod.path._name",
    ...)`` — pytest's MonkeyPatch under any receiver name — plus ``patch.object(obj,
    "_name", ...)``. Bare builtin ``setattr`` is not counted: in a test body it is
    plain attribute assignment on an object the test owns, not patching.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    func = node.func

    if func.attr == "setattr":
        if not node.args:
            return False
        dotted = _str_const(node.args[0])
        if dotted is not None:
            return dotted.rsplit(".", 1)[-1].startswith("_")
        if len(node.args) >= 2:
            name = _str_const(node.args[1])
            return name is not None and name.startswith("_")
        return False

    if func.attr == "object":
        owner = func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else (
            owner.attr if isinstance(owner, ast.Attribute) else None
        )
        if owner_name != "patch":
            return False
        if len(node.args) >= 2:
            name = _str_const(node.args[1])
            return name is not None and name.startswith("_")
        return False

    return False


def _actual_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        n = sum(1 for node in ast.walk(tree) if _is_private_target_patch(node))
        if n:
            counts[path.relative_to(TESTS_ROOT).as_posix()] = n
    return counts


def test_the_scan_is_not_vacuous() -> None:
    """Guard the guard: a mis-anchored test root would silently enforce nothing."""
    assert TESTS_ROOT.is_dir(), f"test root not found at {TESTS_ROOT}"
    assert list(TESTS_ROOT.rglob("test_*.py")), f"no tests found under {TESTS_ROOT}"


def test_no_new_private_patches() -> None:
    actual = _actual_counts()

    introduced = sorted(set(actual) - set(BASELINE))
    assert not introduced, (
        f"these files introduce private-target patches: {introduced}. Faking a "
        f"module's own _private couples the test to implementation and lets the "
        f"real code rot unexercised — exercise the public surface over real data, "
        f"or promote the seam to a public parameter and inject through it."
    )

    grown = {f: (BASELINE[f], n) for f, n in actual.items() if n > BASELINE.get(f, 0)}
    assert not grown, (
        f"private-target patch count grew (file: baseline -> now): {grown}. "
        f"This baseline only shrinks."
    )


def test_baseline_has_not_gone_stale() -> None:
    """Debt that was paid must be recorded as paid, or the baseline drifts upward
    in spirit — a stale entry silently re-authorizes a patch nobody is using."""
    actual = _actual_counts()

    shrunk = {f: (c, actual.get(f, 0)) for f, c in BASELINE.items() if actual.get(f, 0) < c}
    assert not shrunk, (
        f"private-target patch debt was paid down (file: baseline -> now): {shrunk}. "
        f"Lower these entries in BASELINE to match (drop the entry at zero) so the "
        f"ratchet holds the new ground."
    )
