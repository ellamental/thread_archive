"""The family manifest writer — ``python -m thread_archive.manifest``.

Shape per the thread monorepo's ``docs/spec/product-json.md``: three required
fields (v/name/version), optional surfaces only when they exist, atomic write
into the archive home.
"""

import json

from thread_archive.manifest import build_manifest, write_manifest


def test_build_manifest_required_fields(tmp_path):
    dest, manifest = build_manifest(home=str(tmp_path))
    assert dest == tmp_path / "product.json"
    assert manifest["v"] == 1
    assert manifest["name"] == "thread-archive"
    assert isinstance(manifest["version"], str) and manifest["version"]
    assert manifest["stores"] == {"archive": str(tmp_path)}
    assert manifest["console"]["url"].startswith("http://127.0.0.1:")
    assert manifest["health"]["url"].endswith("/api/health")


def test_build_manifest_no_web_omits_surfaces(tmp_path):
    _, manifest = build_manifest(home=str(tmp_path), web=False)
    assert "console" not in manifest
    assert "health" not in manifest


def test_write_manifest_atomic_and_parseable(tmp_path):
    dest = write_manifest(home=str(tmp_path))
    on_disk = json.loads(dest.read_text())
    assert on_disk["name"] == "thread-archive"
    assert on_disk["written_at"]
    # no temp-file residue from the atomic write
    assert list(tmp_path.glob("*.tmp")) == []
