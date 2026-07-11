"""Drop-folder watcher: auto-import account exports left in ``<home>/dumps/``.

Unlike the per-provider watchers that tail a tool's *live* store, this one watches a
human-driven **drop zone**. You download a claude.ai or xAI (Grok) account export — a
ZIP (or unzipped batch directory) of *all* your conversations — drop it into
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
  idempotent per conversation (a re-run skips threads already present), durable in the
  JSONL truth before each commit.
- **Clear on success.** A clean import *deletes* the dropped file/dir — the data now lives
  in the truth log (which is itself the backup), so the download is redundant.
- **Quarantine on failure.** An unrecognized shape, or an import that raises, is moved to
  ``dumps/failed/`` — never deleted, never hot-retried — so the failure is visible.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterator, Optional

from .._importers.exports import (
    classify_export,
    import_claude_ai_export,
    import_xai_export,
)
from .base import SourceWatcher, WatchResult

logger = logging.getLogger(__name__)

# Reserved subdir for exports that don't cleanly import; skipped when scanning.
QUARANTINE_DIRNAME = "failed"


class ExportDropWatcher(SourceWatcher):
    """Imports claude.ai / xAI account exports dropped into ``<home>/dumps/``.

    Detection + dedup + import + cleanup, all in-process: a settled export is imported
    via the local bulk importer, then deleted on success or quarantined on failure.
    """

    def __init__(self, dumps_dir: Optional[Path] = None) -> None:
        if dumps_dir is None:
            from .._config import resolve_paths

            dumps_dir = resolve_paths().dumps_dir
        self.dumps_dir = Path(dumps_dir)
        self.quarantine_dir = self.dumps_dir / QUARANTINE_DIRNAME
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

        Skips dotfiles and the ``failed/`` quarantine subdir (so quarantined exports are
        never rescanned)."""
        if not self.dumps_dir.exists():
            return
        for entry in sorted(self.dumps_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.name == QUARANTINE_DIRNAME:
                    continue
                yield entry
            elif entry.is_file() and entry.suffix.lower() == ".zip":
                yield entry

    def _process(self, path: Path) -> WatchResult:
        name = path.name
        kind = classify_export(path)
        if kind is None:
            logger.warning(
                "export-drop: %s is not a recognized claude.ai / xAI export — quarantining",
                name,
            )
            self._quarantine(path)
            return WatchResult(
                sources_checked=1, errors=[f"export-drop: unrecognized export {name}"]
            )

        logger.info("export-drop: importing %s export %s", kind, name)
        try:
            res = (
                import_xai_export(path) if kind == "xai" else import_claude_ai_export(path)
            )
        except Exception as e:  # noqa: BLE001 — one bad export must not stop the loop
            logger.error(
                "export-drop: import failed for %s: %s — quarantining", name, e
            )
            self._quarantine(path)
            return WatchResult(
                sources_checked=1, errors=[f"export-drop: import error for {name}: {e}"]
            )

        logger.info(
            "export-drop: imported %s — processed=%d imported=%d skipped=%d events=%d; removing",
            name, res.processed, res.imported, res.skipped, res.events_created,
        )
        self._remove(path)
        return WatchResult(
            sources_checked=1,
            items_imported=res.imported,
            events_created=res.events_created,
        )

    def _remove(self, path: Path) -> None:
        """Delete an imported export. The truth log holds the data, so this is safe."""
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:  # pragma: no cover
            logger.warning(
                "export-drop: could not remove %s after import: %s", path.name, e
            )

    def _quarantine(self, path: Path) -> None:
        """Move an export that didn't import into ``dumps/failed/`` (never delete)."""
        try:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            dest = self.quarantine_dir / path.name
            n = 1
            while dest.exists():
                dest = self.quarantine_dir / f"{path.name}.{n}"
                n += 1
            shutil.move(str(path), str(dest))
        except OSError as e:  # pragma: no cover
            logger.warning("export-drop: could not quarantine %s: %s", path.name, e)


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
