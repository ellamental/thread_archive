"""`python -m thread_archive` — the same front door as the `thread_archive`
console script, for any environment where invoking the module by interpreter is
more robust than resolving a script on PATH.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
