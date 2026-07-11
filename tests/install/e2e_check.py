"""End-to-end install check: a real archive lifecycle on a populated store.

The unit suite proves the pieces; this proves the *installed package* works on actual
provider data the way a new user's first run does: import every provider, rebuild the
index from the JSONL truth, and confirm each provider's content is searchable. It exits
non-zero on any failure so the Docker install test (or a host run) fails loudly.

Run after ``pip install -e .`` (it imports the installed library):

    python tests/install/e2e_check.py                 # synthetic corpus in a temp home
    python tests/install/e2e_check.py --fixtures DIR  # a prebuilt corpus (e.g. obfuscated)
    python tests/install/e2e_check.py --keep          # leave the temp home for inspection
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_fixtures  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(fixtures_dir: str | None, home: str | None, keep: bool) -> int:
    from thread_archive import _api as ta

    work = Path(tempfile.mkdtemp(prefix="thread-archive-e2e-"))
    corpus = Path(fixtures_dir) if fixtures_dir else work / "fixtures"
    arc_home = home or str(work / "home")

    if fixtures_dir:
        manifest = json.loads((corpus / "manifest.json").read_text())
        print(f"using prebuilt corpus: {corpus}")
    else:
        manifest = make_fixtures.generate(corpus)
        print(f"generated synthetic corpus: {corpus}")

    print(f"archive home: {arc_home}\n")
    ta.open_archive(arc_home)

    # 1. import every provider session
    imported = 0
    for imp in manifest["imports"]:
        res = ta.import_path(imp["path"], home=arc_home, provider=imp["provider"],
                             source_id=imp.get("source_id"))
        print(f"  imported {imp['provider']:12} {Path(imp['path']).name}  -> {res}")
        imported += 1
    if imported == 0:
        _fail("no sessions imported")

    # 2. rebuild the index purely from the JSONL truth — the lossless-rebuild contract
    counts = ta.reindex(home=arc_home)
    print(f"\nreindex: {counts}")
    if counts.get("threads", 0) <= 0 or counts.get("events", 0) <= 0:
        _fail(f"reindex produced no rows: {counts}")

    st = ta.status(home=arc_home)
    print(f"status:  threads={st['threads']} events={st['events']} fts_indexed={st['fts_indexed']}")
    if st["threads"] < imported:
        _fail(f"expected >= {imported} threads, got {st['threads']}")
    if st["fts_indexed"] <= 0:
        _fail("FTS index is empty after reindex")

    # 3. every provider's content is searchable by its marker
    print("\nsearch markers:")
    missing = []
    for provider, marker in manifest["markers"].items():
        hits = ta.search(marker, home=arc_home, limit=5)
        ok = bool(hits)
        print(f"  {provider:12} '{marker}' -> {len(hits)} hit(s) {'OK' if ok else 'MISSING'}")
        if not ok:
            missing.append(provider)
    if missing:
        _fail(f"markers not found for: {', '.join(missing)}")

    if not keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    print("\nE2E INSTALL CHECK PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="End-to-end install check for thread-archive.")
    p.add_argument("--fixtures", default=None, help="prebuilt corpus dir (with manifest.json); else synthetic")
    p.add_argument("--home", default=None, help="archive home (else a temp dir)")
    p.add_argument("--keep", action="store_true", help="keep the temp work dir")
    args = p.parse_args(argv)
    return run(args.fixtures, args.home, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
