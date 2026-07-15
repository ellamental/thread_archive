"""Child process for the multiprocess durability suite — not a test module.

Imports one session into the archive at ``$THREAD_ARCHIVE_HOME``. In
``import-kill`` mode it SIGKILLs itself at a named truth-log seam, leaving
behind exactly the on-disk state a real crash (power loss, OOM kill) produces
at that point in the drain protocol:

- ``after_intent``: the intent frame is durable, no data line written yet.
- ``mid_append``: intent durable, a strict prefix of the batch's lines written
  (some possibly still in the handle's userspace buffer, lost with the kill).
- ``after_drain``: truth fully written + fsynced, intent cleared, but the
  process dies before the SQLite COMMIT — the JSONL ⊇ SQLite gap.

Invoked by tests/test_multiprocess_durability.py:
    python mp_child.py import <session_file> <source_id>
    python mp_child.py import-kill <session_file> <source_id> <seam>

``$MP_GO_FILE`` (optional) is a start barrier: the child spins until the file
exists, so the parent can line several children up and release them together.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _arm_seam(seam: str) -> None:
    from thread_archive._truth import drain

    if seam == "after_intent":
        real_intent = drain._write_intent

        def kill_after_intent(files):
            real_intent(files)
            _die()

        drain._write_intent = kill_after_intent
    elif seam == "mid_append":
        real_append = drain._append_line
        seen = {"n": 0}

        def kill_mid_append(path, rec):
            real_append(path, rec)
            seen["n"] += 1
            if seen["n"] >= 3:
                _die()

        drain._append_line = kill_mid_append
    elif seam == "after_drain":
        real_clear = drain._clear_intent

        def kill_after_drain():
            real_clear()
            _die()

        drain._clear_intent = kill_after_drain
    else:
        raise SystemExit(f"unknown seam: {seam}")


def main() -> None:
    mode, session_file, source_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]

    go_file = os.environ.get("MP_GO_FILE")
    if go_file:
        deadline = time.monotonic() + 30
        while not os.path.exists(go_file):
            if time.monotonic() > deadline:
                raise SystemExit("start barrier never released")
            time.sleep(0.01)

    if mode == "import-kill":
        _arm_seam(sys.argv[4])

    from thread_archive._importers import import_session_incremental

    r = import_session_incremental(session_file, source_id)
    print(json.dumps({"thread_id": r.thread_id, "events_created": r.events_created}))


if __name__ == "__main__":
    main()
