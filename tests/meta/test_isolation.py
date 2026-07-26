"""Isolation ratchet — no suite reaches the real machine.

A test run must not read or write the operator's live state: ``~/.thread`` (every
product's store), ``~/Library/LaunchAgents`` (the daemons), ``~/.local/bin`` (the
installed entrypoints), ``~/.config``. The suite gets a throwaway ``$HOME`` and the
whole product resolves into it.

The redirect is set in ``conftest.py`` at **import** time rather than in a fixture,
and that timing is the load-bearing part. Modules in this codebase bake machine
locations into module-level constants — a store dir, a plist dir, an installed
entrypoint's symlink — each evaluated once, when the module is first imported. A
fixture runs long after that, so it can redirect a call-time ``Path.home()`` and
still leave every baked constant aimed at the real machine. conftest is imported
before the test modules that import the product, so a ``$HOME`` set there is the
one those constants bake against.

Four checks, in the order the isolation can fail:

- ``$HOME`` itself points somewhere throwaway.
- No environment variable aims back at the real machine's state.
- No module this product ships holds a path into the real machine. This imports
  every module under ``src/`` and reads the constants back, so it catches a state
  path frozen at import — the leak a ``Path.home()`` check cannot see.
- No module freezes a machine-state path *at all*, even one resolving into the
  sandbox. Once the redirect lands early enough, a constant baked against the
  throwaway home is invisible to the check above while being frozen just the
  same — and it names production again the moment it is imported outside a test.

If this fails, the suite is one ``mkdir`` away from writing to production.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import pwd
from pathlib import Path

# Home as the *passwd database* records it. Unlike Path.home(), this does not read
# $HOME — so it cannot be moved by the very redirect it exists to verify, and stays
# a true reading of the real machine from inside an isolated suite.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)

# $HOME as it stood when this module was imported — which is during collection, after
# conftest.py is imported and before any fixture runs. Read at test-call time instead,
# this would show the work of an autouse fixture and so pass happily on a suite whose
# import-time redirect was missing entirely: the exact failure the fixture cannot cover
# and this file exists to catch. Capture it where the fixtures cannot reach.
HOME_AT_IMPORT = Path(os.environ.get("HOME", str(REAL_HOME)))

# The operator's live state. Not the whole of the real home: the source checkout
# typically lives under it too, and a module resolving its own repo root
# from ``__file__`` is reading source, which is fine. These are the directories
# where a stray write lands on production.
REAL_STATE_DIRS = (
    REAL_HOME / ".thread",
    REAL_HOME / "Library" / "LaunchAgents",
    REAL_HOME / ".local" / "bin",
    REAL_HOME / ".config",
)

# The same directories as home-relative tails: ``(".thread",)``,
# ``("Library", "LaunchAgents")``, ``(".local", "bin")``, ``(".config",)``. The
# frozen-path check matches these rather than a particular home, because inside an
# isolated suite every one of them resolves *under the sandbox* — so a frozen
# constant looks nothing like the real machine while being frozen just the same.
# Derived from the tuple above rather than restated, so widening the list of live
# state dirs widens both checks at once and neither can fall behind the other.
REAL_STATE_TAILS = tuple(d.relative_to(REAL_HOME).parts for d in REAL_STATE_DIRS)


def _product_root() -> Path:
    """The nearest ancestor owning both a pyproject and a src tree.

    Discovered rather than hardcoded so this file is byte-identical in every
    product's meta section regardless of how deeply its tests nest.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src").is_dir():
            return parent
    raise RuntimeError(f"no product root above {__file__}")


SRC = _product_root() / "src"


def _hits_real_state(value: object) -> Path | None:
    """The real-machine path this value names, if it names one.

    Containers are searched one level down: a store path is as likely to be a dict
    of Paths (``EVIDENCE_DIRS``) or a tuple of them (``DEFAULT_FOLDERS``) as a bare
    constant.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return next((h for v in value if (h := _hits_real_state(v))), None)
    if isinstance(value, dict):
        return next((h for v in value.values() if (h := _hits_real_state(v))), None)
    if isinstance(value, str):
        if not value.startswith("/"):  # not a path at all
            return None
        value = Path(value)
    if not isinstance(value, Path):
        return None
    for state in REAL_STATE_DIRS:
        if value == state or state in value.parents:
            return value
    return None


def _frozen_state_path(value: object) -> Path | None:
    """The machine-state path this value freezes, if it freezes one.

    Matched on the home-relative tail — ``.thread``, ``Library/LaunchAgents``,
    ``.local/bin``, ``.config`` — rather than against a particular home, so it
    holds whichever home is in force: the operator's or a test's sandbox.

    The tail must appear as consecutive components, which is what keeps
    ``/usr/local/bin`` (a system path, and no business of this ratchet) from
    reading as ``~/.local/bin``.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return next((h for v in value if (h := _frozen_state_path(v))), None)
    if isinstance(value, dict):
        return next((h for v in value.values() if (h := _frozen_state_path(v))), None)
    if isinstance(value, str):
        if not value.startswith("/"):
            return None
        value = Path(value)
    if not isinstance(value, Path):
        return None
    parts = value.parts
    for tail in REAL_STATE_TAILS:
        span = len(tail)
        if any(parts[i : i + span] == tail for i in range(len(parts) - span + 1)):
            return value
    return None


def _shipped_modules() -> list[str]:
    """Every module this product ships, by import name.

    Test trees are excluded even when they live under ``src/`` — importing a suite
    from inside itself is neither meaningful nor safe.
    """
    names: list[str] = []
    for pkg in sorted(p.name for p in SRC.iterdir() if (p / "__init__.py").exists()):
        names.append(pkg)
        for info in pkgutil.walk_packages([str(SRC / pkg)], prefix=f"{pkg}."):
            if "tests" not in info.name.split("."):
                names.append(info.name)
    return names


def test_the_scan_is_not_vacuous() -> None:
    """A ratchet that scans nothing passes for free — see docs/testing.md."""
    assert SRC.is_dir(), f"product source root not found at {SRC}"
    assert _shipped_modules(), f"no modules found under {SRC} — the ratchet scans nothing"


def test_home_was_redirected_before_the_product_was_imported() -> None:
    """The suite runs against a throwaway home — and did so from the first import.

    Asserted against the home in force at *collection*, not at call time, because an
    autouse fixture redirecting ``$HOME`` for the test body is not enough: by then the
    product's modules are imported and their store paths are already frozen. The
    redirect has to be in place earlier than any fixture can run.
    """
    assert (
        HOME_AT_IMPORT != REAL_HOME and REAL_HOME not in HOME_AT_IMPORT.parents
    ), (
        f"$HOME was the real home ({HOME_AT_IMPORT}) when this product was imported. "
        f"Redirect it to a throwaway directory at module scope in conftest.py — not in "
        f"a fixture, which runs too late to move a store path that is already baked. "
        f"Until then a test is one mkdir away from writing to the live machine."
    )


def test_no_env_var_aims_at_the_real_machine() -> None:
    """An override in the ambient environment must not point back at production.

    A redirected ``$HOME`` is no help if ``THREAD_JOBS_DIR`` still names the real
    queue: the explicit override is exactly what wins over the default.
    """
    leaks = {k: v for k, v in os.environ.items() if _hits_real_state(v)}
    assert not leaks, (
        f"these environment variables point into the real machine's state: {leaks}. "
        f"The suite must not inherit them — clear or redirect them in conftest.py."
    )


def test_no_shipped_module_holds_a_real_machine_path() -> None:
    """No module-level constant baked against the real machine when it was imported.

    This is the check ``test_home_is_redirected`` cannot make. A constant like
    ``JOBS_DIR = Path.home() / ".thread" / "jobs"`` is frozen the moment its module
    is first imported; if that happened before the redirect, the constant names the
    operator's live store and every fixture in the suite is powerless over it.
    """
    leaks: dict[str, Path] = {}
    for name in _shipped_modules():
        try:
            module = importlib.import_module(name)
        except Exception:
            continue  # a fail-soft optional dep is absent — not this test's business
        for attr, value in vars(module).items():
            if attr.startswith("__"):
                continue
            if hit := _hits_real_state(value):
                leaks[f"{name}.{attr}"] = hit

    assert not leaks, (
        f"these module-level constants point into the real machine's state: "
        f"{ {k: str(v) for k, v in leaks.items()} }. They were baked at import time, "
        f"before the suite's $HOME redirect took effect — so no fixture can isolate "
        f"them and a test that touches one writes to production. Set $HOME in "
        f"conftest.py at module scope (not in a fixture), so it is already redirected "
        f"when these modules are first imported."
    )


def test_no_shipped_module_freezes_a_machine_state_path() -> None:
    """No module-level constant holds a path into any of the live state dirs.

    Every one of these locations is *configuration* — an env override, a test's
    sandbox, a consumer pointing somewhere else — and a constant answers the
    question once, at import, then ignores every later word on the subject.
    Resolve it in a function instead (``jobs_dir()``, ``launch_agents_dir()``,
    ``bin_symlink()``).

    A frozen *derived* path is worse than a frozen root: move the root and the
    derived paths all still point at the old one, so every caller has to know the
    full list and move each by hand — and whichever it forgets goes on quietly
    reading the real store while the test believes it is sandboxed.

    This is deliberately wider than the real-machine check above, and catches what
    that one structurally cannot. A suite whose ``$HOME`` redirect lands early
    enough freezes these constants against the *sandbox*, so they no longer name
    the operator's machine and the real-path check passes — while an installer
    holding one still writes to production in every context that is not a test.
    """
    frozen: dict[str, Path] = {}
    for name in _shipped_modules():
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        for attr, value in vars(module).items():
            if attr.startswith("__"):
                continue
            if hit := _frozen_state_path(value):
                frozen[f"{name}.{attr}"] = hit

    assert not frozen, (
        f"these module-level constants freeze a machine-state path: "
        f"{ {k: str(v) for k, v in frozen.items()} }. Resolve the location in a "
        f"function so an env override, a test sandbox, or a consumer can actually "
        f"move it — a constant is an answer given once at import and never revisited."
    )
