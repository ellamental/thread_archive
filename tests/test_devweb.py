"""The dev panels' server — the router, driven socket-free.

The same shape as tests/test_web.py: :func:`devweb.server.route` is pure
``(method, path, params) -> (status, content_type, body, headers)``, so these
exercise it directly against a seeded throwaway archive with no sockets and no
browser.

These live in the archive's suite rather than a suite of their own because what
they read is the archive's own ledgers, and the fixtures that seed those
(``archive_home`` and friends) are here. devweb ships in no wheel, so the
install lane skips them with every other viewer test.
"""

from __future__ import annotations

import json

import pytest

from thread_archive._viewer import viewer_available

pytestmark = pytest.mark.viewer

devweb_server = pytest.importorskip(
    "devweb.server", reason="the dev panels are dev-only (no wheel carries them)")


USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello devweb"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi from the assistant"}]}}


def _seed(archive_home):
    """One imported session, so the ledgers the panels read have a home to sit in."""
    from thread_archive import _api as ta

    f = archive_home / "sess.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in (USER, ASSISTANT)) + "\n",
                 encoding="utf-8")
    ta.import_path(f)


def _get(path: str, **params):
    """One GET through the router. Returns (status, content_type, payload)."""
    status, ctype, body, _headers = devweb_server.route(
        "GET", path, {k: [str(v)] for k, v in params.items()})
    if ctype == "application/json":
        return status, ctype, json.loads(body)
    return status, ctype, body


def test_the_probe_and_this_suite_agree() -> None:
    # If the viewer is here, so is devweb: they were split apart, not gated
    # separately, and a tree with one and not the other is a broken checkout.
    assert viewer_available() is True


def test_the_router_is_read_only(archive_home) -> None:
    """Every page here reads. A write surface would be a second thing to guard
    on a server whose whole point is that it carries less."""
    status, _, body = devweb_server.route("POST", "/api/telemetry", {})[:3]
    assert status == 405


def test_an_unknown_api_path_is_a_404_not_the_shell(archive_home) -> None:
    """A renamed endpoint must not answer 200 with HTML — the caller would parse
    the SPA shell as JSON and report something incoherent."""
    status, ctype, _ = _get("/api/not-a-real-endpoint")
    assert status == 404
    assert not ctype.startswith("text/html")


def test_a_non_loopback_bind_is_refused(archive_home) -> None:
    """The panels read this machine's ledgers with no auth, so exposing them
    past loopback must be deliberate rather than a typo'd --host."""
    with pytest.raises(ValueError, match="refusing non-loopback bind"):
        devweb_server.serve(host="0.0.0.0")


def test_telemetry_endpoint_assembles_web_ingest_faults_and_ledger_cost(archive_home):
    """The developer page reads the retained ledgers without moving their data
    into a second metrics store."""
    from datetime import datetime, timezone

    from thread_archive._ops import ingest_errors, ledger
    from thread_archive._watcher import ingest_log
    from thread_archive._web import metrics

    at = datetime.now(timezone.utc).isoformat()
    for row in (
        {"at": at, "path": "/api/status", "status": 200,
         "duration_ms": 10.0, "size": 100},
        {"at": at, "path": "/api/status", "status": 503,
         "duration_ms": 50.0, "size": 20, "concurrent": 2},
        {"at": at, "path": "/api/upload", "method": "POST", "status": 201,
         "duration_ms": 100.0, "size": 10},
    ):
        ledger.append(archive_home / metrics.LEDGER_FILE, row, max_bytes=1 << 20)
    ledger.append(
        archive_home / ingest_log.LEDGER_FILE,
        {
            "at": at,
            "kind": "ingest-pass",
            "source": "codex",
            "pass_ms": 25.0,
            "items": 1,
            "events": 8,
            "lines": 20,
            "bytes": 400,
            "parse_ms": 5.0,
            "write_ms": 10.0,
            "total_ms": 15.0,
        },
        max_bytes=1 << 20,
    )
    # The tally is process-global and only a signature's first sighting writes;
    # a sibling test in this worker may already have burned this one.
    ingest_errors.reset_tally()
    ingest_errors.record(["codex: could not parse /tmp/session-123.jsonl"], home=archive_home)

    status, ctype, payload = _get("/api/telemetry", hours=24)

    assert status == 200 and ctype == "application/json"
    assert payload["hours"] == 24
    assert payload["web"]["requests"] == 3
    assert payload["web"]["errors"] == 1
    assert payload["web"]["concurrent"] == 1
    assert payload["web"]["endpoints"][0]["path"] == "/api/upload"
    status_row = next(
        row for row in payload["web"]["endpoints"] if row["path"] == "/api/status"
    )
    assert status_row["n"] == 2 and status_row["errors"] == 1
    assert payload["ingest"]["sources"]["codex"]["events"] == 8
    assert payload["ingest"]["stages"]["write_ms"] == 10.0
    assert payload["faults"][0]["source"] == "codex"
    ledgers = {row["file"]: row for row in payload["ledgers"]}
    assert ledgers["web-requests.jsonl"]["bytes"] > 0
    assert ledgers["ingest-runs.jsonl"]["segments"] == 1


def test_telemetry_endpoint_honors_its_window(archive_home):
    from datetime import datetime, timezone

    from thread_archive._ops import ledger
    from thread_archive._web import metrics

    for at in ("2020-01-01T00:00:00+00:00", datetime.now(timezone.utc).isoformat()):
        ledger.append(
            archive_home / metrics.LEDGER_FILE,
            {"at": at, "path": "/api/status", "status": 200,
             "duration_ms": 10.0, "size": 1},
            max_bytes=1 << 20,
        )

    payload = _get("/api/telemetry", hours=1)[2]
    assert payload["web"]["requests"] == 1




def test_retrieval_endpoint_serves_the_dev_report(archive_home):
    """The dev page's source. Its subject is the search pipeline rather than the
    corpus, so it reads the ledgers and answers whether or not the index is
    usable — and it comes from the search lab, so a checkout has it."""
    _seed(archive_home)
    status, ctype, payload = _get("/api/retrieval")
    assert status == 200 and ctype == "application/json"
    assert payload["hours"] > 0 and payload["bucket"] in ("hour", "day")


def test_search_lab_endpoint_serves_the_bench_inventory(archive_home):
    """The other dev page's source: what the bench has to measure with. Its rows
    come off the lab's registries and the corpora on disk, so it answers on a box
    where nothing has ever been built — an empty bench is a state, not an error."""
    _seed(archive_home)
    status, ctype, payload = _get("/api/search-lab")
    assert status == 200 and ctype == "application/json"
    assert payload["benchmarks"] and payload["datasets"]
    assert {b["state"] for b in payload["benchmarks"]} <= {
        "missing", "fresh", "stale", "never-run"}


def test_the_run_ledger_is_its_own_route(archive_home):
    """Every recorded benchmark run, rather than the newest of each row the
    inventory carries. Its own route because it is a file read and the inventory
    is a cached filesystem walk — a run that just finished has to appear here
    now, and must not wait out the walk's cache to do it. An empty ledger is a
    box nothing has run on, which is a state and not an error."""
    _seed(archive_home)
    status, ctype, payload = _get("/api/search-lab/runs")
    assert status == 200 and ctype == "application/json"
    assert set(payload) >= {"code_id", "total", "returned", "runs"}
    assert payload["returned"] == len(payload["runs"]) <= payload["total"]
    for run in payload["runs"]:
        assert run["id"] and run["row"] and run["at"]
        assert isinstance(run["on_bench"], bool)
        assert run["code_current"] in (True, False, None)


def test_a_runs_per_query_detail_is_its_own_route(archive_home):
    """Off the run's own sidecar, so opening one run reads one file and the runs
    list above reads none. A run with no detail kept answers empty rather than
    404 — pruned by the cap, never recorded, and never run are all "nothing
    here", and the page says so the same way for each."""
    _seed(archive_home)
    status, ctype, payload = _get("/api/search-lab/runs/deadbeef0000/queries")
    assert status == 200 and ctype == "application/json"
    assert set(payload) >= {"run_id", "order", "total", "misses", "rows"}
    assert payload["rows"] == []


def test_a_run_id_in_the_url_cannot_reach_out_of_the_store(archive_home):
    """The id is a URL segment reaching a filename. The viewer is unauthenticated
    and binds to localhost, so this is the request nobody gets to make."""
    _seed(archive_home)
    for hostile in ("..%2F..%2Fetc%2Fpasswd", "....%2F%2Fconfig", "index.db"):
        status, _, payload = _get(f"/api/search-lab/runs/{hostile}/queries")
        assert status == 200 and payload["rows"] == []


def test_the_ledger_read_is_bounded(archive_home):
    """The ledger is append-only and never pruned, and the viewer is
    unauthenticated — so no request gets to ask for an unbounded read."""
    _seed(archive_home)
    _, _, payload = _get("/api/search-lab/runs", limit=1)
    assert payload["returned"] <= 1


def test_the_inventory_is_assembled_once_and_served_from_cache(archive_home):
    """Assembling it walks the eval cache root — tens of GB across the built
    corpora — so a page that refreshes must not turn into a filesystem sweep per
    request. Identity, not equality: two equal payloads would also be two walks."""
    _seed(archive_home)
    module = devweb_server._lab("inventory")
    assert module is not None, "a checkout has the lab"
    assert (devweb_server._inventory_payload(module)
            is devweb_server._inventory_payload(module))
