"""Write the thread-family manifest — ``<archive home>/product.json``.

Every installed thread-family product declares itself with one small JSON file
in its home directory; discovery is enumeration (a consumer globs
``~/.thread/*/product.json``), no registry. This writer is the reference for
the manifest's shape.

This is installer machinery, not library code: it lives in ``host/`` (never
packaged — see ``pyproject.toml``'s sdist ``only-include``) because it depends
on a repo checkout, resolving the MCP server commands out of the repo venv. The
manifest is machine state — written by ``make install-agent`` / ``make
manifest``, never hand-edited, never checked in.

The ``console`` / ``health`` surfaces point at the cohosted viewer the watcher
agent serves on :8787 (``archive watch`` — the process ``install-agent``
installs), so this writer declares them. A box running only ad-hoc imports (no
agent) can pass ``--no-web`` to omit them.

Run it with the repo venv's python:

    .venv/bin/python host/write-manifest.py [--home DIR] [--no-web]
"""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import os
import tempfile
from pathlib import Path

from thread_archive._config import resolve_paths

REPO_ROOT = Path(__file__).resolve().parents[1]

WEB_BASE = "http://127.0.0.1:8787"


def build_manifest(*, home: str | None = None, web: bool = True) -> tuple[Path, dict]:
    """The manifest destination (``<home>/product.json``) and its content."""
    paths = resolve_paths(home)
    try:
        version = importlib.metadata.version("thread-archive")
    except importlib.metadata.PackageNotFoundError:
        version = "0.0.0"
    manifest: dict = {
        "v": 1,
        "name": "thread-archive",
        "version": version,
        "description": (
            "Serverless-native local archive for AI conversations: JSONL truth "
            "log + rebuildable SQLite index, searched and read locally, exposed "
            "over MCP."
        ),
        "repo": "https://github.com/ellamental/thread_archive",
        "stores": {"archive": str(paths.home)},
    }
    if web:
        manifest["console"] = {"url": WEB_BASE}
        manifest["health"] = {"url": f"{WEB_BASE}/api/health"}
    mcp = [
        {"name": "thread-archive", "command": str(REPO_ROOT / ".venv" / "bin" / "archive-mcp")},
        {
            "name": "thread-archive-librarian",
            "command": str(REPO_ROOT / ".venv" / "bin" / "archive-librarian-mcp"),
        },
    ]
    manifest["mcp"] = [m for m in mcp if Path(m["command"]).exists()] or None
    if manifest["mcp"] is None:
        del manifest["mcp"]
    manifest["written_by"] = "thread-archive install (host/write-manifest.py)"
    manifest["written_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )
    return paths.home / "product.json", manifest


def write_manifest(*, home: str | None = None, web: bool = True) -> Path:
    """Write ``<home>/product.json`` atomically (temp file + rename)."""
    dest, manifest = build_manifest(home=home, web=web)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        os.rename(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="host/write-manifest.py",
        description="write the thread-family product.json manifest",
    )
    parser.add_argument("--home", default=None, help="archive home (default: resolved)")
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="omit the console/health surfaces (no watcher agent on this box)",
    )
    args = parser.parse_args(argv)
    dest = write_manifest(home=args.home, web=not args.no_web)
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
