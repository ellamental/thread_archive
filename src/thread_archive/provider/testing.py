"""Test support for provider plugins — an isolated archive and a golden harness.

A provider is a format contract with something you don't control, and the way
that contract breaks is quiet: a renamed key, a block that stops being emitted, a
timestamp that shifts. None of it raises. What catches it is pinning the *whole*
normalized output and reading the diff when it moves.

That is what :func:`assert_golden` does. Import a fixture transcript into a
throwaway archive, and the truth JSONL it produces — the canonical normalized
form, exactly what would be stored for real — is compared field-for-field
against a reviewed file you commit. Any change to what your importer emits shows
up as a reviewable diff instead of passing silently.

Volatile fields are normalized before comparison so a golden is stable across
runs: per-run uuids (stream/api-call ids) become ordinals, wall-clock fields
(row id, ``recorded_at``) are dropped, and the tmp path is scrubbed to a
placeholder. What a golden locks is *your normalization*, not the provider's
format — drift on their side still needs real captures.

## Using it

This module ships a pytest plugin. Enable it in your ``conftest.py``::

    pytest_plugins = ["thread_archive.provider.testing"]

which gives you an ``archive_home`` fixture — a tmp archive, fully isolated from
the real one. Then::

    def test_myharness_golden(archive_home):
        init_archive()
        path = archive_home / "session.jsonl"
        path.write_text(MY_FIXTURE)
        import_myharness_session(path, "s1")
        assert_golden("myharness", archive_home, GOLDEN_DIR)

Generate the golden the first time (and after any deliberate importer change)
with ``UPDATE_GOLDENS=1 pytest``, then **read the diff before committing it** —
a regenerated golden that nobody looked at locks in whatever regression prompted
the regeneration.

Make the fixture carry your format's interesting shapes, not a happy path: tool
calls with their results, thinking/reasoning, a block type you don't model, an
unknown field, a torn final line. Those are where preservation actually gets
decided.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import pytest
except ImportError as e:  # pragma: no cover — the extra isn't installed
    # Test support, so pytest is an extra rather than a runtime dependency of the
    # archive itself: importing thread_archive must never require a test runner.
    raise ImportError(
        "thread_archive.provider.testing requires pytest — install the 'testing' "
        "extra (pip install 'thread-archive[testing]')"
    ) from e

from .._config import ENV_HOME, resolve_paths
from .._store import init_db

#: Thread-record fields a golden pins. Identity and provenance — the things a
#: format change can silently alter — not the mutable bookkeeping around them.
THREAD_KEYS = ("name", "title", "source", "source_id", "source_metadata", "description")


@pytest.fixture
def archive_home(tmp_path, monkeypatch) -> Iterator[Path]:
    """A tmp archive home, pointed at by ``THREAD_ARCHIVE_HOME``.

    Isolated from the real archive: an import in a test can never reach the
    machine's actual conversation store.
    """
    from .._store import _base
    from .._truth import jsonl_log

    home = tmp_path / "archive"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(ENV_HOME, str(home))
    monkeypatch.delenv("THREAD_ARCHIVE_TRUTH_DIR", raising=False)
    monkeypatch.delenv("THREAD_ARCHIVE_INDEX", raising=False)
    _base.close_engine()
    jsonl_log.reset_handles()
    yield home
    jsonl_log.reset_handles()
    _base.close_engine()


def init_archive() -> None:
    """Create the archive schema in the current home. Call before importing."""
    init_db()


def _scrub(value: Any, tmp: str) -> Any:
    if isinstance(value, str):
        return value.replace(tmp, "<fixtures>")
    if isinstance(value, list):
        return [_scrub(v, tmp) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, tmp) for k, v in value.items()}
    return value


def normalized_truth(home) -> list[dict]:
    """Every truth record in thread-file order, volatile fields normalized.

    Thread files are ULID-named, so plain lexicographic sort is mint order.

    The comparable form of what an import actually wrote. Stream and api-call
    uuids are replaced by ordinals in first-seen order — they are freshly minted
    each run, so comparing them raw would fail every time while still hiding a
    real change in how turns *group*, which the ordinals preserve exactly.
    """
    threads_dir = resolve_paths(None).truth_dir / "threads"
    streams: dict[str, str] = {}
    calls: dict[str, str] = {}
    out: list[dict] = []
    if not threads_dir.exists():
        return out
    for f in sorted(threads_dir.rglob("*.jsonl")):
        for raw in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(raw)
            if rec.get("type") == "thread":
                row: dict = {"type": "thread", **{k: rec.get(k) for k in THREAD_KEYS}}
            elif rec.get("type") == "event":
                sid, cid = rec.get("stream_id"), rec.get("api_call_id")
                row = {
                    "type": "event",
                    "event_type": rec["event_type"],
                    "occurred_at": rec.get("occurred_at"),
                    "stream": streams.setdefault(sid, f"s{len(streams)}") if sid else None,
                    "api_call": calls.setdefault(cid, f"c{len(calls)}") if cid else None,
                    "dedup_key": rec.get("dedup_key"),
                    "payload": rec.get("payload"),
                }
            else:
                row = rec
            out.append(_scrub(row, str(home)))
    return out


def assert_golden(name: str, home, golden_dir, *, update_env: str = "UPDATE_GOLDENS") -> None:
    """Compare this import's normalized truth against the reviewed golden ``name``.

    With ``$UPDATE_GOLDENS`` set the golden is rewritten and the test **skips**
    rather than passing — a regeneration is not evidence of anything until
    someone reads the diff, and a pass would read as one.
    """
    got = normalized_truth(home)
    golden_file = Path(golden_dir) / f"{name}.json"
    if os.environ.get(update_env):
        golden_file.parent.mkdir(parents=True, exist_ok=True)
        golden_file.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"golden regenerated: {golden_file.name} — review the diff")
    assert golden_file.exists(), (
        f"no golden for {name}; generate it with {update_env}=1 and review it before committing"
    )
    want = json.loads(golden_file.read_text(encoding="utf-8"))
    assert got == want, (
        f"{name}: normalized output diverged from the reviewed golden. If the importer "
        f"change is deliberate, regenerate with {update_env}=1 and review the diff."
    )


def write_jsonl(path, lines: list, *, torn_tail: Optional[str] = None) -> None:
    """Write a JSONL fixture, optionally ending mid-line.

    ``torn_tail`` appends unterminated bytes with no trailing newline — a write
    cut off by a crash. Worth having in a fixture: a torn final line is the
    normal state of any transcript being written right now, so an importer that
    can't skip one and keep the complete lines fails constantly in practice.
    """
    text = "\n".join(json.dumps(ln) for ln in lines) + "\n"
    if torn_tail is not None:
        text += torn_tail
    Path(path).write_text(text, encoding="utf-8")


__all__ = [
    "archive_home",
    "init_archive",
    "assert_golden",
    "normalized_truth",
    "write_jsonl",
    "THREAD_KEYS",
]
