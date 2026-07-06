"""Filesystem layout for a thread-archive instance.

A single archive lives under one *home* directory:

    <home>/
      truth/              # the durable JSONL truth (the backup)
        manifest.json     # shard depth + checkpoint watermark
        threads/          # one file per thread: <id>.jsonl (metadata record + events)
        kg_events.jsonl   # append-only curatorial log (topics / links / citations)
        thread_links.jsonl    # cross-thread overlay (topic-graph edges, folded from kg_events)
        topic_messages.jsonl  # cross-thread overlay (topic evidence, folded from kg_events)
      index.db            # SQLite projection, rebuildable from truth/ via `archive reindex`
      dumps/              # drop zone: account exports dropped here are auto-imported

`home` resolves from ``THREAD_ARCHIVE_HOME`` (env), else ``~/.thread_archive``.
Truth and index paths can be overridden individually (e.g. for tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "THREAD_ARCHIVE_HOME"
ENV_TRUTH = "THREAD_ARCHIVE_TRUTH_DIR"
ENV_INDEX = "THREAD_ARCHIVE_INDEX"

DEFAULT_HOME = Path.home() / ".thread_archive"


@dataclass(frozen=True)
class ArchivePaths:
    """Resolved on-disk locations for one archive instance."""

    home: Path
    truth_dir: Path
    index_path: Path

    def ensure(self) -> "ArchivePaths":
        """Create the home + truth directories if absent. Idempotent."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.truth_dir.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite:///{self.index_path}"

    @property
    def dumps_dir(self) -> Path:
        """Drop zone for downloaded account exports (auto-imported by the watcher)."""
        return self.home / "dumps"


def resolve_paths(
    home: str | os.PathLike[str] | None = None,
    *,
    truth_dir: str | os.PathLike[str] | None = None,
    index_path: str | os.PathLike[str] | None = None,
) -> ArchivePaths:
    """Resolve archive paths from explicit args, then env, then defaults."""
    base = Path(home) if home is not None else Path(os.environ.get(ENV_HOME, DEFAULT_HOME))
    base = base.expanduser()

    truth = (
        Path(truth_dir).expanduser()
        if truth_dir is not None
        else Path(os.environ.get(ENV_TRUTH, base / "truth")).expanduser()
    )
    index = (
        Path(index_path).expanduser()
        if index_path is not None
        else Path(os.environ.get(ENV_INDEX, base / "index.db")).expanduser()
    )
    return ArchivePaths(home=base, truth_dir=truth, index_path=index)
