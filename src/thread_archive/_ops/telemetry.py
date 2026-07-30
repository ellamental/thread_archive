"""Whether this install records how it ran.

The archive keeps several append-only ledgers of its own *runtime*: every served
web request with its latency, every retrieval call with its per-stage breakdown,
every ingest poll that did work, every load run. They are instruments for whoever
maintains thread-archive — the population a bench replays, the series a "when did
this get slow" question reads — and their consumers are the dev panels
(``devweb/``) and the search lab, not any surface someone came here to read
conversations through.

An install that is merely *run* has no use for them. Nothing in the product reads
these files to decide anything, and they are not free: rotation retains every
segment on purpose (:mod:`.ledger`), so the rows accumulate for as long as the
install lives; each row is an append on the path it describes; and the contention
sample behind them reads the machine's load average, this process's peak memory
and the index's WAL on every call. A retrieval row also carries the query text —
telemetry about a search someone ran, kept on disk beside the archive, for a
question nobody on that box is going to ask.

So recording is **off unless this install is being developed on**:
``"dev_mode": true`` in ``config.json`` (:func:`.._config.dev_mode`), the same
switch that decides whether preserved provider drift is a to-do worth warning
about today.

Each ledger keeps its own environment switch, and that switch outranks the config
in both directions. ``THREAD_ARCHIVE_WEB_METRICS=0`` silences one ledger on a dev
install; ``THREAD_ARCHIVE_INGEST_LOG=1`` turns one on for an operator who has been
asked for a trace of a slow install, without making them a developer or touching
their config. Unset — the normal state — is what defers to ``dev_mode``.

**Fault records are not runtime telemetry and are not gated here.** Ingest errors,
capture skips, validation drift, verify failures and the repair patch log all say
that conversations may not have been preserved, which is the thing an archive owes
its operator whoever they are. They record on every install. So does
``load-state.json``: it is live progress for a load in flight rather than history,
and it is how anyone watching a multi-hour import can tell it from a hung one.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .._config import dev_mode, load_config

#: Values that read as "off" in any of the per-ledger environment switches. An
#: unrecognized value is *on*, which is the behavior these switches have always
#: had: they were written as kill switches, and a typo in one must not silently
#: disable the recording someone set it to guarantee.
OFF_VALUES = ("0", "false", "no", "off")


def recording(env_var: str, home: Optional[Any] = None) -> bool:
    """Whether the ledger whose switch is ``env_var`` should record here.

    Read per call rather than resolved once at import. The processes that write
    these ledgers — the watcher, the viewer, an MCP server — run for weeks, and a
    cached answer would mean turning recording on requires restarting all of them
    before the slowness being investigated can be measured. The cost is a small
    JSON file read against an append the caller was about to make anyway.
    """
    raw = os.environ.get(env_var)
    if raw is not None and raw.strip():
        return raw.strip().lower() not in OFF_VALUES
    return dev_mode(load_config(home))
