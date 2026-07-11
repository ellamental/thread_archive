"""The family manifest writer — ``host/write-manifest.py``.

Shape per the thread monorepo's ``docs/spec/product-json.md``: three required
fields (v/name/version), optional surfaces only when they exist, atomic write
into the archive home.

The writer is installer machinery, not library code: it lives in ``host/``,
outside the package, and is loaded here by path. An sdist ships ``tests/`` but
not ``host/``, so these skip when the script isn't there.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "host" / "write-manifest.py"


def _load():
    if not SCRIPT.exists():
        pytest.skip("host/ is not part of the packaged distribution")
    spec = importlib.util.spec_from_file_location("_host_write_manifest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_manifest_required_fields(tmp_path):
    dest, manifest = _load().build_manifest(home=str(tmp_path))
    assert dest == tmp_path / "product.json"
    assert manifest["v"] == 1
    assert manifest["name"] == "thread-archive"
    assert isinstance(manifest["version"], str) and manifest["version"]
    assert manifest["stores"] == {"archive": str(tmp_path)}
    assert manifest["console"]["url"].startswith("http://127.0.0.1:")
    assert manifest["health"]["url"].endswith("/api/health")


def test_build_manifest_no_web_omits_surfaces(tmp_path):
    _, manifest = _load().build_manifest(home=str(tmp_path), web=False)
    assert "console" not in manifest
    assert "health" not in manifest


def test_write_manifest_atomic_and_parseable(tmp_path):
    dest = _load().write_manifest(home=str(tmp_path))
    on_disk = json.loads(dest.read_text())
    assert on_disk["name"] == "thread-archive"
    assert on_disk["written_at"]
    # no temp-file residue from the atomic write
    assert list(tmp_path.glob("*.tmp")) == []
