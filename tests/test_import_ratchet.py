"""The single-path ratchet.

thread-archive has exactly one storage/runtime path: JSONL + SQLite. This test is
the spine that keeps it that way as code lands. It statically scans every module
under ``src/`` (parsing imports with ``ast``, so even lazy/in-function imports are
caught) and fails if any of them reaches for a server backend or a Postgres
dialect.

If this fails, a module reached for a server backend — strip the server arm and
keep the one storage path (JSONL + SQLite).
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

# Top-level modules that must never appear in our source tree.
BANNED_TOP_LEVEL = frozenset(
    {
        "ops",
        "psycopg",
        "psycopg2",
        "asyncpg",
        "pgvector",
        "neo4j",
        "meilisearch",
        "typesense",
        "manticoresearch",
        "alembic",
        # MCP is built on the library directly, not a FastAPI route surface.
        "fastapi",
        "starlette",
        "uvicorn",
    }
)

# Banned dotted prefixes (a specific submodule of an otherwise-allowed package).
BANNED_PREFIXES = ("sqlalchemy.dialects.postgresql",)


def _iter_source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) are in-package; ignore.
            if node.level == 0 and node.module:
                mods.add(node.module)
    return mods


def _violations(module_name: str) -> list[str]:
    top = module_name.split(".", 1)[0]
    bad: list[str] = []
    if top in BANNED_TOP_LEVEL:
        bad.append(module_name)
    for prefix in BANNED_PREFIXES:
        if module_name == prefix or module_name.startswith(prefix + "."):
            bad.append(module_name)
    return bad


@pytest.mark.parametrize("path", _iter_source_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_banned_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offending = sorted({m for mod in _imported_modules(tree) for m in _violations(mod)})
    assert not offending, (
        f"{path.relative_to(SRC)} imports server/postgres modules forbidden in the "
        f"serverless archive: {offending}"
    )


def test_fresh_import_pulls_no_banned_modules() -> None:
    """A fresh `import thread_archive` (in an isolated subprocess, so other tests'
    imports of the external `mcp` SDK can't pollute the check) must not pull in a
    stray top-level `archive` package or any banned server backend.
    """
    import subprocess

    banned = sorted(BANNED_TOP_LEVEL | {"archive"})
    code = (
        "import sys, thread_archive\n"
        f"banned = {banned!r}\n"
        "print(','.join(sorted(set(banned) & set(sys.modules))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    leaked = [m for m in out.stdout.strip().split(",") if m]
    assert not leaked, f"importing thread_archive loaded banned modules: {leaked}"
