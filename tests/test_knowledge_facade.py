"""The librarian seam — what the archive reports with and without thread-librarian.

The archive keeps only the knowledge layer's data plane; everything analytic
(curation stats, graph metadata, the subjects lens) belongs to the optional
``thread_librarian`` package and must degrade cleanly when it is absent. Both
sides are pinned here: the delegation half runs in-process against the dev
venv's plugin (``importorskip`` guards it), and the degradation half runs in a
real interpreter that genuinely does not have the package — see
:func:`_without_plugin`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import textwrap
from pathlib import Path

import pytest

from thread_archive import _api as ta

SRC = str(Path(__file__).resolve().parent.parent / "src")

# Every child starts by proving its own premise: if thread_librarian turns out
# to be importable, the run is not the box we meant to test and must not be
# allowed to pass for the wrong reason.
_PRELUDE = """
import json, sys
try:
    import thread_librarian
except ImportError:
    pass
else:
    raise SystemExit("thread_librarian is importable: not a plugin-free interpreter")
"""


def _without_plugin(home, body: str):
    """Run ``body`` in an interpreter that genuinely lacks ``thread_librarian``
    and return the JSON it printed.

    ``-S`` skips site processing, so the editable install's ``.pth`` never runs
    and the package cannot be found, while the rest of site-packages
    (sqlalchemy and friends) still imports off ``PYTHONPATH`` — a machine where
    the optional plugin was never installed, not a faked import failure.
    """
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([sysconfig.get_paths()["purelib"], SRC]),
           "THREAD_ARCHIVE_HOME": str(home),
           "THREAD_ARCHIVE_EMBED": "off",
           "THREAD_ARCHIVE_RERANK": "off"}
    proc = subprocess.run([sys.executable, "-S", "-c", _PRELUDE + textwrap.dedent(body)],
                          capture_output=True, text=True, timeout=180, env=env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.integration
def test_api_curation_stats_degrades(archive_home) -> None:
    out = _without_plugin(archive_home, """
        from thread_archive import _api as ta
        print(json.dumps(ta.curation_stats()))
    """)
    assert out["available"] is False and "thread-librarian" in out["error"]


def test_api_curation_stats_delegates(archive_home) -> None:
    pytest.importorskip("thread_librarian")
    from thread_archive._store import init_db

    init_db()
    out = ta.curation_stats(days=1)
    # The plugin's stats shape: drains + coverage over the (empty) store.
    assert "drains" in out and "coverage" in out


@pytest.mark.integration
def test_topic_read_degrades_without_plugin(archive_home) -> None:
    """The compatibility topic reader works plugin-free: existing KG records
    still read, just without graph metadata or community peers."""
    tid = "01T0PIC0000000000000000001"
    detail = _without_plugin(archive_home, f"""
        from thread_archive._knowledge import topic_get
        from thread_archive._store import Thread, get_session, init_db

        init_db()
        with get_session() as s:
            s.add(Thread(id="{tid}", name="t", title="A Topic", thread_type="topic"))
            s.commit()
        print(json.dumps(topic_get("{tid}"), default=str))
    """)
    assert detail["title"] == "A Topic"
    assert detail["graph"] is None and detail["peers"] == []


@pytest.mark.integration
def test_search_render_degrades_without_plugin(archive_home) -> None:
    """The subjects seam is a strict no-op when the lens's package is absent."""
    assert _without_plugin(archive_home, """
        from thread_archive._retrieval.format import subjects_line
        print(json.dumps(subjects_line([{"thread_id": "x", "event_id": 1}])))
    """) is None
