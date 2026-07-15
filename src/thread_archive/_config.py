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
      config.json         # operator choices (source opt-outs, setup state); absent = all defaults

`home` resolves from ``THREAD_ARCHIVE_HOME`` (env), else ``~/.thread/archive``
(the family's ``~/.thread/<product>/`` namespace; ``~/.thread_archive``
survives as a compat symlink on some boxes).
Truth and index paths can be overridden individually (e.g. for tests).

``config.json`` is the durable form of the choices a user makes in the
``thread_archive`` setup flow — which sources to ingest, what setup decided —
and every ingest path (the watcher daemon, lazy MCP catch-up, ``archive
watch``) consults it via :func:`source_enabled`. A missing or unreadable file
means "all defaults": every source enabled, exactly the pre-config behavior.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_HOME = "THREAD_ARCHIVE_HOME"
ENV_TRUTH = "THREAD_ARCHIVE_TRUTH_DIR"
ENV_INDEX = "THREAD_ARCHIVE_INDEX"

# Resolved per call, never frozen into a constant: the store location is
# configuration (THREAD_ARCHIVE_HOME, a test's sandbox), and a constant captures
# whatever $HOME said at import and then ignores it.
def default_home() -> Path:
    return Path.home() / ".thread" / "archive"


@dataclass(frozen=True)
class ArchivePaths:
    """Resolved on-disk locations for one archive instance."""

    home: Path
    truth_dir: Path
    index_path: Path

    def ensure(self) -> "ArchivePaths":
        """Create the home + truth directories if absent; keep both private (0700).

        The archive is full conversation content — group/other must not be able
        to traverse into it, whatever mode individual files carry. Re-asserted on
        every open so a loosened or pre-existing home self-heals. Fail-soft on
        the chmod: opening an archive we can't own must not fail the open."""
        for d in (self.home, self.truth_dir):
            d.mkdir(parents=True, exist_ok=True)
            try:
                d.chmod(0o700)
            except OSError:
                pass
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
    base = Path(home) if home is not None else Path(os.environ.get(ENV_HOME) or default_home())
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


# ── config.json: durable operator choices ────────────────────────────────────

CONFIG_FILE = "config.json"


def config_path(home: str | os.PathLike[str] | None = None) -> Path:
    return resolve_paths(home).home / CONFIG_FILE


def load_config(home: str | os.PathLike[str] | None = None) -> dict:
    """The parsed config, or ``{}`` when absent/unreadable (all defaults).

    Fail-soft on purpose: a corrupt config file must degrade to default
    behavior (ingest everything), never take an ingest path down. But only a
    *missing* file is silent — a file that exists and won't parse flips every
    source opt-out (possibly a privacy choice) back to enabled, so it logs at
    error on every load until someone fixes or removes it.
    """
    path = config_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.error(
            "config: %s exists but could not be read/parsed — ALL defaults apply "
            "(every source enabled, opt-outs ignored)",
            path,
            exc_info=True,
        )
        return {}
    if not isinstance(data, dict):
        logger.error(
            "config: %s does not hold a JSON object — ALL defaults apply "
            "(every source enabled, opt-outs ignored)",
            path,
        )
        return {}
    return data


def save_config(cfg: dict, home: str | os.PathLike[str] | None = None) -> Path:
    """Write the config atomically and durably (tmp + fsync + rename). Returns the
    path. The fsync matters here like everywhere else in the archive: a source
    opt-out that vanishes in a power loss silently re-enables ingest."""
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    return path


def source_enabled(cfg: dict, source_name: str) -> bool:
    """Whether a source watcher may ingest. Unlisted sources default to enabled."""
    entry = cfg.get("sources", {}).get(source_name, {})
    if not isinstance(entry, dict):
        return True
    return bool(entry.get("enabled", True))
