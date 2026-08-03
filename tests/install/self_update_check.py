"""Self-update drill: the update path driven against a real index.

The unit suite (tests/test_self_update.py) proves the plan/apply logic over a
local stand-in index; this proves the same machinery against artifacts an index
actually serves, from inside a real packaged install — the drill workflow
(.github/workflows/drill.yml) runs it in a fresh venv against TestPyPI, and a
local wheel directory works the same way via ``--find-links``. Three legs, one
per invocation:

    up-to-date  the running version is the newest *final* on the index — holds
                the guardrail that a newer pre-release is never offered
    update      the full orchestration (`self_update`): resolve, format-gate,
                install, real smoke — and the interpreter must come back
                running the target version
    rollback    a forced smoke failure after the install — the environment
                must come back at the version it started on

Everything after ``--`` is passed to every pip invocation (index selection;
``--no-deps`` is forced on so an open index like TestPyPI only ever serves the
one artifact under judgement — dependencies are already in the venv). Exits
non-zero on any failure. Run with the *venv under drill*'s interpreter; like
e2e_check.py, this harness travels in the tree while the package under test
stays the installed wheel, which the site-packages guard holds mechanically.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import sysconfig
import tempfile


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _installed_version() -> str:
    """The version a fresh interpreter of this venv runs — this process holds
    modules imported before any install, so it must not answer from memory."""
    r = subprocess.run(
        [sys.executable, "-c", "import thread_archive; print(thread_archive.__version__)"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        _fail(f"could not read installed version: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def _guard_installed_package() -> None:
    import thread_archive

    pkg = pathlib.Path(thread_archive.__file__).resolve()
    site = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
    if site not in pkg.parents:
        _fail(f"thread_archive resolves to {pkg}, not the installed wheel under {site}")
    print(f"under drill: {pkg}")


def run(mode: str, current: str, target: str | None, pip_args: list[str]) -> int:
    # The archive home every leg reads and writes (self_update stamps
    # health.json; the smoke check opens the store): a throwaway.
    home = tempfile.mkdtemp(prefix="thread-archive-drill-")
    os.environ["THREAD_ARCHIVE_HOME"] = home

    _guard_installed_package()
    from thread_archive import _api, _update

    # A real install's home is initialized at the running code's truth format;
    # an uninitialized one would read as format 1 and turn the update leg into
    # a migration drill of an empty directory.
    _api.open_archive(home)

    running = _installed_version()
    if running != current:
        _fail(f"venv runs {running}, expected to start at {current}")

    pip_args = ["--no-deps", *pip_args]

    if mode == "up-to-date":
        result = _update.self_update(home, check_only=True, pip_args=pip_args)
        print(result)
        if result.get("action") != "up-to-date":
            _fail(f"expected up-to-date at {current}, got {result}")

    elif mode == "update":
        result = _update.self_update(home, pip_args=pip_args)
        print(result)
        if result.get("action") != "updated" or result.get("target") != target:
            _fail(f"expected an update to {target}, got {result}")
        now = _installed_version()
        if now != target:
            _fail(f"venv runs {now} after the update, expected {target}")

    elif mode == "rollback":
        with tempfile.TemporaryDirectory(prefix="thread-archive-drill-dl-") as scratch:
            plan = _update.plan_update(
                pathlib.Path(scratch), pip_args=pip_args, current_version=current,
            )
            if plan.action != "update" or plan.target != target:
                _fail(f"expected a plan updating to {target}, got {plan}")

            def broken_smoke(_home: str | None) -> None:
                raise RuntimeError("drill: forced smoke failure")

            result = _update.apply_update(
                plan, home=home,
                install=lambda req: _update._default_install(req, pip_args=pip_args),
                smoke=broken_smoke,
            )
        print(result)
        if result.get("action") != "rolled-back":
            _fail(f"expected a rollback to {current}, got {result}")
        now = _installed_version()
        if now != current:
            _fail(f"venv runs {now} after the rollback, expected {current}")

    print(f"OK: {mode}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["up-to-date", "update", "rollback"])
    p.add_argument("--current", required=True, help="version the venv must start at")
    p.add_argument("--target", help="version the index's newest final must be (update/rollback)")
    p.add_argument("pip_args", nargs="*", help="after --: index selection for every pip call")
    args = p.parse_args()
    if args.mode in ("update", "rollback") and not args.target:
        p.error(f"{args.mode} requires --target")
    return run(args.mode, args.current, args.target, args.pip_args)


if __name__ == "__main__":
    raise SystemExit(main())
