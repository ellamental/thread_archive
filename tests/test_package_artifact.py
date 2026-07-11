"""The distributable artifact, not the checkout: build the wheel + sdist, prove
their contents, then install the wheel into a clean venv and run the real
``archive`` CLI lifecycle from it.

This is the release lane (``-m package`` — deselected from the default run, see
pyproject). The rest of the suite runs against the editable checkout, which
cannot catch a packaging regression: a schema or web asset missing from the
wheel, a stray top-level package, a broken entry point. Every assertion here is
against the built artifact or the cleanly-installed environment.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.package

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
    # provider JSON schemas are data files — the easiest thing to lose in packaging
    assert any(n.startswith("thread_archive/_thread_import/schemas/") and n.endswith(".json")
               for n in names)
    # the pre-built web viewer ships so `pip install` needs no node
    assert "thread_archive/_web/static/index.html" in names
    assert any(n.startswith("thread_archive/_web/static/assets/") and n.endswith(".js")
               for n in names)


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
    assert "archive = thread_archive.cli:main" in ep
    assert "archive-mcp = " in ep
    assert "archive-librarian-mcp = " in ep


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
    # Retrieval has no CLI verbs (search/read are the MCP tools, exercised
    # below); the CLI lifecycle is ingest + the durability kit.
    home = tmp_path
    session_file = home / "sess.jsonl"
    session_file.write_text(
        "\n".join(json.dumps(x) for x in SESSION) + "\n", encoding="utf-8")

    r = _run(installed, ["archive", "import", str(session_file)], home)
    assert r.returncode == 0, r.stderr

    r = _run(installed, ["archive", "status"], home)
    assert r.returncode == 0, r.stderr
    assert "threads: 1" in r.stdout

    r = _run(installed, ["archive", "reindex"], home)
    assert r.returncode == 0, r.stderr

    r = _run(installed, ["archive", "verify"], home)
    assert r.returncode == 0, f"verify red on a fresh install:\n{r.stdout}\n{r.stderr}"


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
    r = _run(installed, ["archive", "import", str(session_file)], home)
    assert r.returncode == 0, r.stderr

    responses = _mcp_session(installed, home, [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "thread_search",
                    "arguments": {"query": "packaged lifecycle"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "thread_read", "arguments": {"thread_id": 1}}},
    ])
    search = _tool_result(responses, 2)
    assert not search.get("isError"), search["content"][0]["text"]
    assert "probe" in search["content"][0]["text"]
    read = _tool_result(responses, 3)
    assert not read.get("isError"), read["content"][0]["text"]
    assert "packaged lifecycle probe" in read["content"][0]["text"]


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
        "for prov in ('chatgpt', 'claude', 'claude-code', 'cursor'):\n"
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
