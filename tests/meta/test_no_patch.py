"""No patching, in any form — the umbrella ratchet over all of it.

The house style is to avoid mocks and patching at all, at (almost) any cost:
drive the real code over real data — a seeded store, a real file, a real
subprocess. Every patch call fakes something the product actually runs, so the
test passes against an implementation the user never gets, and keeps passing
while the real path rots. When a boundary genuinely cannot be crossed in a
test, the seam belongs in the front door — a parameter, a constructor argument,
a setting — with a small hand-written fake injected through it; that is
dependency injection, not patching, and this ratchet does not count it.

The sibling ratchets pin the two worst forms at their floors —
``test_no_global_patch`` (string-target ``patch()``, the leaky form) and
``test_no_private_patch`` (private targets, the implementation-coupled form).
This one counts every patch call whatever its target, public seams included,
and only lets the total fall.

``BASELINE`` freezes the patch calls that exist today. They may only shrink. A
file not listed may not introduce one at all.

Paying it down: exercise the public surface over real data (seed the store,
write the real file), or promote the seam to an explicit public knob and inject
through it — then lower the count here (drop the entry at zero). The counts are
asserted exactly, in both directions, so the baseline cannot go stale — a file
that gets *better* than its entry reds the suite until the entry is lowered to
match.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent

# Known patch calls, by path relative to the test root.
# This mapping can only shrink, never grow.
BASELINE: dict[str, int] = {
    "test_backfill_export_annotations.py": 2,
    "test_cli_smoke.py": 3,
    "test_cov_cli.py": 4,
    "test_cov_ops_truth.py": 3,
    "test_cov_setup.py": 3,
    "test_cov_watcher.py": 2,
    "test_exthost.py": 1,
    "test_integrity_hardening.py": 1,
    "test_nightly.py": 4,
    "test_repair.py": 4,
    "test_search.py": 2,
    "test_self_update.py": 1,
    "test_store.py": 3,
    "test_truth_manifest.py": 1,
    "test_watch.py": 4,
}

_MONKEYPATCH_MUTATORS = frozenset({"setattr", "delattr", "setitem", "delitem"})


def _is_patch_call(node: ast.AST) -> bool:
    """Any patching call, whatever its target.

    Matches pytest's MonkeyPatch mutators under any receiver name —
    ``<x>.setattr`` / ``.delattr`` / ``.setitem`` / ``.delitem`` — and every
    ``unittest.mock`` spelling: ``patch(...)`` bare or dotted
    (``mock.patch(...)``), plus ``patch.object`` / ``patch.dict`` /
    ``patch.multiple``. Bare builtin ``setattr`` is not counted: in a test
    body it is plain attribute assignment on an object the test owns, not
    patching. ``monkeypatch.setenv`` / ``.delenv`` / ``.chdir`` /
    ``.syspath_prepend`` shape the process environment rather than replace
    code, and are not counted either.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _MONKEYPATCH_MUTATORS or func.attr == "patch":
        return True
    if func.attr in {"object", "dict", "multiple"}:
        owner = func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else (
            owner.attr if isinstance(owner, ast.Attribute) else None
        )
        return owner_name == "patch"
    return False


def _actual_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        n = sum(1 for node in ast.walk(tree) if _is_patch_call(node))
        if n:
            counts[path.relative_to(TESTS_ROOT).as_posix()] = n
    return counts


def test_the_scan_is_not_vacuous() -> None:
    """Guard the guard: a mis-anchored test root would silently enforce nothing."""
    assert TESTS_ROOT.is_dir(), f"test root not found at {TESTS_ROOT}"
    assert list(TESTS_ROOT.rglob("test_*.py")), f"no tests found under {TESTS_ROOT}"


def test_no_new_patches() -> None:
    actual = _actual_counts()

    introduced = sorted(set(actual) - set(BASELINE))
    assert not introduced, (
        f"these files introduce patching: {introduced}. The house style is no "
        f"patching at all: drive the real code over real data, and when a "
        f"boundary genuinely cannot be crossed, inject a hand-written fake "
        f"through a public seam — a parameter, a constructor argument, a "
        f"setting — not over the module's attributes."
    )

    grown = {f: (BASELINE[f], n) for f, n in actual.items() if n > BASELINE.get(f, 0)}
    assert not grown, (
        f"patch call count grew (file: baseline -> now): {grown}. "
        f"This baseline only shrinks."
    )


def test_baseline_has_not_gone_stale() -> None:
    """Debt that was paid must be recorded as paid, or the baseline drifts upward
    in spirit — a stale entry silently re-authorizes a patch nobody is using."""
    actual = _actual_counts()

    shrunk = {f: (c, actual.get(f, 0)) for f, c in BASELINE.items() if actual.get(f, 0) < c}
    assert not shrunk, (
        f"patch debt was paid down (file: baseline -> now): {shrunk}. "
        f"Lower these entries in BASELINE to match (drop the entry at zero) so the "
        f"ratchet holds the new ground."
    )
