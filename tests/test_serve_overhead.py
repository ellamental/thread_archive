"""The serving layer's own cost, which no other number here has ever included.

Every latency this product records is taken inside the tool. The MCP wrapper —
the throttled ingest kick, the dispatch around it — has always been outside all
of them, so "the search took 40 ms" and "the agent waited 40 ms" were assumed
equal rather than measured that way.

The slow-kick cases inject a hand-written gate through ``_served``'s ``throttle``
parameter: the real wrapper, the real ledger, a fake only at the seam the module
already exposes for it.
"""

from __future__ import annotations

import json
import time

from thread_archive import _tools
from thread_archive._mcp import server
from thread_archive._retrieval import usage


class Gate:
    """A catch-up gate that costs exactly what it is told to.

    Stands in for :class:`~thread_archive._mcp.server.IngestThrottle` at the
    wrapper's seam — the real thing decides whether to kick off a background
    ingest pass, which a latency test has no way to make deterministic."""

    def __init__(self, cost_s: float = 0.0) -> None:
        self.cost_s = cost_s
        self.kicks = 0

    def maybe_catch_up(self) -> None:
        self.kicks += 1
        if self.cost_s:
            time.sleep(self.cost_s)


def _rows(home):
    path = home / usage.LEDGER_FILE
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _serve_rows(home):
    return [r for r in _rows(home) if r.get("kind") == "serve"]


def test_a_tool_hands_its_own_measurement_outward(archive_home) -> None:
    from thread_archive._store import init_db

    init_db()
    with _tools.call_span() as span:
        _tools.thread_search("anything at all", limit=1)
    assert "tool_ms" in span, "the wrapper needs the tool's own number to subtract"
    assert span["tool_ms"] >= 0.0


def test_a_span_with_no_call_inside_it_stays_empty(archive_home) -> None:
    with _tools.call_span() as span:
        pass
    assert span == {}


def test_publishing_with_nobody_listening_is_a_no_op() -> None:
    _tools._publish_call_ms(12.0)  # must not raise


def test_a_fast_serve_writes_no_row(archive_home) -> None:
    """A row per search saying "the wrapper cost nothing" would be the largest
    thing in the ledger and would say nothing."""
    gate = Gate()

    def quick(**kwargs) -> str:
        _tools._publish_call_ms(0.0)
        return "ok"

    server._served(quick, gate)()
    assert gate.kicks == 1, "the wrapper still kicks; it just has nothing to report"
    assert _serve_rows(archive_home) == []


def test_real_overhead_is_recorded_against_the_tool_it_wrapped(archive_home) -> None:
    gate = Gate(cost_s=0.02)  # a kick that actually costs something

    def quick(**kwargs) -> str:
        _tools._publish_call_ms(0.0)  # the tool measured itself as ~free
        return "ok"

    quick.__name__ = "thread_search"
    server._served(quick, gate)()

    (row,) = _serve_rows(archive_home)
    assert row["tool"] == "thread_search"
    assert row["overhead_ms"] >= 15.0, "the kick's cost belongs to the serving layer"
    assert row["kick_ms"] >= 15.0
    assert row["served_ms"] >= row["tool_ms"]


def test_a_slow_failure_in_the_layer_is_recorded_as_one(archive_home) -> None:
    gate = Gate(cost_s=0.02)

    def boom(**kwargs) -> str:
        _tools._publish_call_ms(0.0)  # tools publish on the way out, raise or not
        raise RuntimeError("nope")

    boom.__name__ = "thread_read"
    try:
        server._served(boom, gate)()
    except RuntimeError:
        pass
    (row,) = _serve_rows(archive_home)
    assert row["failed"] is True
    assert row["overhead_ms"] >= 15.0


def test_a_fast_failure_is_as_uninteresting_as_a_fast_success(archive_home) -> None:
    """The tool's own row already records the failure and its duration; this
    ledger exists only to say the layer around it took time."""
    def boom(**kwargs) -> str:
        _tools._publish_call_ms(0.0)
        raise RuntimeError("nope")

    try:
        server._served(boom, Gate())()
    except RuntimeError:
        pass
    assert _serve_rows(archive_home) == []


def test_a_real_served_search_measures_itself_end_to_end(archive_home) -> None:
    """Through the actual wrapped tool the server exports — no fakes at all."""
    from thread_archive._store import init_db

    init_db()
    out = server.thread_search("nothing will match this", limit=1)
    assert isinstance(out, str)
    searches = [r for r in _rows(archive_home) if r.get("kind") == "search"]
    assert len(searches) == 1, "the tool's own row is written exactly once"


def test_telemetry_failure_never_breaks_a_served_call(archive_home, monkeypatch) -> None:
    # A home that cannot hold a ledger: the write fails for real, and the call
    # still has to be served.
    broken = archive_home / "not-a-directory"
    broken.write_text("", encoding="utf-8")
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(broken))

    def quick(**kwargs) -> str:
        _tools._publish_call_ms(0.0)
        return "still served"

    assert server._served(quick, Gate(cost_s=0.02))() == "still served"
