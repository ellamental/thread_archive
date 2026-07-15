"""Drop-folder watcher: auto-import account exports left in ``<home>/dumps/``.

Unlike the per-provider watchers that tail a tool's *live* store, this one watches a
human-driven **drop zone**. You download a claude.ai, ChatGPT, or xAI (Grok) account
export — a ZIP (or unzipped batch directory) of *all* your conversations — drop it into
``~/.thread/archive/dumps/``, and the watcher imports it on the next poll and clears it.

Serverless by construction: it calls the in-process bulk importer
(:mod:`.._importers.exports`) directly. The monorepo ancestor POSTed the path to a backend
that ran the import server-side; here there is no backend — the import *is* local.

Lifecycle of one dropped export:

- **Settle.** A candidate (a ``.zip`` or an export directory) is acted on only once its
  ``(size, mtime)`` signal is unchanged across two consecutive polls, so a still-copying
  upload is never imported half-written.
- **Import.** A recognized export imports each conversation as its own thread — inline in
  the poll (a rare, human-initiated event, so blocking the loop briefly is acceptable),
  idempotent per conversation (a redrop merges: a conversation that grew since the last
  export gains exactly its new messages in the existing thread, an unchanged one imports
  nothing), durable in the JSONL truth before each commit.
- **Retain on clean success.** A drop is not deleted at the moment of import — the
  importer normalizes into the JSONL truth, but normalization is lossy in ways the
  importer can't always see (attachments/images/branch structure a parser doesn't yet
  carry), so the original download is the only place that content still exists. A fully
  clean import moves the drop into ``dumps/imported/<kind>/`` as the recovery copy. Only
  the **most recent export per kind** is kept: an account export is a full dump, so the
  next clean import of the same kind supersedes the last as the recovery source and the
  older one is pruned — the safety net stays, disk use stays bounded (no ever-growing
  pile from a weekly re-export).
- **Quarantine on failure or partial loss.** An unrecognized shape, an import that raises,
  an import that processed zero conversations (a recognized container whose contents
  didn't match the expected shape), or an import where **any** conversation errored (those
  are preserved as stub threads, but the export needs a look) is moved to ``dumps/failed/``
  — never deleted, never hot-retried — so the problem is visible and the drop is re-importable
  after a fix.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterator, Optional

from .._importers.exports import (
    classify_export,
    import_chatgpt_export,
    import_claude_ai_export,
    import_xai_export,
)
from .base import SourceWatcher, WatchResult

logger = logging.getLogger(__name__)

# Reserved subdirs, both skipped when scanning: exports that need attention go to
# ``failed/``; cleanly-imported exports are retained (never deleted) in ``imported/``.
QUARANTINE_DIRNAME = "failed"
IMPORTED_DIRNAME = "imported"


class ExportDropWatcher(SourceWatcher):
    """Imports claude.ai / ChatGPT / xAI account exports dropped into ``<home>/dumps/``.

    Detection + dedup + import + cleanup, all in-process: a settled export is imported
    via the local bulk importer, then deleted on success or quarantined on failure.
    """

    def __init__(self, dumps_dir: Optional[Path] = None) -> None:
        if dumps_dir is None:
            from .._config import resolve_paths

            dumps_dir = resolve_paths().dumps_dir
        self.dumps_dir = Path(dumps_dir)
        self.quarantine_dir = self.dumps_dir / QUARANTINE_DIRNAME
        self.imported_dir = self.dumps_dir / IMPORTED_DIRNAME
        # Create the drop zone so there's always an obvious place to drop exports.
        try:
            self.dumps_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:  # pragma: no cover — e.g. a read-only home
            logger.warning("export-drop: cannot create %s: %s", self.dumps_dir, e)
        # Previous poll's settle signal per candidate path — act only when unchanged.
        self._signals: dict[str, tuple] = {}

    @property
    def source_name(self) -> str:
        return "export-drop"

    def is_available(self) -> bool:
        return self.dumps_dir.exists()

    def poll(self) -> WatchResult:
        # Compute this poll's signals first, decide what's settled, *then* mutate —
        # so the delete/move during import can't perturb the scan we're iterating.
        current: dict[str, tuple] = {}
        settled: list[Path] = []
        for path in self._candidates():
            key = str(path)
            try:
                signal = _settle_signal(path)
            except OSError:
                continue  # vanished mid-scan; reconsidered next poll
            current[key] = signal
            if self._signals.get(key) == signal:
                settled.append(path)

        # Carry forward signals only for candidates we are *not* about to consume; a
        # settled one is about to be deleted/moved, so drop it (re-recorded if it
        # somehow remains, e.g. a quarantine that failed).
        consumed = {str(p) for p in settled}
        self._signals = {k: v for k, v in current.items() if k not in consumed}

        result = WatchResult()
        for path in settled:
            result = result + self._process(path)
        return result

    def _candidates(self) -> Iterator[Path]:
        """Top-level zips and export directories in the drop zone, sorted by name.

        Skips dotfiles and the reserved ``failed/`` and ``imported/`` subdirs (so
        quarantined and retained exports are never rescanned)."""
        if not self.dumps_dir.exists():
            return
        for entry in sorted(self.dumps_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.name in (QUARANTINE_DIRNAME, IMPORTED_DIRNAME):
                    continue
                yield entry
            elif entry.is_file() and entry.suffix.lower() == ".zip":
                yield entry

    def _process(self, path: Path) -> WatchResult:
        name = path.name
        kind = classify_export(path)
        if kind is None:
            logger.warning(
                "export-drop: %s is not a recognized claude.ai / ChatGPT / xAI export — quarantining",
                name,
            )
            self._quarantine(path)
            return WatchResult(
                sources_checked=1, errors=[f"export-drop: unrecognized export {name}"]
            )

        logger.info("export-drop: importing %s export %s", kind, name)
        try:
            # Global-name dispatch (not a dict captured at import time), so tests
            # can monkeypatch the importer functions on this module.
            #
            # force=True: an account export is a full dump, so a redrop must *merge*
            # conversations that grew since the last export. Without force the importer
            # skips every already-present conversation id outright — new messages in an
            # old conversation would never land. The event-level dedup makes force safe:
            # an unchanged conversation imports nothing.
            if kind == "xai":
                res = import_xai_export(path, force=True)
            elif kind == "chatgpt":
                res = import_chatgpt_export(path, force=True)
            else:
                res = import_claude_ai_export(path, force=True)
        except Exception as e:  # noqa: BLE001 — one bad export must not stop the loop
            logger.error(
                "export-drop: import failed for %s: %s — quarantining", name, e
            )
            self._quarantine(path)
            return WatchResult(
                sources_checked=1, errors=[f"export-drop: import error for {name}: {e}"]
            )

        if res.processed == 0:
            # The container was recognized but nothing inside matched the expected
            # conversation shape — a misclassification or a format change. Deleting
            # would destroy the user's download over an import of nothing; keep it
            # inspectable instead.
            logger.warning(
                "export-drop: %s classified as %s but contained no importable "
                "conversations — quarantining", name, kind,
            )
            self._quarantine(path)
            return WatchResult(
                sources_checked=1,
                errors=[f"export-drop: {name} ({kind}) had no importable conversations"],
            )

        if res.errored:
            # Some conversations raised and were preserved as stub threads (raw +
            # error) — the data isn't lost, but the export needs a look and a re-import
            # after the parser is fixed. Quarantine so it's visible, don't delete.
            logger.warning(
                "export-drop: imported %s with %d errored conversation(s) "
                "(processed=%d imported=%d skipped=%d events=%d) — quarantining for review",
                name, res.errored, res.processed, res.imported, res.skipped, res.events_created,
            )
            self._move_into(self.quarantine_dir, path)
            return WatchResult(
                sources_checked=1,
                items_imported=res.imported,
                events_created=res.events_created,
                errors=[
                    f"export-drop: {name} imported with {res.errored} errored "
                    f"conversation(s) preserved as stubs — quarantined for review"
                ],
            )

        logger.info(
            "export-drop: imported %s — processed=%d imported=%d skipped=%d events=%d; retaining",
            name, res.processed, res.imported, res.skipped, res.events_created,
        )
        self._retain(path, kind)
        return WatchResult(
            sources_checked=1,
            items_imported=res.imported,
            events_created=res.events_created,
        )

    def _retain(self, path: Path, kind: str) -> None:
        """Keep a cleanly-imported export as the recovery copy — bounded to one per kind.

        Normalization is lossy in ways the importer can't detect, so the source download
        is kept as the last line of defense. It's not deleted at import time; instead it
        moves into ``dumps/imported/<kind>/`` and *then* any older retained export of the
        same kind is pruned — a full account export supersedes the previous one, so a
        recurring re-export replaces rather than accumulates."""
        kind_dir = self.imported_dir / kind
        dest = self._move_into(kind_dir, path)
        if dest is None:
            return  # move failed; the drop stays put and is retried — nothing pruned
        for entry in kind_dir.iterdir():
            if entry != dest:
                self._prune(entry)

    def _quarantine(self, path: Path) -> None:
        """Move an export that needs attention into ``dumps/failed/`` (never delete)."""
        self._move_into(self.quarantine_dir, path)

    def _move_into(self, dest_dir: Path, path: Path) -> Optional[Path]:
        """Collision-safe move of a settled drop into a reserved subdir; the final
        destination path, or ``None`` if the move failed (the drop is left in place)."""
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / path.name
            n = 1
            while dest.exists():
                dest = dest_dir / f"{path.name}.{n}"
                n += 1
            shutil.move(str(path), str(dest))
            return dest
        except OSError as e:  # pragma: no cover
            logger.warning(
                "export-drop: could not move %s into %s: %s", path.name, dest_dir.name, e
            )
            return None

    def _prune(self, path: Path) -> None:
        """Delete a retained export that a newer one of the same kind has superseded.

        Only ever called on an already-retained copy in ``imported/`` — never on a drop
        at import time — so this can't be the thing that loses an unimported download."""
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:  # pragma: no cover
            logger.warning("export-drop: could not prune superseded export %s: %s", path.name, e)


def _settle_signal(path: Path) -> tuple:
    """A cheap signal that changes while a drop is still being written.

    A file → its own ``(size, mtime_ns)``. A directory (the batch-form export) → the
    aggregate ``(file count, total size, max mtime_ns)`` over its tree, so a directory
    still being copied in reads as changing until the copy finishes."""
    if path.is_dir():
        count = total = latest = 0
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            count += 1
            total += st.st_size
            latest = max(latest, st.st_mtime_ns)
        return (count, total, latest)
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)
