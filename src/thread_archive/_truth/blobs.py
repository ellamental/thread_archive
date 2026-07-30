"""Content-addressed blob store: binary payload content as files beside the truth.

Pasted screenshots, tool-result images, and base64 documents arrive inside event
payloads as base64 strings. Left inline they bloat the greppable JSONL, get
json-dumped into FTS shadow rows, and render as opaque reprs. This module gives
that content a home of its own:

    truth/blobs/<hh>/<sha256hex><ext>     # hh = first two hex chars of the hash

Blob files are truth — the bytes are conversation content, so the directory
lives inside the truth dir and rides every backup/mirror of it. They are
content-addressed (the filename IS the sha256 of the raw bytes), which makes
writes idempotent and duplicates free: the same screenshot pasted into five
threads is one file.

**The extraction transform is exactly invertible.** ``extract_blobs`` replaces,
anywhere in a payload, a dict carrying ``{"data": <base64 str>, "media_type":
...}`` with the same dict minus ``data`` plus ``{"blob_hash": <sha256>,
"blob_bytes": <n>}`` — every other key (``type``, ``seq``, whatever) is left
untouched. ``reconstitute_blobs`` is the inverse: it reads the blob file back
and restores the exact original ``data`` string. Exactness matters because the
dedup key's content hash is computed over the *inline* form at parse time (see
``event_builder.compute_content_hash``): the hash gate re-hashes stored
payloads against their keys, and for a blob-bearing payload it must be able to
reproduce the inline bytes byte-for-byte (``_truth.rebuild._hash_key_check``
reconstitutes on mismatch). Extraction therefore only fires when the base64
round-trips exactly (decode → re-encode reproduces the string); data with
nonstandard padding or embedded newlines stays inline, which is always safe.

Two producers write blobs:

* the import write seam (``drain.write_events``) extracts every new event's
  payload before it reaches SQLite or the truth file, so new truth never
  carries inline base64 above the size floor;
* the renderers *materialize on read*: historical events whose base64 is still
  inline get their bytes written here (content-addressed, so idempotent) the
  first time something wants to show them — no truth rewrite involved.

Crash ordering: a blob file is fsynced into place before the payload
referencing it is staged, so truth never references a blob that isn't durable.
An orphaned blob (crash between the two) is harmless dead weight, not damage.

Out of scope for now: codex ``input_image`` data-URI *strings* (a string must
stay a string, and rewriting it would need its own inverse) stay inline.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from .layout import _fsync_dir, log_dir

logger = logging.getLogger(__name__)

BLOBS_SUBDIR = "blobs"

# Decoded-size floor below which base64 stays inline: tiny payloads (icons,
# 1×1 pixels) aren't worth a file each, and the JSONL they'd save is trivial.
BLOB_MIN_BYTES = 1024

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# media_type → filename extension. The extension is a courtesy (agents Read the
# path, humans browse the directory, the web layer maps it back to a
# Content-Type); identity is the hash alone.
_EXT_BY_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
}
_MEDIA_TYPE_BY_EXT = {v: k for k, v in _EXT_BY_MEDIA_TYPE.items()}


def blobs_dir(d: Optional[Path] = None) -> Path:
    return (d or log_dir()) / BLOBS_SUBDIR


def ext_for_media_type(media_type: Optional[str]) -> str:
    return _EXT_BY_MEDIA_TYPE.get((media_type or "").lower(), ".bin")


def media_type_for_path(path: Path) -> str:
    return _MEDIA_TYPE_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def store_bytes(raw: bytes, media_type: Optional[str], *, d: Optional[Path] = None) -> str:
    """Write ``raw`` into the blob store (idempotent) and return its sha256 hex.

    tmp + fsync + rename, then a dir fsync — the same durability discipline as
    truth appends, because a payload referencing this hash may be fsynced into
    the truth moments later."""
    digest = hashlib.sha256(raw).hexdigest()
    bucket = blobs_dir(d) / digest[:2]
    path = bucket / f"{digest}{ext_for_media_type(media_type)}"
    if path.exists():
        return digest
    bucket.mkdir(parents=True, exist_ok=True)
    # Writer-unique tmp name: several processes (watcher import, an MCP read
    # materializing, the web server) may store the same content concurrently;
    # a shared tmp path would let their writes interleave. Identical content
    # means whoever renames last wins with the same bytes.
    tmp = bucket / f".{digest}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(bucket)
    return digest


def blob_file(
    blob_hash: str,
    media_type: Optional[str] = None,
    *,
    ext: Optional[str] = None,
    d: Optional[Path] = None,
) -> Optional[Path]:
    """The on-disk file for ``blob_hash``, or None when absent (or the hash is
    malformed — callers pass payload data here, so refuse garbage rather than
    globbing with it).

    ``ext`` pins the answer to one extension instead of taking whichever twin the
    glob reaches first. Identity is the hash, but the *extension* is what a
    served response's Content-Type is read off, and one content can be stored
    under several — the same bytes declared ``image/png`` in one message and
    ``image/svg+xml`` in another are two files. A caller that already told
    somebody which form it was handing over (a URL carries the extension) must
    resolve that form or none, so the type it promised is the type it serves."""
    if not isinstance(blob_hash, str) or not _HASH_RE.match(blob_hash):
        return None
    bucket = blobs_dir(d) / blob_hash[:2]
    if ext is not None:
        candidate = bucket / f"{blob_hash}{ext}"
        return candidate if candidate.is_file() else None
    if media_type:
        candidate = bucket / f"{blob_hash}{ext_for_media_type(media_type)}"
        if candidate.exists():
            return candidate
    for candidate in bucket.glob(f"{blob_hash}.*"):
        if not candidate.name.endswith(".tmp"):
            return candidate
    return None


def is_blob_ref(value: Any) -> bool:
    """True for a dict produced by :func:`extract_blobs` — carries ``blob_hash``,
    no longer carries ``data``."""
    return isinstance(value, dict) and isinstance(value.get("blob_hash"), str) and "data" not in value


def _extractable(value: Any) -> Optional[bytes]:
    """The decoded bytes when ``value`` is a dict whose ``data`` is extractable
    base64 (media-typed, above the floor, exactly round-trippable), else None."""
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, str) or not isinstance(value.get("media_type"), str):
        return None
    if len(data) < (BLOB_MIN_BYTES * 4) // 3:  # cheap pre-filter before decoding
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return None
    if len(raw) < BLOB_MIN_BYTES:
        return None
    # The inverse must reproduce the exact string the dedup hash was fed.
    if base64.b64encode(raw).decode("ascii") != data:
        return None
    return raw


def extract_blobs(value: Any, *, d: Optional[Path] = None) -> tuple[Any, int]:
    """Recursively replace extractable base64 ``data`` fields with blob refs.

    Returns ``(new_value, blobs_extracted)``. Pure: the input structure is not
    mutated; untouched subtrees are shared, and when nothing matches the
    original object is returned as-is."""
    if isinstance(value, dict):
        raw = _extractable(value)
        if raw is not None:
            digest = store_bytes(raw, value["media_type"], d=d)
            out = {k: v for k, v in value.items() if k != "data"}
            out["blob_hash"] = digest
            out["blob_bytes"] = len(raw)
            return out, 1
        n = 0
        items: dict = {}
        changed = False
        for k, v in value.items():
            new_v, sub = extract_blobs(v, d=d)
            n += sub
            changed = changed or new_v is not v
            items[k] = new_v
        return (items if changed else value), n
    if isinstance(value, list):
        n = 0
        out_list = []
        changed = False
        for v in value:
            new_v, sub = extract_blobs(v, d=d)
            n += sub
            changed = changed or new_v is not v
            out_list.append(new_v)
        return (out_list if changed else value), n
    return value, 0


def has_blob_refs(value: Any) -> bool:
    """True when the structure contains at least one blob ref anywhere."""
    if isinstance(value, dict):
        if is_blob_ref(value):
            return True
        return any(has_blob_refs(v) for v in value.values())
    if isinstance(value, list):
        return any(has_blob_refs(v) for v in value)
    return False


def reconstitute_blobs(value: Any, *, d: Optional[Path] = None) -> tuple[Any, int]:
    """The inverse of :func:`extract_blobs`: restore inline ``data`` from blob
    files. Returns ``(new_value, missing)`` — ``missing`` counts refs whose blob
    file is gone (those refs are left in place)."""
    if isinstance(value, dict):
        if is_blob_ref(value):
            path = blob_file(value["blob_hash"], value.get("media_type"), d=d)
            if path is None:
                return value, 1
            try:
                raw = path.read_bytes()
            except OSError:
                return value, 1
            out = {k: v for k, v in value.items() if k not in ("blob_hash", "blob_bytes")}
            out["data"] = base64.b64encode(raw).decode("ascii")
            return out, 0
        missing = 0
        items: dict = {}
        changed = False
        for k, v in value.items():
            new_v, sub = reconstitute_blobs(v, d=d)
            missing += sub
            changed = changed or new_v is not v
            items[k] = new_v
        return (items if changed else value), missing
    if isinstance(value, list):
        missing = 0
        out_list = []
        changed = False
        for v in value:
            new_v, sub = reconstitute_blobs(v, d=d)
            missing += sub
            changed = changed or new_v is not v
            out_list.append(new_v)
        return (out_list if changed else value), missing
    return value, 0


def materialize(value: dict, *, d: Optional[Path] = None) -> Optional[Path]:
    """A readable file path for one image/document dict, whichever form it's in.

    A blob ref resolves to its file; an inline dict is written into the store
    first (content-addressed, so repeat reads are free) — this is the lazy path
    that makes *historical* truth (still inline) viewable without any
    migration. Deliberately more lenient than :func:`extract_blobs`: viewing
    doesn't need the exact-round-trip guarantee, so sub-floor and
    whitespace-wrapped base64 materialize too. Returns None when the dict is
    neither form (pointer-only refs) or on any failure — rendering must
    degrade, never raise."""
    try:
        if is_blob_ref(value):
            return blob_file(value["blob_hash"], value.get("media_type"), d=d)
        data = value.get("data")
        if isinstance(data, str) and isinstance(value.get("media_type"), str) and data:
            raw = base64.b64decode(data)
            if raw:
                digest = store_bytes(raw, value["media_type"], d=d)
                return blob_file(digest, value.get("media_type"), d=d)
    except Exception:  # noqa: BLE001 — advisory path; the transcript must render
        logger.exception("blob materialize failed")
    return None
