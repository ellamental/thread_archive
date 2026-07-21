"""Realistic first-run install check: discovery-driven ingest from real store locations.

Where :mod:`e2e_check` hand-feeds each provider's path to the importer, this proves the
path a *new user's first run* actually takes: a fake ``$HOME`` with every harness's store
in its **real default location** (``~/.claude/projects``, ``~/.codex/sessions``, the
OS-correct app-data dir for Cursor/Cowork, …), driven entirely through the installed
``thread_archive`` CLI — ``watch --once`` discovers and ingests the live stores with no
hand-fed paths, ``import-export`` takes the two account exports a user drops in by hand —
then reindexed and searched back. It is OS-aware: the same run proves discovery on macOS
(``~/Library/Application Support``) and on Linux (``~/.config``), so it is the cross-OS
lane the packaged distribution otherwise lacks.

Every step runs against the *installed* CLI (``--bin`` = the venv's ``bin``; default: the
venv running this script), so it doubles as an install proof. It exits non-zero on any
failure.

    python tests/install/first_run.py            # against the CLI on PATH / this venv
    python tests/install/first_run.py --keep     # leave the fake home for inspection

The ``package`` pytest lane calls :func:`run` against a clean wheel-only venv (macOS via
thread-ci, Linux via GitHub Actions); the Docker install lane runs this as a script inside
a clean container. One corpus, one code path, both OSes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_fixtures  # noqa: E402


class FirstRunError(AssertionError):
    """A first-run step failed (raised so pytest reports it; caught by ``main``)."""


def _run(bin_dir: Path, argv: list[str], home: Path, archive_home: Path) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "THREAD_ARCHIVE_HOME": str(archive_home),
        # venv bin alone: the installed `thread_archive` / `python` win, and no
        # stray XDG_CONFIG_HOME repoints the Linux app-data base out from under
        # where the fixtures were placed.
        "PATH": str(bin_dir),
    }
    # cwd = home (outside any checkout) so an editable src/ never shadows the
    # installed package.
    return subprocess.run(
        [str(bin_dir / argv[0]), *argv[1:]],
        capture_output=True, text=True, cwd=str(home), env=env,
    )


def _ok(bin_dir: Path, argv: list[str], home: Path, archive_home: Path) -> subprocess.CompletedProcess:
    r = _run(bin_dir, argv, home, archive_home)
    if r.returncode != 0:
        raise FirstRunError(
            f"`{' '.join(argv)}` exited {r.returncode}\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )
    return r


# The search probe runs inside the installed venv (its `thread_archive`, not this
# script's), so search is proven against the packaged retrieval stack — the same
# `_api.search` the read MCP tool calls. Markers arrive as argv; a MISSING list +
# non-zero exit is the failure signal.
_SEARCH_PROBE = r"""
import json, sys
from thread_archive import _api as ta
home = sys.argv[1]
markers = json.loads(sys.argv[2])
ta.open_archive(home)
missing = []
for provider, marker in markers.items():
    hits = ta.search(marker, home=home, limit=5)
    print(f"  {provider:14} {marker!r:22} -> {len(hits)} hit(s) {'OK' if hits else 'MISSING'}")
    if not hits:
        missing.append(provider)
if missing:
    print("MISSING:" + ",".join(missing))
    sys.exit(1)
print("SEARCH_OK")
"""


def _threads_and_indexed(status_stdout: str) -> tuple[int, int]:
    threads = re.search(r"^threads:\s*(\d+)", status_stdout, re.M)
    indexed = re.search(r"^indexed:\s*(\d+)", status_stdout, re.M)
    if not threads or not indexed:
        raise FirstRunError(f"could not parse status output:\n{status_stdout}")
    return int(threads.group(1)), int(indexed.group(1))


def _default_bin() -> Path:
    """The bin dir holding the installed console scripts. ``sys.executable``'s dir
    (NOT resolved — a venv python is a symlink to the base interpreter, and
    resolving escapes the venv), falling back to wherever ``thread_archive`` is on
    PATH if that dir doesn't carry it (e.g. a login shell that reset PATH out from
    under the venv)."""
    here = Path(sys.executable).parent
    if (here / "thread_archive").exists():
        return here
    found = shutil.which("thread_archive")
    return Path(found).parent if found else here


def run(bin_dir: Path | None = None, home: Path | None = None, keep: bool = False) -> None:
    """Drive the realistic first run against the CLI in ``bin_dir`` (default: this
    venv). Raises :class:`FirstRunError` on any failure."""
    bin_dir = Path(bin_dir) if bin_dir else _default_bin()
    work = Path(home) if home else Path(tempfile.mkdtemp(prefix="thread-archive-first-run-"))
    fake_home = work / "machine"
    fake_home.mkdir(parents=True, exist_ok=True)
    # The real default archive home for this fake machine — a user's is ~/.thread/archive.
    archive_home = fake_home / ".thread" / "archive"

    layout = make_fixtures.realistic_layout(fake_home)
    markers = layout["markers"]
    print(f"bin:          {bin_dir}")
    print(f"fake $HOME:   {fake_home}")
    print(f"archive home: {archive_home}\n")

    try:
        # 1. Discover + ingest every live store from its real default location — no
        #    hand-fed paths. This is the whole point: `watch --once` finds them.
        r = _ok(bin_dir, ["thread_archive", "watch", "--once"], fake_home, archive_home)
        print(r.stdout.strip())

        # 2. The two account exports a user drops in by hand (no live store).
        for export in layout["exports"]:
            r = _ok(bin_dir, ["thread_archive", "import-export", export], fake_home, archive_home)
            print(r.stdout.strip())
        print()

        # 3. Status: the ingest actually landed conversations and indexed them.
        r = _ok(bin_dir, ["thread_archive", "status"], fake_home, archive_home)
        threads, indexed = _threads_and_indexed(r.stdout)
        print(f"after ingest:  threads={threads} indexed={indexed}")
        if threads < layout["min_threads"]:
            raise FirstRunError(
                f"expected >= {layout['min_threads']} threads after discovery ingest, got {threads}\n{r.stdout}")
        if indexed <= 0:
            raise FirstRunError(f"FTS index empty after ingest:\n{r.stdout}")

        # 4. Rebuild the index purely from JSONL truth — the lossless-rebuild contract —
        #    then confirm nothing was lost.
        _ok(bin_dir, ["thread_archive", "reindex"], fake_home, archive_home)
        r = _ok(bin_dir, ["thread_archive", "status"], fake_home, archive_home)
        threads2, indexed2 = _threads_and_indexed(r.stdout)
        print(f"after reindex: threads={threads2} indexed={indexed2}")
        if threads2 < threads or indexed2 <= 0:
            raise FirstRunError(
                f"reindex lost data: {threads}->{threads2} threads, {indexed}->{indexed2} indexed\n{r.stdout}")

        # 5. Every provider's content is searchable by its marker — through the
        #    installed retrieval stack.
        print("\nsearch markers:")
        probe = _run(
            bin_dir,
            ["python", "-c", _SEARCH_PROBE, str(archive_home), json.dumps(markers)],
            fake_home, archive_home,
        )
        print(probe.stdout.rstrip())
        if probe.returncode != 0:
            raise FirstRunError(
                f"search probe failed:\n{probe.stdout}\n--- stderr ---\n{probe.stderr}")
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)

    print("\nFIRST-RUN INSTALL CHECK PASSED")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Realistic first-run install check for thread-archive.")
    p.add_argument("--bin", default=None, help="venv bin dir with the installed CLI (default: this venv)")
    p.add_argument("--home", default=None, help="work dir (else a temp dir)")
    p.add_argument("--keep", action="store_true", help="keep the fake home for inspection")
    args = p.parse_args(argv)
    try:
        run(bin_dir=args.bin, home=args.home, keep=args.keep)
    except FirstRunError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
