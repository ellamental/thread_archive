"""The truth directory's ``manifest.json``: resilient reads, serialized writes.

* A corrupt or deleted manifest on a sharded archive infers its shard depth
  from the directory layout instead of silently resetting writers to the flat
  layout.
* Manifest writers (checkpoint, truth re-emit, ``update_manifest``) are locked
  read-modify-writes that mutate only their own keys, so a foreign key (e.g.
  verify's hashes baseline) written concurrently survives.
"""

from __future__ import annotations

from thread_archive import _api as ta
from thread_archive._truth import jsonl_log

from .helpers import import_cc_session


def test_manifest_corruption_infers_shard_depth_from_layout(
    archive_home, tmp_path, monkeypatch
) -> None:
    import_cc_session(tmp_path, "a")
    import_cc_session(tmp_path, "b")
    d = archive_home / "truth"

    monkeypatch.setattr(jsonl_log.layout, "FLAT_MAX", 1)  # force a rebalance at 2 threads
    jsonl_log.checkpoint(snapshots=False)
    assert jsonl_log._shard_depth(d) >= 1
    depth = jsonl_log._shard_depth(d)

    # Corrupt manifest: depth must be inferred from the bucket dirs, never reset flat.
    (d / "manifest.json").write_text("{corrupt", encoding="utf-8")
    assert jsonl_log._read_manifest(d)["shard_depth"] == depth

    # Deleted manifest: same inference.
    (d / "manifest.json").unlink()
    assert jsonl_log._read_manifest(d)["shard_depth"] == depth

    # A flat archive (no bucket dirs) still infers 0.
    assert jsonl_log._infer_shard_depth(tmp_path / "nowhere") == 0


def test_checkpoint_preserves_foreign_manifest_keys(archive_home, tmp_path, monkeypatch) -> None:
    import_cc_session(tmp_path)
    d = archive_home / "truth"
    jsonl_log.checkpoint()

    baseline = {"at": "2026-01-01T00:00:00+00:00", "truth_mismatched": 0, "index_mismatched": 0}
    orig = jsonl_log.checkpoint_changed_threads

    def sneaky(dd, depth, last_iso):
        # A verify --hashes run landing mid-checkpoint, after the manifest read.
        m = jsonl_log._read_manifest(dd)
        m["hashes_baseline"] = baseline
        jsonl_log._write_manifest(dd, m)
        return orig(dd, depth, last_iso)

    monkeypatch.setattr(jsonl_log.maintenance, "checkpoint_changed_threads", sneaky)
    jsonl_log.checkpoint()
    assert jsonl_log._read_manifest(d).get("hashes_baseline") == baseline


def test_truth_reemit_preserves_foreign_manifest_keys(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    d = archive_home / "truth"
    jsonl_log.checkpoint()

    baseline = {"at": "2026-01-01T00:00:00+00:00", "truth_mismatched": 0, "index_mismatched": 0}
    m = jsonl_log._read_manifest(d)
    m["hashes_baseline"] = baseline
    jsonl_log._write_manifest(d, m)

    jsonl_log.rebuild_truth_from_store()
    m = jsonl_log._read_manifest(d)
    assert m.get("hashes_baseline") == baseline
    assert "shard_depth" in m and "last_checkpoint_at" in m


def test_update_manifest_preserves_foreign_keys(archive_home, tmp_path):
    import_cc_session(tmp_path)
    d = jsonl_log.log_dir()
    jsonl_log.update_manifest(d, lambda m: m.__setitem__("custom_key", "survives"))
    ta.checkpoint()  # stamps shard_depth / last_checkpoint_at via update_manifest
    m = jsonl_log._read_manifest(d)
    assert m["custom_key"] == "survives"
    assert m["last_checkpoint_at"] is not None

    # verify --hashes stamps its baseline the same way, preserving others.
    ta.verify(hashes=True)
    m = jsonl_log._read_manifest(d)
    assert m["custom_key"] == "survives"
    assert "hashes_baseline" in m


def test_future_format_version_is_refused(archive_home, tmp_path) -> None:
    # A reader must never interpret a newer truth layout with old assumptions
    # (sharding or record semantics may have changed) — it refuses instead.
    import json

    import pytest

    import_cc_session(tmp_path)
    d = archive_home / "truth"
    jsonl_log.checkpoint()

    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["version"] == jsonl_log.TRUTH_FORMAT_VERSION

    m["version"] = jsonl_log.TRUTH_FORMAT_VERSION + 1
    (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(jsonl_log.TruthFormatError):
        jsonl_log._read_manifest(d)
