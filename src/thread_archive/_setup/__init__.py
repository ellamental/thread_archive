"""Setup + status for humans: the ``thread_archive`` console script.

See :mod:`.wizard` for the flow and :mod:`.clients` for MCP client wiring.
Private machinery like everything else underscore-prefixed — the command's
*existence* is the product surface, its internals are not.
"""

from __future__ import annotations

from .wizard import main

__all__ = ["main"]
