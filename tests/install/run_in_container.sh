#!/usr/bin/env bash
# Container entrypoint for the isolated install test: prove the installed package works
# end to end. Any failure exits non-zero (set -e), failing the `docker run`.
set -euo pipefail
cd /app

echo "=== environment ==="
python --version
thread_archive --version
echo

echo "=== full unit suite (isolated container) ==="
pytest tests/ -q
echo

echo "=== end-to-end install check (import all providers -> reindex -> search) ==="
python tests/install/e2e_check.py
echo

# The realistic first run: discover each provider's store in its real default
# location (~/.claude/projects, ~/.codex/sessions, the Linux ~/.config app-data
# dir for Cursor/Cowork) and ingest it through the installed CLI with no hand-fed
# paths — the clean-container Linux half of the cross-OS first-run proof (macOS is
# the `package` pytest lane). Always synthetic: it lays out its own fake $HOME.
echo "=== realistic first-run install check (watch --once discovery -> reindex -> search) ==="
python tests/install/first_run.py
echo

echo "ALL INSTALL TESTS PASSED"
