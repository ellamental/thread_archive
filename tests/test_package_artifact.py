"""The distributable artifact, not the checkout: build the wheel + sdist, prove
their contents, then install the wheel into a clean venv and run the real
``thread_archive`` CLI lifecycle from it.

This is the release lane (``-m package`` — deselected from the default run, see
pyproject). The rest of the suite runs against the editable checkout, which
cannot catch a packaging regression: a schema or web asset missing from the
wheel, a stray top-level package, a broken entry point. Every assertion here is
against the built artifact or the cleanly-installed environment.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

# The larger timeout outranks the suite-wide 300s hang cap: the session-scoped
# wheel build + clean-venv install is charged to whichever test runs first and
# can be minutes on a loaded box.
pytestmark = [pytest.mark.package, pytest.mark.timeout(900)]

REPO = Path(__file__).resolve().parent.parent

SESSION = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/p", "message": {"role": "user", "content": "packaged lifecycle probe"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T10:00:05Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "m",
                                    "content": [{"type": "text", "text": "probe answer"}]}},
]


@pytest.fixture(scope="session")
def dist(tmp_path_factory) -> tuple[Path, Path]:
    """Build the wheel + sdist once for the whole lane. Returns (wheel, sdist)."""
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out), str(REPO)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"python -m build failed:\n{r.stdout}\n{r.stderr}"
    (wheel,) = out.glob("*.whl")
    (sdist,) = out.glob("*.tar.gz")
    return wheel, sdist


@pytest.fixture(scope="session")
def installed(dist, tmp_path_factory) -> Path:
    """A clean venv with ONLY the built wheel installed. Returns its bin dir."""
    wheel, _ = dist
    venv = tmp_path_factory.mktemp("venv")
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    bin_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
    r = subprocess.run(
        [str(bin_dir / "pip"), "install", "--quiet", str(wheel)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"wheel install failed:\n{r.stdout}\n{r.stderr}"
    return bin_dir


# ── artifact contents ────────────────────────────────────────────────────────

def test_wheel_carries_the_whole_runtime(dist) -> None:
    wheel, _ = dist
    names = zipfile.ZipFile(wheel).namelist()

    assert "thread_archive/__init__.py" in names
    assert "thread_archive/cli.py" in names
    # the vendored parser island ships inside the package
    assert "thread_archive/_thread_import/__init__.py" in names
    # the pre-built web viewer ships so `pip install` needs no node
    assert "thread_archive/_web/static/index.html" in names
    assert any(n.startswith("thread_archive/_web/static/assets/") and n.endswith(".js")
               for n in names)


def test_wheel_carries_no_measurement_surface(dist) -> None:
    # Everything that scores search — the scoring core, the harnesses, corpus
    # freezing, the run ledgers — lives in search_lab/, which is repo territory.
    # An install gets preservation and retrieval and no instruments.
    wheel, _ = dist
    names = zipfile.ZipFile(wheel).namelist()
    leaked = [n for n in names
              if "search_lab" in n
              or n.endswith(("_eval.py", "eval_core.py", "run_meta.py",
                             "retrieval_report.py"))]
    assert not leaked, f"measurement surface leaked into the wheel: {leaked}"


def test_sdist_keeps_the_search_lab(dist) -> None:
    # The other half of the split: the sdist ships tests, and the quality tests
    # import search_lab.* — dropping the lab there would ship a red suite.
    _, sdist = dist
    names = tarfile.open(sdist).getnames()
    assert any("/search_lab/eval_core.py" in n for n in names)
    assert any("/search_lab/benchmark.py" in n for n in names)


def test_wheel_plants_no_public_top_level_packages(dist) -> None:
    wheel, _ = dist
    top = {n.split("/", 1)[0] for n in zipfile.ZipFile(wheel).namelist()}
    # exactly one importable package + its dist-info: no public `thread_import`,
    # no stray `tests`, `src`, or `archive`
    unexpected = {t for t in top if t != "thread_archive" and not t.endswith(".dist-info")}
    assert not unexpected, f"wheel plants unexpected top-level names: {unexpected}"


def test_wheel_declares_all_entry_points(dist) -> None:
    wheel, _ = dist
    zf = zipfile.ZipFile(wheel)
    (ep_name,) = [n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt")]
    ep = zf.read(ep_name).decode()
    # One namespaced front door — every verb (incl. `setup`) under thread_archive,
    # both spellings. No bare, generic `archive` script squatting a user's PATH.
    assert "thread_archive = thread_archive.cli:main" in ep
    assert "thread-archive = thread_archive.cli:main" in ep
    assert "\narchive = " not in ep, f"a bare `archive` console script leaked:\n{ep}"
    # The MCP server keeps its own script — what MCP clients point at.
    assert "archive-mcp = " in ep


def test_sdist_ships_sources_and_tests_but_no_node_modules(dist) -> None:
    _, sdist = dist
    names = tarfile.open(sdist).getnames()
    rel = {n.split("/", 1)[1] for n in names if "/" in n}  # strip the version dir
    assert any(n.startswith("src/thread_archive/") for n in rel)
    assert any(n.startswith("tests/") for n in rel)
    assert any(n.startswith("frontend/src") for n in rel)
    assert not any("node_modules" in n for n in rel)
    assert not any(n.startswith("frontend/dist") for n in rel)


# ── the installed environment ────────────────────────────────────────────────

def _run(bin_dir: Path, argv: list[str], home: Path) -> subprocess.CompletedProcess:
    # cwd OUTSIDE the checkout so imports resolve from site-packages, never src/
    return subprocess.run(
        [str(bin_dir / argv[0]), *argv[1:]],
        capture_output=True, text=True, cwd=str(home),
        env={"PATH": str(bin_dir), "HOME": str(home), "THREAD_ARCHIVE_HOME": str(home / "archive")},
    )


def test_installed_cli_lifecycle_import_reindex_verify(installed, tmp_path) -> None:
    # Ingest, the backup kit, and — from the same console script — retrieval:
    # `search` / `read` are the MCP tools with a terminal in front of them (the
    # MCP door is driven over stdio below), so the install has to answer on both.
    home = tmp_path
    session_file = home / "sess.jsonl"
    session_file.write_text(
        "\n".join(json.dumps(x) for x in SESSION) + "\n", encoding="utf-8")

    r = _run(installed, ["thread_archive","import", str(session_file)], home)
    assert r.returncode == 0, r.stderr

    r = _run(installed, ["thread_archive","status"], home)
    assert r.returncode == 0, r.stderr
    assert "threads: 1" in r.stdout

    r = _run(installed, ["thread_archive","reindex"], home)
    assert r.returncode == 0, r.stderr

    r = _run(installed, ["thread_archive","verify"], home)
    assert r.returncode == 0, f"verify red on a fresh install:\n{r.stdout}\n{r.stderr}"

    # Retrieval, end to end on the installed package: browse to a thread id, then
    # read that thread back.
    r = _run(installed, ["thread_archive","search","--output","linkable"], home)
    assert r.returncode == 0, r.stderr
    thread_id = json.loads(r.stdout)[0]["thread_id"]

    r = _run(installed, ["thread_archive","read", thread_id], home)
    assert r.returncode == 0, r.stderr
    assert "[USER" in r.stdout


# ── the installed MCP server: the actual consumer path ───────────────────────
# `claude mcp add thread-archive -- archive-mcp` is the whole advertised setup,
# so the installed `archive-mcp` binary is driven here the way a client does:
# JSON-RPC over stdio. Ingest is pinned off so the lane never scans the host's
# real AI-tool stores.

def _mcp_session(bin_dir: Path, home: Path, requests: list[dict]) -> dict[int, dict]:
    """Pipe ``requests`` (plus the initialize handshake) into ``archive-mcp``
    and return responses keyed by request id."""
    handshake: list[dict] = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "package-lane", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    expected = {m["id"] for m in requests if "id" in m} | {1}
    payload = "".join(json.dumps(m) + "\n" for m in handshake + requests)
    # stdin must stay open until every response lands — the server treats EOF
    # as shutdown and drops in-flight requests — so write-all/read-until-done
    # rather than subprocess.run.
    proc = subprocess.Popen(
        [str(bin_dir / "archive-mcp")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=str(home),
        env={"PATH": str(bin_dir), "HOME": str(home),
             "THREAD_ARCHIVE_HOME": str(home / "archive"),
             "THREAD_ARCHIVE_MCP_INGEST": "0"},
    )
    responses: dict[int, dict] = {}
    killer = threading.Timer(180, proc.kill)
    killer.start()
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
        while expected - set(responses):
            line = proc.stdout.readline()
            if not line:
                break  # server exited (or was killed by the watchdog)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg.get("id"), int):
                responses[msg["id"]] = msg
    finally:
        killer.cancel()
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()
    assert responses, "archive-mcp produced no JSON-RPC responses"
    return responses


def _tool_result(responses: dict[int, dict], rid: int) -> dict:
    assert rid in responses, f"no response for request {rid}: {sorted(responses)}"
    assert "result" in responses[rid], responses[rid]
    return responses[rid]["result"]


def test_installed_mcp_first_search_on_virgin_home(installed, tmp_path) -> None:
    # The brand-new consumer's first tool call: an EMPTY archive home, a search
    # racing the server's startup warm thread through first-open schema
    # creation. Must answer cleanly, never "table already exists".
    responses = _mcp_session(installed, tmp_path, [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "thread_search", "arguments": {"query": "hello world"}}},
    ])
    tools = {t["name"] for t in _tool_result(responses, 2)["tools"]}
    assert {"thread_search", "thread_read"} <= tools
    search = _tool_result(responses, 3)
    text = search["content"][0]["text"]
    assert not search.get("isError"), f"first search on a virgin home errored:\n{text}"


def test_installed_mcp_search_and_read_over_imported_data(installed, tmp_path) -> None:
    home = tmp_path
    session_file = home / "sess.jsonl"
    session_file.write_text(
        "\n".join(json.dumps(x) for x in SESSION) + "\n", encoding="utf-8")
    r = _run(installed, ["thread_archive","import", str(session_file)], home)
    assert r.returncode == 0, r.stderr

    responses = _mcp_session(installed, home, [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "thread_search",
                    "arguments": {"query": "packaged lifecycle"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "thread_read", "arguments": {"thread_id": "sess"}}},
    ])
    search = _tool_result(responses, 2)
    assert not search.get("isError"), search["content"][0]["text"]
    assert "probe" in search["content"][0]["text"]
    read = _tool_result(responses, 3)
    assert not read.get("isError"), read["content"][0]["text"]
    assert "packaged lifecycle probe" in read["content"][0]["text"]


# ── the realistic first run: discovery-driven ingest from real store locations ──
# The lifecycle test above hand-feeds a session path to `import`. This proves the
# path a new user's first run actually takes: a fake $HOME with every harness's
# store in its REAL default location (~/.claude/projects, ~/.codex/sessions, the
# OS-correct app-data dir for Cursor/Cowork), discovered and ingested by the
# installed `thread-archive watch --once` with no hand-fed paths, then reindexed
# and searched back. Runs against the clean wheel-only venv, so it doubles as the
# cross-OS install proof this `package` lane runs on both macOS (the maintainer's
# local CI) and Linux (GitHub Actions). The logic lives in tests/install/first_run.py, shared
# with the clean-container Docker install lane.

def test_installed_first_run_discovers_realistic_stores_and_searches(installed, tmp_path) -> None:
    sys.path.insert(0, str(REPO / "tests" / "install"))
    import first_run

    # keep=True: pytest owns tmp_path's cleanup, so first_run must not rmtree it.
    first_run.run(bin_dir=installed, home=tmp_path, keep=True)


def test_installed_cli_advertises_only_verbs_an_install_can_run(installed, tmp_path) -> None:
    # Every verb in `--help` must be one this wheel can actually execute, and every
    # verb it executes must be listed: no hidden commands, and nothing advertised
    # that degrades to a pointer at the repo. The measurement verbs are gone rather
    # than hidden, so the two sets are now the same set.
    h = _run(installed, ["thread_archive", "--help"], tmp_path)
    assert h.returncode == 0, h.stderr
    listed = set(re.findall(r"^\s{4}([a-z][a-z-]+)\b", h.stdout, re.M))
    assert listed, h.stdout
    for gone in ("mine", "eval", "snapshot", "archives"):
        assert gone not in listed, f"{gone} is advertised but no longer exists"

    for verb in sorted(listed):
        r = _run(installed, ["thread_archive", verb, "--help"], tmp_path)
        assert r.returncode == 0, f"{verb} --help failed: {r.stdout}\n{r.stderr}"
        assert "Traceback" not in r.stderr, f"{verb}: {r.stderr}"

    # And a removed verb is an argparse error, never an ImportError from a module
    # the wheel no longer carries.
    gone = _run(installed, ["thread_archive", "mine"], tmp_path)
    assert gone.returncode == 2
    assert "invalid choice" in gone.stderr and "Traceback" not in gone.stderr


def test_installed_uninstall_points_at_the_package_it_came_from(installed, tmp_path) -> None:
    # A wheel install has no clone to delete, so the way out is `pip uninstall` —
    # and this lane is the only place that branch is real: the repo's own suite
    # runs from a checkout, where the verb correctly names the clone instead.
    r = _run(installed, ["thread_archive", "uninstall", "--yes"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "pip uninstall thread-archive" in r.stdout
    assert "clone" not in r.stdout
    # And it says where the conversations are before it says how to remove the code.
    assert str(tmp_path / "archive") in r.stdout


def test_installed_package_is_private_and_asset_complete(installed, tmp_path) -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "import thread_archive\n"
        "assert 'site-packages' in thread_archive.__file__, thread_archive.__file__\n"
        "from thread_archive._web import server\n"
        "assets = Path(server.STATIC_DIR) / 'assets'\n"
        "assert (Path(server.STATIC_DIR) / 'index.html').is_file(), 'viewer shell missing'\n"
        "assert any(p.suffix == '.js' for p in assets.iterdir()), 'built JS missing'\n"
        "from thread_archive._thread_import import get_parser\n"
        "for prov in ('chatgpt', 'claude', 'claude-code'):\n"
        "    assert get_parser(prov) is not None, prov\n"
        "try:\n"
        "    import thread_import\n"
        "except ModuleNotFoundError:\n"
        "    pass\n"
        "else:\n"
        "    sys.exit('public top-level thread_import leaked into site-packages')\n"
    )
    r = _run(installed, ["python", "-c", code], tmp_path)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
