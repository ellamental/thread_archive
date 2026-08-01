#!/usr/bin/env python3
"""The bench's corpora and built homes, packaged so another machine can run the gate.

The quick tier (``python -m search_lab gate --run --quick``) needs two kinds of
artifact this box built over hours: the pinned dataset files each harness reads,
and the built corpus homes — ingested, embedded, graph-persisted — each row scores
against. Both are deterministic derived data, so they travel the same way the
corpora are already pinned: as content-hashed bytes, not as a computation the
destination repeats. A machine that restores them runs the gate paying only for
query embedding, which is minutes of CPU.

    python -m search_lab packs build            # tar what this box has; print hashes
    python -m search_lab packs build --update   # …and accept the result into the manifest
    python -m search_lab packs publish          # upload the built tars as release assets
    python -m search_lab packs pull             # download + verify + extract what's missing
    python -m search_lab packs status           # what is local vs what the manifest names

**The manifest is the integrity story, not the release.** Release assets are
mutable — anyone with push access can replace one in place — so the checked-in
manifest (``bench-packs.json``, beside this file, same reasoning as
``dataset-pins.json``) records the sha256 a pack was accepted at, and ``pull``
refuses bytes that hash differently no matter what the release serves. Assets are
append-only by convention: a rebuilt pack gets a new release tag, never an upload
over an old name.

**What a pack is.** One gzip'd tar per artifact, rooted at the eval cache root
(:data:`eval_home.CACHE_ROOT`): ``dataset-<name>`` holds exactly the files
``dataset_pins.SOURCES`` declares (the bytes the pins row verifies), and
``home-<name>`` holds the built home directory whole. A home's own snapshot id is
content-derived, so a restored home reads back as the corpus the baseline rows
name — the gate's ``corpus_id`` check binds across machines with nothing extra.

**Hosting is a private repo, deliberately.** The homes contain the corpus text
itself (JSONL truth plus its FTS index), and several upstream datasets do not
clearly license public redistribution. ``publish``/``pull`` therefore go through
``gh``, which carries the caller's auth; CI supplies a fine-grained read token.
``pull --from DIR`` reads assets from a local directory instead — the offline
path, and what the tests drive.

**Missing is a fact about the box.** ``build`` packs what is present and says
what it skipped; ``pull`` fetches only what the manifest names. Neither invents
an error out of a corpus this machine never built — the same stance as the pins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dataset_pins  # noqa: E402
import eval_home  # noqa: E402

#: The accepted pack hashes. Beside the code, like the pins and the baseline:
#: "which bytes may the gate run against" is part of what a release means.
MANIFEST_PATH = Path(__file__).resolve().parent / "bench-packs.json"

#: Where `pull` records what it extracted, so a re-run skips work: one stamp per
#: pack under the cache root, carrying the tarball hash the extraction came from.
STAMP_DIR = ".bench-packs"

#: The gated quick tier's datasets (dataset_pins.SOURCES names) and built homes
#: (directory names under <cache>/homes). trec-covid and mtrag stay out: no
#: baseline row reads them, so no gate run needs them.
DATASETS = ("scifact", "nfcorpus", "locomo", "longmemeval", "beam", "perltqa", "cdr")
HOMES = ("scifact", "nfcorpus", "locomo", "longmemeval", "beam", "perltqa", "cdr")


# ── naming ───────────────────────────────────────────────────────────────────


def pack_names() -> list[str]:
    return [f"dataset-{d}" for d in DATASETS] + [f"home-{h}" for h in HOMES]


def pack_members(name: str, root: Path) -> list[Path]:
    """The absolute paths a pack carries, or [] when the box doesn't have them.

    Datasets resolve through ``dataset_pins.source_paths`` — the pack is exactly
    the bytes the pins verify, nothing beside them. Homes are the built directory
    whole: the index and its WAL, the truth log, the vector pack, the persisted
    corpus graph, the harness markers. Partial is treated as absent for datasets
    (the pins refuse to fingerprint a partial download; packing one would ship an
    identity the pins would then reject) but not for homes, whose contents differ
    legitimately by arm (a lexical-only build has no vector pack)."""
    kind, _, base = name.partition("-")
    if kind == "dataset":
        declared = dataset_pins.SOURCES[base].paths
        if any(not (root / rel).exists() for rel in declared):
            return []
        expanded: set[Path] = set()
        for rel in declared:
            p = root / rel
            if p.is_dir():
                expanded.update(q for q in p.rglob("*") if q.is_file())
            else:
                expanded.add(p)
        return sorted(expanded)
    home = root / "homes" / base
    if not home.is_dir():
        return []
    return sorted(p for p in home.rglob("*") if p.is_file())


# ── hashing / manifest ───────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """The accepted packs, or an empty manifest when none is recorded yet.

    Same contract as the baseline: absent means nothing accepted, unparseable is
    an error — a corrupted manifest that read as empty would verify nothing."""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"repo": DEFAULT_REPO, "release": None, "packs": {}}
    if not isinstance(blob, dict) or not isinstance(blob.get("packs"), dict):
        raise ValueError(f"{path} is not a bench-packs manifest")
    return blob


DEFAULT_REPO = "ellamental/thread-archive-bench-data"


# ── build ────────────────────────────────────────────────────────────────────


def build_pack(name: str, root: Path, out_dir: Path) -> Optional[dict[str, Any]]:
    """Tar one pack into ``out_dir``; returns its manifest entry, or None when
    the box doesn't have the artifact.

    Members are stored relative to the cache root in sorted order with owner and
    mtime normalized, so the same bytes on disk produce the same tarball and the
    hash names the content rather than the packing run."""
    members = pack_members(name, root)
    if not members:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"{name}.tar.gz"
    with tarfile.open(tar_path, "w:gz", compresslevel=6) as tar:
        for member in members:
            info = tar.gettarinfo(member, arcname=str(member.relative_to(root)))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with member.open("rb") as fh:
                tar.addfile(info, fh)
    return {
        "asset": tar_path.name,
        "sha256": sha256_file(tar_path),
        "bytes": tar_path.stat().st_size,
        "files": len(members),
    }


def cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.data_dir).expanduser()
    out_dir = Path(args.out).expanduser()
    manifest_path = Path(args.manifest).expanduser()
    manifest = load_manifest(manifest_path)
    built: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for name in pack_names():
        entry = build_pack(name, root, out_dir)
        if entry is None:
            skipped.append(name)
            continue
        built[name] = entry
        accepted = manifest["packs"].get(name, {}).get("sha256")
        mark = ("=accepted" if accepted == entry["sha256"]
                else "DIFFERS from accepted" if accepted else "unaccepted")
        print(f"  {name}: {entry['bytes']:>12,} bytes  {entry['sha256'][:16]}  [{mark}]")
    if skipped:
        print(f"  not on this box (skipped): {', '.join(skipped)}")
    if not built:
        print("nothing to pack — no gated corpus artifacts under " + str(root))
        return 1
    if args.update:
        if args.release:
            manifest["release"] = args.release
        if not manifest.get("release"):
            print("--update needs a release tag on the manifest; pass --release", file=sys.stderr)
            return 1
        manifest["packs"].update(built)
        manifest["accepted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        print(f"accepted {len(built)} packs into {manifest_path.name} "
              f"(release {manifest['release']})")
    else:
        print("(dry: pass --update to accept these hashes into the manifest)")
    return 0


# ── publish / pull ───────────────────────────────────────────────────────────


def _gh(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *argv], capture_output=True, text=True)


def cmd_publish(args: argparse.Namespace) -> int:
    """Upload built tars as assets on the manifest's release, creating it if absent.

    No ``--clobber``: an asset that already exists under a name is left alone and
    reported, which is what keeps the release append-only. A changed pack ships
    under a new release tag (build --update --release <new>), never over an old
    asset."""
    manifest = load_manifest(Path(args.manifest).expanduser())
    release, repo = manifest.get("release"), manifest.get("repo", DEFAULT_REPO)
    if not release:
        print("manifest names no release; run build --update --release <tag> first",
              file=sys.stderr)
        return 1
    out_dir = Path(args.out).expanduser()
    view = _gh("release", "view", release, "--repo", repo, "--json", "assets")
    if view.returncode != 0:
        create = _gh("release", "create", release, "--repo", repo,
                     "--title", release, "--notes",
                     "Content-addressed bench packs; hashes of record live in "
                     "thread_archive's search_lab/bench-packs.json.")
        if create.returncode != 0:
            print(f"cannot create release {release} on {repo}: {create.stderr.strip()}",
                  file=sys.stderr)
            return 1
        existing: set[str] = set()
    else:
        existing = {a["name"] for a in json.loads(view.stdout)["assets"]}
    failures = 0
    for name, entry in manifest["packs"].items():
        tar_path = out_dir / entry["asset"]
        if entry["asset"] in existing:
            print(f"  {name}: already on the release — leaving it (append-only)")
            continue
        if not tar_path.is_file():
            print(f"  {name}: {tar_path} not built on this box — skipped")
            continue
        if sha256_file(tar_path) != entry["sha256"]:
            print(f"  {name}: local tar does not match the accepted hash — rebuild "
                  f"and --update, or discard the stale tar", file=sys.stderr)
            failures += 1
            continue
        up = _gh("release", "upload", release, str(tar_path), "--repo", repo)
        if up.returncode != 0:
            print(f"  {name}: upload failed: {up.stderr.strip()}", file=sys.stderr)
            failures += 1
        else:
            print(f"  {name}: uploaded {entry['bytes']:,} bytes")
    return 1 if failures else 0


def _stamp_path(root: Path, name: str) -> Path:
    return root / STAMP_DIR / f"{name}.json"


def _read_stamp(root: Path, name: str) -> Optional[str]:
    try:
        return json.loads(_stamp_path(root, name).read_text())["sha256"]
    except (FileNotFoundError, ValueError, KeyError):
        return None


def extract_pack(tar_path: Path, root: Path, *, name: str, sha256: str) -> None:
    """Verify the tarball against the accepted hash, then extract into the root.

    Hash first, extraction second, stamp last — a mismatched download never
    touches the tree, and an extraction that died mid-way left no stamp, so the
    next pull repeats it. ``filter="data"`` is the stdlib's traversal guard: a
    member that resolves outside the root is a hard error, not a write."""
    actual = sha256_file(tar_path)
    if actual != sha256:
        raise SystemExit(f"refusing {name}: downloaded bytes hash {actual[:16]}, "
                         f"manifest accepts {sha256[:16]} — the asset moved under "
                         f"its name, which append-only publishing rules out")
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(root, filter="data")
    stamp = _stamp_path(root, name)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"sha256": sha256,
                                 "extracted_at": datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds")}) + "\n",
                     encoding="utf-8")


def cmd_pull(args: argparse.Namespace) -> int:
    root = Path(args.data_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(Path(args.manifest).expanduser())
    release, repo = manifest.get("release"), manifest.get("repo", DEFAULT_REPO)
    wanted = args.only or list(manifest["packs"])
    unknown = [n for n in wanted if n not in manifest["packs"]]
    if unknown:
        print(f"not in the manifest: {', '.join(unknown)}", file=sys.stderr)
        return 1
    failures = 0
    for name in wanted:
        entry = manifest["packs"][name]
        if _read_stamp(root, name) == entry["sha256"]:
            print(f"  {name}: present (stamp matches accepted hash)")
            continue
        with tempfile.TemporaryDirectory(prefix="bench-pack-") as tmp:
            tar_path = Path(tmp) / entry["asset"]
            if args.source:
                src = Path(args.source).expanduser() / entry["asset"]
                if not src.is_file():
                    print(f"  {name}: {src} absent from --from dir", file=sys.stderr)
                    failures += 1
                    continue
                shutil.copyfile(src, tar_path)
            else:
                if not release:
                    print("manifest names no release and no --from was given",
                          file=sys.stderr)
                    return 1
                dl = _gh("release", "download", release, "--repo", repo,
                         "--pattern", entry["asset"], "--dir", tmp)
                if dl.returncode != 0:
                    print(f"  {name}: download failed: {dl.stderr.strip()}",
                          file=sys.stderr)
                    failures += 1
                    continue
            extract_pack(tar_path, root, name=name, sha256=entry["sha256"])
            print(f"  {name}: fetched, verified, extracted ({entry['bytes']:,} bytes)")
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.data_dir).expanduser()
    manifest = load_manifest(Path(args.manifest).expanduser())
    print(f"repo {manifest.get('repo', DEFAULT_REPO)}  release {manifest.get('release')}")
    for name in pack_names():
        entry = manifest["packs"].get(name)
        if entry is None:
            state = "unaccepted (no manifest entry)"
        elif _read_stamp(root, name) == entry["sha256"]:
            state = "present, matches accepted"
        elif pack_members(name, root):
            state = "artifact on box, no matching pull stamp (locally built?)"
        else:
            state = "absent locally"
        print(f"  {name}: {state}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m search_lab packs",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", default=str(eval_home.CACHE_ROOT),
                    help="eval cache root the packs are built from / extracted into")
    ap.add_argument("--manifest", default=str(MANIFEST_PATH),
                    help="the accepted-hashes file (the checked-in one by default; "
                         "tests point this elsewhere)")
    sub = ap.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="tar the gated artifacts this box has")
    b.add_argument("--out", default=str(eval_home.CACHE_ROOT / "packs-out"),
                   help="where the tarballs land")
    b.add_argument("--update", action="store_true",
                   help="accept the built hashes into bench-packs.json")
    b.add_argument("--release", default=None,
                   help="release tag to record with --update (append-only: a "
                        "changed pack means a new tag)")
    b.set_defaults(func=cmd_build)

    p = sub.add_parser("publish", help="upload built tars as release assets (via gh)")
    p.add_argument("--out", default=str(eval_home.CACHE_ROOT / "packs-out"))
    p.set_defaults(func=cmd_publish)

    d = sub.add_parser("pull", help="download+verify+extract accepted packs")
    d.add_argument("--only", nargs="*", default=None, help="pack names to restrict to")
    d.add_argument("--from", dest="source", default=None,
                   help="read assets from this local directory instead of the release")
    d.set_defaults(func=cmd_pull)

    s = sub.add_parser("status", help="local state vs the manifest")
    s.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
