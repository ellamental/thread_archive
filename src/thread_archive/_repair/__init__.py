"""``thread-archive source fix`` — user-run repair of a drifted provider import.

The archive's parsers rot on the provider's schedule, not the maintainer's: a
harness update changes its transcript format and the importer soft-degrades
(ledgers, coverage, the search-result notice) until someone fixes it. The user
whose machine has the drift also has the samples, a harness with edit rights,
and the motivation — so the fix runs *there*: this module scaffolds an
override-plugin patch with the repair protocol beside it (:mod:`.scaffold`),
the user (or whatever agent they point at the scaffold) writes the parse logic,
and the result is gated through deterministic activation (:mod:`.activate`) —
tests green, then enabled, then the ledger-driven re-import recovers everything
the broken parser consumed (:mod:`.reimport`).

Nothing here runs an agent. Archive lays out the work and holds the gate;
whoever does the fix is the user's choice, and no claim about the fix enables a
patch — only the scaffold's tests passing in a fresh subprocess does.

The re-import stands on its own as ``thread-archive source recheck`` and is the
first move for any degradation verdict, patch or no patch. A parse fix reaches a
machine by core release at least as often as by local patch, and the operator who
upgrades into one otherwise has no way to retire a verdict their upgrade already
fixed — the ledger records keep it standing for the rest of the rolling window,
naming a repair that is already done. Re-reading is also the only honest test of
a fix: the records close if the findings don't come back, and not otherwise.

Patches are temporary by default (retired by the next self-update —
:mod:`.retire`) and pinnable for "I always want mine". Every lifecycle
transition lands in ``patch-log.jsonl`` (:mod:`.ledger`).
"""

from __future__ import annotations

import logging

from .activate import ActivationError, activate, set_pinned  # noqa: F401 — package API
from .reimport import reimport_source  # noqa: F401 — package API
from .retire import retire_patches  # noqa: F401 — package API
from .scaffold import plugin_dir, scaffold  # noqa: F401 — package API

logger = logging.getLogger(__name__)
