"""One-shot truth migration: integer thread ids → ULIDs (truth format v1 → v2).

Rewrites the whole JSONL truth so every thread id is a ULID whose timestamp is
the thread's real start (first event's ``occurred_at``, else the thread row's
``inserted_at``), with the old integer preserved as ``legacy_id`` on the thread
record. Everything that references a thread id is mapped: event records,
``thread_links.jsonl`` / ``topic_messages.jsonl`` snapshots, ``kg_events.jsonl``
(column, entity_id, and payload fields), ``import_state.jsonl``,
``amendments.jsonl``, ``redactions.jsonl``, and ``source_metadata.branched_from``.

Safety model — nothing is destroyed until the operator says so:

1. The new thread tree is built at ``truth/threads.new`` and the overlays as
   ``<name>.jsonl.new`` siblings; the old files are untouched while building.
2. The swap moves the old tree + overlays to ``<home>/pre-ulid-backup/`` and
   renames the new ones into place, then writes a v2 manifest (the content-hash
   baseline is dropped — every line changed).
3. The SQLite index is deleted and rebuilt with ``archive reindex``; rollback is
   moving the backup back and restoring the saved v1 manifest.

Run with the watcher stopped: ``launchctl bootout gui/$UID/com.thread-archive.watcher``
first (thread-monitor must also be told, or use the ops pause mechanism), then::

    .venv/bin/python -m thread_archive._scripts.migrate_thread_ulids [--home PATH] [--dry-run]

``--dry-run`` builds the new tree and reports counts without swapping.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .._store.ulid import mint_ulid
from .._truth.layout import (
    THREADS_SUBDIR,
    TRUTH_FORMAT_VERSION,
    ULID_MAPPING_FILE,
    _depth_for,
    _thread_relpath,
    update_manifest,
)

# Overlay files whose records carry thread-id fields, with the fields to map.
_OVERLAYS: dict[str, tuple[str, ...]] = {
    "thread_links.jsonl": ("source_thread_id", "target_thread_id", "created_by_thread_id"),
    "topic_messages.jsonl": ("topic_id", "thread_id", "created_by_thread_id"),
    "import_state.jsonl": ("thread_id",),
    "amendments.jsonl": ("thread_id",),
    "redactions.jsonl": ("thread_id",),
}

# kg_events payload keys that carry thread/topic ids (event ids stay integers).
_KG_PAYLOAD_KEYS = (
    "thread_id", "topic_id", "from_id", "into_id",
    "source_thread_id", "target_thread_id",
)


def _parse_ts_ms(value: object) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def build_mapping(index_db: Path) -> dict[int, str]:
    """legacy integer id → freshly minted ULID, timestamped at thread start."""
    conn = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
    try:
        starts: dict[int, int | None] = {}
        for tid, inserted in conn.execute("SELECT id, inserted_at FROM threads"):
            starts[int(tid)] = _parse_ts_ms(inserted)
        for tid, first in conn.execute(
            "SELECT thread_id, MIN(occurred_at) FROM events GROUP BY thread_id"
        ):
            ms = _parse_ts_ms(first)
            if ms is not None:
                starts[int(tid)] = ms
    finally:
        conn.close()
    mapping: dict[int, str] = {}
    seen: set[str] = set()
    for tid, ms in starts.items():
        ulid = mint_ulid(ms)
        while ulid in seen:  # pragma: no cover — 80-bit collision
            ulid = mint_ulid(ms)
        seen.add(ulid)
        mapping[tid] = ulid
    return mapping


class Migrator:
    def __init__(self, home: Path, mapping: dict[int, str]):
        self.home = home
        self.truth = home / "truth"
        self.mapping = mapping
        self.minted_unindexed = 0  # ids seen only in truth, never in the index

    def map_id(self, value: object) -> object:
        """Map an integer (or digit-string) thread id; leave anything else."""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            tid = int(value)
            if tid not in self.mapping:
                # Truth knows a thread the index doesn't (e.g. discarded before
                # commit but its file survived a crash). Mint so no ref dangles.
                self.mapping[tid] = mint_ulid()
                self.minted_unindexed += 1
            return self.mapping[tid]
        return value

    # ── per-thread files ────────────────────────────────────────────────────
    def rewrite_threads(self, new_threads: Path, depth: int) -> tuple[int, int]:
        n_threads = n_events = 0
        old_threads = self.truth / THREADS_SUBDIR
        for path in sorted(old_threads.rglob("*.jsonl")):
            try:
                legacy = int(path.stem)
            except ValueError:
                print(f"  ! skipping stray file {path}", file=sys.stderr)
                continue
            ulid = self.map_id(legacy)
            out_lines: list[str] = []
            saw_thread_record = False
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # torn line; repair's job, don't carry it
                    kind = rec.get("type", "event")
                    if kind == "thread":
                        rec["id"] = self.map_id(rec.get("id", legacy))
                        rec["legacy_id"] = legacy
                        meta = rec.get("source_metadata")
                        if isinstance(meta, dict) and meta.get("branched_from") is not None:
                            meta["branched_from"] = self.map_id(meta["branched_from"])
                        saw_thread_record = True
                    else:
                        rec["thread_id"] = self.map_id(rec.get("thread_id", legacy))
                        n_events += 1
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
            if not saw_thread_record:
                # Synthesize metadata so the legacy alias survives reindex.
                stub = {"type": "thread", "id": ulid, "legacy_id": legacy,
                        "name": f"thread:{legacy}"}
                out_lines.insert(0, json.dumps(stub, ensure_ascii=False))
            rel = _thread_relpath(str(ulid), depth).relative_to(THREADS_SUBDIR)
            dest = new_threads / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Append: a shard-depth twin of the same thread merges rather than
            # clobbers (reindex collapses duplicate event ids; last metadata wins).
            with open(dest, "a", encoding="utf-8") as out:
                out.write("\n".join(out_lines) + "\n")
            n_threads += 1
        return n_threads, n_events

    # ── overlays ────────────────────────────────────────────────────────────
    def rewrite_overlay(self, name: str, fields: tuple[str, ...]) -> int:
        src = self.truth / name
        if not src.exists():
            return 0
        n = 0
        with open(src, encoding="utf-8") as fh, \
                open(src.with_suffix(".jsonl.new"), "w", encoding="utf-8") as out:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for f in fields:
                    if rec.get(f) is not None:
                        rec[f] = self.map_id(rec[f])
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        return n

    def rewrite_kg_events(self) -> int:
        src = self.truth / "kg_events.jsonl"
        if not src.exists():
            return 0
        n = 0
        with open(src, encoding="utf-8") as fh, \
                open(src.with_suffix(".jsonl.new"), "w", encoding="utf-8") as out:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("actor_thread_id") is not None:
                    rec["actor_thread_id"] = self.map_id(rec["actor_thread_id"])
                ent_type, ent = rec.get("entity_type"), rec.get("entity_id")
                if ent is not None:
                    if ent_type == "topic":
                        rec["entity_id"] = str(self.map_id(ent))
                    elif ent_type == "link":
                        s, t, *rest = str(ent).split(":")
                        rec["entity_id"] = ":".join(
                            [str(self.map_id(s)), str(self.map_id(t)), *rest])
                    elif ent_type == "topic_message":
                        topic, _, ev = str(ent).partition(":")
                        rec["entity_id"] = f"{self.map_id(topic)}:{ev}"
                payload = rec.get("payload")
                if isinstance(payload, dict):
                    for k in _KG_PAYLOAD_KEYS:
                        if payload.get(k) is not None:
                            payload[k] = self.map_id(payload[k])
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=os.environ.get(
        "THREAD_ARCHIVE_HOME", str(Path.home() / ".thread" / "archive")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    home = Path(args.home)
    truth = home / "truth"
    manifest = json.loads((truth / "manifest.json").read_text())
    if int(manifest.get("version", 1)) >= 2:
        print("already at truth format v2 — nothing to do")
        return 0
    index_db = home / "index.db"

    print("building id mapping from the index …")
    mapping = build_mapping(index_db)
    print(f"  {len(mapping)} threads mapped")

    m = Migrator(home, mapping)
    depth = _depth_for(len(mapping))
    new_threads = truth / "threads.new"
    if new_threads.exists():
        shutil.rmtree(new_threads)
    print(f"rewriting thread files (shard depth {depth}) …")
    nt, ne = m.rewrite_threads(new_threads, depth)
    print(f"  {nt} thread files, {ne} event records")
    for name, fields in _OVERLAYS.items():
        n = m.rewrite_overlay(name, fields)
        if n:
            print(f"  {name}: {n} records")
    nkg = m.rewrite_kg_events()
    print(f"  kg_events.jsonl: {nkg} records")
    if m.minted_unindexed:
        print(f"  ! minted {m.minted_unindexed} id(s) for threads unknown to the index")

    # Persist the mapping — the durable record of which legacy id became which
    # ULID, independent of the thread records themselves. The backup mirror's
    # renamed-twin detection reads it to converge a pre-migration backup.
    (home / ULID_MAPPING_FILE).write_text(json.dumps(
        {str(k): v for k, v in sorted(m.mapping.items())}, indent=0))

    if args.dry_run:
        print("dry run: leaving truth/threads.new and *.jsonl.new in place; no swap")
        return 0

    print("swapping …")
    backup = home / "pre-ulid-backup"
    backup.mkdir(exist_ok=True)
    (backup / "manifest.v1.json").write_text(json.dumps(manifest, indent=2))
    os.replace(truth / THREADS_SUBDIR, backup / THREADS_SUBDIR)
    os.replace(new_threads, truth / THREADS_SUBDIR)
    for name in (*_OVERLAYS, "kg_events.jsonl"):
        newf = (truth / name).with_suffix(".jsonl.new")
        if newf.exists():
            os.replace(truth / name, backup / name)
            os.replace(newf, truth / name)

    def _mut(mf: dict) -> None:
        mf["version"] = TRUTH_FORMAT_VERSION
        mf["shard_depth"] = depth
        mf.pop("hashes_baseline", None)  # every line changed; baseline is void

    update_manifest(truth, _mut)
    print(f"swap done; old truth in {backup}")
    print("next: archive reindex && archive verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
