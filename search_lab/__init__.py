"""The search lab: quality, calibration, and latency harnesses over a real archive.

Not part of the installed product. The dependency runs lab → package, so the
bench is free to reach into package privates. The one edge back is
``thread_archive._dev``, which imports :mod:`retrieval_report` to serve the
``/retrieval`` dev page — and ``_dev`` is itself excluded from the wheel and
imported fail-softly, so an install still has no path to the bench.

Two invocation styles, both supported, which is what the ``sys.path`` setup below
is for:

* **a script** — ``.venv/bin/python search_lab/retrieval_eval.py`` (how the
  harnesses are documented and run). The interpreter puts this directory on
  ``sys.path`` itself, so a harness reaches its siblings by bare import
  (``from eval_core import evaluate``).
* **a module** — ``import search_lab.eval_core``, which is how the tests reach
  the shared cores (:mod:`eval_core`, :mod:`snapshot`, :mod:`run_meta`).
  Importing the package runs this file, which puts the same two directories on
  the path, so the bare sibling imports inside the harnesses resolve here too.

A module loaded both ways lands in ``sys.modules`` twice under two names. The
cores are stateless scoring and ledger code, so that costs a second import and
nothing else; the harnesses that load a sibling by path guard with
``sys.modules.setdefault`` where the identity matters.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# The lab dir (sibling imports) and the package source (an editable install
# already resolves `thread_archive`; this covers a bare checkout).
for _p in (str(_HERE), str(_HERE.parent / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
