"""No global-symbol patching — the leaky form of mock.

The house style is ``monkeypatch`` plus a small hand-written fake. When a patch is
genuinely needed it patches the **module attribute the code under test resolves**
(``monkeypatch.setattr(api.subprocess, "run", ...)``), never a global symbol named
by dotted string (``patch("subprocess.run")``). The string form replaces the
symbol for every module in the process, so it leaks: an unrelated test sharing the
process sees the fake, and the failure surfaces far from the patch that caused it.
It also survives no refactor — the string is not a reference, so moving the code
silently patches nothing and the test keeps passing against the real object.

``BASELINE`` freezes the string-target ``patch()`` calls that exist today. They may
only shrink. A file not listed may not introduce one at all.

Paying it down: rewrite the call as ``monkeypatch.setattr`` on the resolved module
attribute, then lower the count here (drop the entry at zero). The counts are
asserted exactly, in both directions, so the baseline cannot go stale — a file that
gets *better* than its entry reds the suite until the entry is lowered to match.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent

# Known string-target patch() calls, by path relative to the test root.
# This mapping can only shrink, never grow.
BASELINE: dict[str, int] = {
    # Clean: this product has never reached for the global form. Keep it that way.
}


def _is_string_target_patch(node: ast.AST) -> bool:
    """A ``patch("some.dotted.target")`` call — the global, leaky form.

    ``patch.object(mod, "attr")`` is not this: it takes a real module reference, so
    it neither leaks process-wide nor outlives a rename silently.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return False
    if name != "patch":
        return False
    return bool(node.args) and isinstance(node.args[0], ast.Constant) and isinstance(
        node.args[0].value, str
    )


def _actual_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        n = sum(1 for node in ast.walk(tree) if _is_string_target_patch(node))
        if n:
            counts[path.relative_to(TESTS_ROOT).as_posix()] = n
    return counts


def test_the_scan_is_not_vacuous() -> None:
    """Guard the guard: a mis-anchored test root would silently enforce nothing."""
    assert TESTS_ROOT.is_dir(), f"test root not found at {TESTS_ROOT}"
    assert list(TESTS_ROOT.rglob("test_*.py")), f"no tests found under {TESTS_ROOT}"


def test_no_new_global_patches() -> None:
    actual = _actual_counts()

    introduced = sorted(set(actual) - set(BASELINE))
    assert not introduced, (
        f"these files introduce string-target patch(): {introduced}. Use "
        f"monkeypatch.setattr on the module attribute the code under test resolves "
        f"— patch(\"a.b.c\") replaces the symbol process-wide and leaks into every "
        f"other test sharing the process."
    )

    grown = {f: (BASELINE[f], n) for f, n in actual.items() if n > BASELINE.get(f, 0)}
    assert not grown, (
        f"string-target patch() count grew (file: baseline -> now): {grown}. "
        f"This baseline only shrinks."
    )


def test_baseline_has_not_gone_stale() -> None:
    """Debt that was paid must be recorded as paid, or the baseline drifts upward
    in spirit — a stale entry silently re-authorizes a patch nobody is using."""
    actual = _actual_counts()

    shrunk = {f: (c, actual.get(f, 0)) for f, c in BASELINE.items() if actual.get(f, 0) < c}
    assert not shrunk, (
        f"string-target patch() debt was paid down (file: baseline -> now): {shrunk}. "
        f"Lower these entries in BASELINE to match (drop the entry at zero) so the "
        f"ratchet holds the new ground."
    )
