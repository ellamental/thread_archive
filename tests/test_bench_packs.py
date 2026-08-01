"""Bench packs: the gate's corpora and homes, moved between machines as bytes.

The manifest is the integrity story — release assets are mutable, so what
``pull`` extracts must be exactly what ``build --update`` accepted, and a tar
that hashes differently must never touch the tree. What must hold, and what
these cover:

- a pack's hash names its *content*: the same files pack to the same sha256
  across separate builds, and a changed file changes it
- a dataset pack is all-or-nothing (the pins refuse a partial corpus, so a pack
  of one would ship an identity the pins then reject); a home pack tolerates a
  layout that differs by arm
- the build→pull roundtrip restores byte-identical files at the same relative
  paths under a different root
- ``pull`` refuses a tarball whose bytes do not hash to the accepted value,
  before extracting anything
- a matching stamp short-circuits the fetch; a pack absent from the ``--from``
  directory is a named failure, not a silent skip

Run against real files, real tars, and a manifest in a tmp tree — the checked-in
manifest is never read or written here.
"""

from __future__ import annotations

import json

import pytest

from search_lab import bench_packs, dataset_pins


def _dataset_tree(root, dataset="locomo", body=b"one\ntwo\n"):
    for rel in dataset_pins.SOURCES[dataset].paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return root


def _home_tree(root, name="perltqa"):
    home = root / "homes" / name
    (home / "truth").mkdir(parents=True)
    (home / "truth" / "events.jsonl").write_bytes(b'{"e":1}\n')
    (home / "index.db").write_bytes(b"sqlite-bytes")
    return root


# ── members ──────────────────────────────────────────────────────────────────


def test_dataset_pack_is_all_or_nothing(tmp_path):
    root = _dataset_tree(tmp_path, "scifact")
    assert bench_packs.pack_members("dataset-scifact", root)
    (root / dataset_pins.SOURCES["scifact"].paths[0]).unlink()
    assert bench_packs.pack_members("dataset-scifact", root) == []


def test_home_pack_carries_whatever_the_build_left(tmp_path):
    root = _home_tree(tmp_path)
    members = bench_packs.pack_members("home-perltqa", root)
    assert [m.name for m in members] == ["index.db", "events.jsonl"]
    assert bench_packs.pack_members("home-cdr", root) == []


# ── hashing ──────────────────────────────────────────────────────────────────


def test_same_content_packs_to_same_hash(tmp_path):
    root = _dataset_tree(tmp_path / "tree", "locomo")
    one = bench_packs.build_pack("dataset-locomo", root, tmp_path / "out1")
    two = bench_packs.build_pack("dataset-locomo", root, tmp_path / "out2")
    assert one["sha256"] == two["sha256"]


def test_changed_content_changes_the_hash(tmp_path):
    root = _dataset_tree(tmp_path / "tree", "locomo")
    one = bench_packs.build_pack("dataset-locomo", root, tmp_path / "out1")
    _dataset_tree(tmp_path / "tree", "locomo", body=b"three\n")
    two = bench_packs.build_pack("dataset-locomo", root, tmp_path / "out2")
    assert one["sha256"] != two["sha256"]


# ── the roundtrip ────────────────────────────────────────────────────────────


def _built_manifest(tmp_path, root):
    """Run ``build --update`` against a tmp manifest; return its path."""
    manifest = tmp_path / "manifest.json"
    out = tmp_path / "assets"
    rc = bench_packs.main(["--data-dir", str(root), "--manifest", str(manifest),
                           "build", "--out", str(out), "--update",
                           "--release", "packs-test"])
    assert rc == 0
    return manifest, out


def test_build_pull_roundtrip_restores_identical_bytes(tmp_path):
    root = _home_tree(_dataset_tree(tmp_path / "src", "locomo"))
    manifest, assets = _built_manifest(tmp_path, root)
    dest = tmp_path / "dest"
    rc = bench_packs.main(["--data-dir", str(dest), "--manifest", str(manifest),
                           "pull", "--from", str(assets)])
    assert rc == 0
    for rel in dataset_pins.SOURCES["locomo"].paths:
        assert (dest / rel).read_bytes() == (root / rel).read_bytes()
    home_rel = "homes/perltqa/truth/events.jsonl"
    assert (dest / home_rel).read_bytes() == (root / home_rel).read_bytes()


def test_pull_refuses_bytes_that_hash_differently(tmp_path):
    root = _dataset_tree(tmp_path / "src", "locomo")
    manifest, assets = _built_manifest(tmp_path, root)
    blob = json.loads(manifest.read_text())
    asset = blob["packs"]["dataset-locomo"]["asset"]
    (assets / asset).write_bytes((assets / asset).read_bytes() + b"x")
    dest = tmp_path / "dest"
    with pytest.raises(SystemExit, match="refusing dataset-locomo"):
        bench_packs.main(["--data-dir", str(dest), "--manifest", str(manifest),
                          "pull", "--from", str(assets)])
    assert not (dest / dataset_pins.SOURCES["locomo"].paths[0]).exists()


def test_matching_stamp_short_circuits_the_fetch(tmp_path):
    root = _dataset_tree(tmp_path / "src", "locomo")
    manifest, assets = _built_manifest(tmp_path, root)
    dest = tmp_path / "dest"
    argv = ["--data-dir", str(dest), "--manifest", str(manifest),
            "pull", "--from", str(assets)]
    assert bench_packs.main(argv) == 0
    for tar in assets.iterdir():
        tar.unlink()  # a second pull that fetched anything would now fail
    assert bench_packs.main(argv) == 0


def test_asset_missing_from_source_dir_is_a_failure(tmp_path):
    root = _dataset_tree(tmp_path / "src", "locomo")
    manifest, _ = _built_manifest(tmp_path, root)
    rc = bench_packs.main(["--data-dir", str(tmp_path / "dest"),
                           "--manifest", str(manifest),
                           "pull", "--from", str(tmp_path / "empty")])
    assert rc == 1
