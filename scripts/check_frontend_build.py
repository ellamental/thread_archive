"""Fail when the committed web bundle differs from a clean Vite build."""

from __future__ import annotations

import filecmp
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
COMMITTED = REPO / "src" / "thread_archive" / "_web" / "static"


def _files(root: Path) -> dict[str, Path]:
    return {
        str(path.relative_to(root)): path
        for path in root.rglob("*")
        if path.is_file()
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="thread-archive-web-build-") as raw:
        built = Path(raw)
        result = subprocess.run(
            ["npx", "vite", "build", "--outDir", str(built)],
            cwd=FRONTEND,
            text=True,
        )
        if result.returncode:
            return result.returncode

        expected = _files(COMMITTED)
        actual = _files(built)
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            name for name in set(expected) & set(actual)
            if not filecmp.cmp(expected[name], actual[name], shallow=False)
        )
        if missing or extra or changed:
            print("committed frontend bundle is stale", file=sys.stderr)
            if missing:
                print(f"  missing from build: {missing}", file=sys.stderr)
            if extra:
                print(f"  missing from committed bundle: {extra}", file=sys.stderr)
            if changed:
                print(f"  changed: {changed}", file=sys.stderr)
            print("run `cd frontend && npm run build`", file=sys.stderr)
            return 1
    print("committed frontend bundle matches a clean Vite build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
