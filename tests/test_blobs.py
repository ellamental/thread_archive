"""The blob store: binary payload content as content-addressed truth files.

The contract under test (see ``_truth.blobs``): import extracts base64
image/document content out of payloads into ``truth/blobs/`` — new truth
carries refs, not megabytes of base64 — while staying *exactly invertible*, so
dedup keys computed over the inline form at parse time keep validating (and
deduplicating) the extracted form. Renderers show real file paths (MCP) and
blob URLs (web); redaction shreds the files; historical inline payloads
materialize lazily on read.
"""

from __future__ import annotations

import base64
import json
import os

from sqlalchemy import text

from tests.helpers import write_jsonl
from thread_archive import _api as ta
from thread_archive._store import get_session
from thread_archive._truth import blobs

PNG_BYTES = b"\x89PNG-fake-" + os.urandom(4096)
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def _cc_image_session(name: str, data_b64: str = PNG_B64) -> list[dict]:
    """A CC session: a user turn with a pasted screenshot, an assistant tool
    call, and a tool result carrying an image block (a browser screenshot)."""
    return [
        {
            "type": "user", "uuid": f"u-{name}", "timestamp": "2026-01-01T10:00:00Z",
            "cwd": "/proj",
            "message": {"role": "user", "content": [
                {"type": "text", "text": f"look at this screenshot ({name})"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": data_b64}},
            ]},
        },
        {
            "type": "assistant", "uuid": f"a-{name}", "timestamp": "2026-01-01T10:00:05Z",
            "message": {"role": "assistant", "model": "claude-opus-4", "content": [
                {"type": "text", "text": "taking a screenshot"},
                {"type": "tool_use", "id": f"t-{name}", "name": "screenshot", "input": {}},
            ]},
        },
        {
            "type": "user", "uuid": f"r-{name}", "timestamp": "2026-01-01T10:00:06Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t-{name}", "content": [
                    {"type": "text", "text": "here you go"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": data_b64}},
                ]},
            ]},
        },
    ]


def _import_image_session(tmp_path, name: str = "img", data_b64: str = PNG_B64):
    f = tmp_path / f"{name}.jsonl"
    write_jsonl(f, _cc_image_session(name, data_b64))
    return f, ta.import_path(f)


def _blob_files(archive_home) -> list:
    d = archive_home / "truth" / "blobs"
    return sorted(p for p in d.rglob("*") if p.is_file()) if d.exists() else []


# ── the transform ────────────────────────────────────────────────────────────
def test_extract_reconstitute_round_trip(tmp_path):
    payload = {
        "content": "look",
        "images": [{"media_type": "image/png", "data": PNG_B64}],
        "output": [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": PNG_B64}}],
    }
    extracted, n = blobs.extract_blobs(payload, d=tmp_path)
    assert n == 2
    assert "data" not in extracted["images"][0]
    assert extracted["images"][0]["blob_hash"] == extracted["output"][0]["source"]["blob_hash"]
    assert extracted["images"][0]["blob_bytes"] == len(PNG_BYTES)
    # content-addressed: the same bytes twice are one file
    assert len(list((tmp_path / "blobs").rglob("*.png"))) == 1
    restored, missing = blobs.reconstitute_blobs(extracted, d=tmp_path)
    assert missing == 0
    assert restored == payload


def test_small_and_dirty_base64_stay_inline(tmp_path):
    small = {"media_type": "image/png", "data": base64.b64encode(b"tiny").decode()}
    # whitespace-wrapped base64 can't round-trip byte-exactly — must stay inline
    dirty = {"media_type": "image/png", "data": PNG_B64[:100] + "\n" + PNG_B64[100:]}
    for payload in (small, dirty):
        extracted, n = blobs.extract_blobs(dict(payload), d=tmp_path)
        assert n == 0
        assert extracted == payload
    assert _blob_files_under(tmp_path) == []


def _blob_files_under(d):
    b = d / "blobs"
    return sorted(p for p in b.rglob("*") if p.is_file()) if b.exists() else []


def test_missing_blob_counts_as_missing(tmp_path):
    extracted, _ = blobs.extract_blobs(
        {"images": [{"media_type": "image/png", "data": PNG_B64}]}, d=tmp_path)
    for f in _blob_files_under(tmp_path):
        f.unlink()
    restored, missing = blobs.reconstitute_blobs(extracted, d=tmp_path)
    assert missing == 1
    assert restored == extracted  # the ref is left in place, not dropped


# ── import: extraction at the write seam ─────────────────────────────────────
def test_import_extracts_and_dedups(tmp_path, archive_home):
    f, result = _import_image_session(tmp_path)
    files = _blob_files(archive_home)
    assert len(files) == 1  # user paste + tool result = same bytes, one file
    assert files[0].read_bytes() == PNG_BYTES

    # no base64 of the image survives anywhere in the truth JSONL or the index
    truth_files = list((archive_home / "truth" / "threads").rglob("*.jsonl"))
    for tf in truth_files:
        assert PNG_B64[:64].encode() not in tf.read_bytes()
    with get_session() as s:
        n = s.execute(text("SELECT count(*) FROM events WHERE payload LIKE :p"),
                      {"p": f"%{PNG_B64[:64]}%"}).scalar()
        assert n == 0
        fts = s.execute(text("SELECT count(*) FROM events_fts WHERE content LIKE :p"),
                        {"p": f"%{PNG_B64[:64]}%"}).scalar()
        assert fts == 0

    # a re-import of the same file adds nothing: the dedup key was computed over
    # the inline form, and the extracted store still answers to it
    again = ta.import_path(f)
    with get_session() as s:
        per_key = s.execute(text(
            "SELECT count(*) FROM events GROUP BY thread_id, dedup_key "
            "HAVING count(*) > 1")).fetchall()
    assert per_key == []
    assert (again.events_created if hasattr(again, "events_created") else 0) == 0


def test_verify_hash_gate_and_reindex(tmp_path, archive_home):
    _import_image_session(tmp_path)
    from thread_archive._ops.verify import _hash_scan_truth_dir

    scan = _hash_scan_truth_dir(archive_home / "truth", None)
    assert scan["mismatched"] == 0
    assert scan["checked"] > 0

    # reindex rebuilds the projection from extracted truth without loss. Close
    # first: unlinking the index under a live engine leaves pooled connections
    # bound to the deleted inode, and the next one reused raises "disk I/O error".
    ta.close()
    (archive_home / "index.db").unlink()
    ta.reindex()
    scan = _hash_scan_truth_dir(archive_home / "truth", None)
    assert scan["mismatched"] == 0

    # a lost blob file is lost content: the gate goes red, not silent
    for f in _blob_files(archive_home):
        f.unlink()
    scan = _hash_scan_truth_dir(archive_home / "truth", None)
    assert scan["mismatched"] > 0


# ── redaction ────────────────────────────────────────────────────────────────
def test_redact_shreds_blob_and_unredact_restores(tmp_path, archive_home):
    _import_image_session(tmp_path)
    from thread_archive._ops import redact as rd

    with get_session() as s:
        tid = s.execute(text("SELECT DISTINCT thread_id FROM events")).scalar()
    result = rd.redact_events(tid, None)  # whole thread
    assert result["blobs_shredded"] == 1
    assert _blob_files(archive_home) == []
    # nothing under the home still holds the image bytes outside the ciphertext
    holders = [p for p in archive_home.rglob("*")
               if p.is_file() and PNG_B64[:64].encode() in p.read_bytes()]
    assert holders == []

    restored = rd.unredact(result["key_id"])
    assert restored["events_restored"] > 0
    # the bundle carried the content inline — the payload is whole again
    with get_session() as s:
        payloads = [json.loads(r[0]) if isinstance(r[0], str) else r[0]
                    for r in s.execute(text("SELECT payload FROM events"))]
    inline = [p for p in payloads if PNG_B64 in json.dumps(p)]
    assert inline, "unredact must restore the image content"
    # and the restored (inline) form still validates against its dedup key
    from thread_archive._ops.verify import _hash_scan_truth_dir

    assert _hash_scan_truth_dir(archive_home / "truth", None)["mismatched"] == 0


def test_redact_keeps_blob_shared_with_live_thread(tmp_path, archive_home):
    _import_image_session(tmp_path, "one")
    _import_image_session(tmp_path, "two")  # same bytes, second thread
    from thread_archive._ops import redact as rd

    with get_session() as s:
        tids = sorted(r[0] for r in s.execute(
            text("SELECT DISTINCT thread_id FROM events")))
    assert len(tids) == 2
    result = rd.redact_events(tids[0], None)
    assert result["blobs_shredded"] == 0  # thread two still references the hash
    assert len(_blob_files(archive_home)) == 1


# ── rendering ────────────────────────────────────────────────────────────────
def test_read_renders_image_paths(tmp_path, archive_home):
    _import_image_session(tmp_path)
    with get_session() as s:
        tid = s.execute(text("SELECT DISTINCT thread_id FROM events")).scalar()

    user_view = ta.read_thread(tid, mode="user")
    assert "[image image/png 4 KB — " in user_view
    assert "/truth/blobs/" in user_view

    full = ta.read_thread(tid, mode="full", tool_results=True)
    assert full.count("/truth/blobs/") >= 2  # the paste and the tool result

    structured = ta.read_thread_structured(tid)
    user_blocks = [b for m in structured["messages"] if m["role"] == "user"
                   for b in m["blocks"] if b.get("images")]
    assert user_blocks
    img = user_blocks[0]["images"][0]
    assert img["url"].startswith("/api/blob/") and img["url"].endswith(".png")
    assert img["media_type"] == "image/png" and img["kind"] == "image"


def test_image_only_turn_is_not_dropped(tmp_path, archive_home):
    f = tmp_path / "solo.jsonl"
    write_jsonl(f, [
        {
            "type": "user", "uuid": "u-solo", "timestamp": "2026-01-01T10:00:00Z",
            "cwd": "/proj",
            "message": {"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": PNG_B64}},
            ]},
        },
        {
            "type": "assistant", "uuid": "a-solo", "timestamp": "2026-01-01T10:00:05Z",
            "message": {"role": "assistant", "model": "claude-opus-4",
                        "content": [{"type": "text", "text": "nice screenshot"}]},
        },
    ])
    ta.import_path(f)
    with get_session() as s:
        tid = s.execute(text("SELECT DISTINCT thread_id FROM events")).scalar()
    assert "[image image/png" in ta.read_thread(tid, mode="user")


def test_inline_history_materializes_on_read(tmp_path, archive_home):
    """Historical truth (inline base64, written before extraction existed) renders
    to a real path via lazy materialization — no migration, no truth rewrite."""
    small = base64.b64encode(b"small-but-real-image-bytes").decode()
    _import_image_session(tmp_path, "hist", data_b64=small)  # sub-floor: stays inline
    truth_files = list((archive_home / "truth" / "threads").rglob("*.jsonl"))
    assert any(small.encode() in tf.read_bytes() for tf in truth_files)
    assert _blob_files(archive_home) == []

    with get_session() as s:
        tid = s.execute(text("SELECT DISTINCT thread_id FROM events")).scalar()
    view = ta.read_thread(tid, mode="user")
    assert "/truth/blobs/" in view  # materialized on read
    assert len(_blob_files(archive_home)) == 1
    # the truth is untouched — inline stays inline
    assert any(small.encode() in tf.read_bytes() for tf in truth_files)


# ── the web surface ──────────────────────────────────────────────────────────
def test_web_blob_endpoint(tmp_path, archive_home):
    _import_image_session(tmp_path)
    from thread_archive._web import route

    name = _blob_files(archive_home)[0].name
    status, ctype, body, headers = route("GET", f"/api/blob/{name}", {})
    assert status == 200
    assert ctype == "image/png"
    assert body == PNG_BYTES
    assert "immutable" in headers.get("Cache-Control", "")

    # hash-only (no extension) resolves too
    status, _, body, _ = route("GET", f"/api/blob/{name.split('.')[0]}", {})
    assert status == 200 and body == PNG_BYTES

    for bad in ("../../etc/passwd", "zz" * 32, "a" * 63, f"{'0' * 64}.png"):
        status, _, _, _ = route("GET", f"/api/blob/{bad}", {})
        assert status == 404
