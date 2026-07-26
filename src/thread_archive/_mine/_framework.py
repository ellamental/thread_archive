"""The gold-mining framework — the contract every miner shares.

A *miner* turns real corpus signal into snapshot-bound eval ``--cases`` rows: a
graded relevance benchmark that later ``retrieval_eval.py --cases`` runs (and
``retrieval_gold_gate.py``) score against for free. Each miner spends real
agent tokens once to mint labels a cheaper protocol can't; the shapes differ
(one starts from a real query, one from a topic, one reranks a retrieved pool,
one generates queries for a known thread), but they all:

- run against a frozen corpus **snapshot** (``thread_archive snapshot``), so the
  golds bind to a corpus that can't change under them (``require_snapshot``);
- shell their agents into one snapshot-bound corpus seam
  (``python -m thread_archive._mine tool search|read`` — :func:`tool_cmd`);
- append validated rows to a case file under the archive **gold dir**
  (``~/.thread/archive``), each row stamped with its ``miner`` and ``snapshot_id``
  provenance, plus a ``-detail`` sidecar carrying the agent's reasoning;
- skip inputs already mined into that file, so re-running is an append cadence.

A miner is a :class:`Miner` subclass exposing one module-level ``MINER`` instance;
:func:`thread_archive._mine.load_registry` collects them. The ``Miner`` class
attributes (``unit``, ``measures``, ``cost``, ``target_kind``) are what the
``thread_archive mine`` list view reads, so the registry is the single source for
"which miners exist and what each measures" — there is no second catalog to keep
in sync.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The gold dir: mined case files live beside the trend ledgers, out of the repo
# (they quote real usage). A ``~``-string, not a resolved Path — a shipped module
# must not freeze a machine-state path (tests/meta/test_isolation.py); expanded
# at call time.
GOLD_DIR = "~/.thread/archive"


def gold_dir() -> Path:
    """The archive gold dir, expanded now (never frozen at import)."""
    return Path(GOLD_DIR).expanduser()


def default_cases_path(stem: str) -> Path:
    """``<gold dir>/<stem>.jsonl`` — a miner's default output, discoverable by the
    gold gate's ``*cases*.jsonl`` glob (keep ``cases`` in every stem)."""
    return gold_dir() / f"{stem}.jsonl"


def detail_path_for(cases_path: Path) -> Path:
    """The reasoning sidecar beside a case file. The ``detail`` marker keeps it out
    of the gold gate's discovery (it is not a scorable gold file)."""
    return cases_path.with_name(cases_path.stem + "-detail.jsonl")


def open_output(out: Path | None, default: Path) -> tuple[Path, Path]:
    """Resolve a miner's (cases_path, detail_path), honoring an ``--out`` override,
    and ensure the parent dir exists. The detail sidecar always derives from the
    case file so the two travel together."""
    cases_path = out.expanduser() if out else default
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    return cases_path, detail_path_for(cases_path)


def claude_available() -> bool:
    """Whether the ``claude`` CLI is on PATH — mining drives headless agents through
    it, so its absence is a hard stop, not a degraded run."""
    return shutil.which("claude") is not None


def require_snapshot() -> str:
    """The current home's ``snapshot_id``, or ``SystemExit`` if the home is the live
    archive. Mining must run against a frozen snapshot so every case binds to a
    corpus that can't grow underneath the measurement; the id travels in each row
    and the eval refuses to score cases whose id no longer matches the home."""
    from search_lab.snapshot import read_snapshot_id

    sid = read_snapshot_id()
    if sid is None:
        raise SystemExit(
            "mining must run against a corpus snapshot, not the live archive: "
            "`python search_lab/snapshot.py <dir>`, then point THREAD_ARCHIVE_HOME at "
            "it. Cases carry the snapshot's id so the eval can reject them once "
            "the corpus has moved on."
        )
    return sid


def tool_cmd() -> str:
    """The command a mining agent shells into for snapshot-bound corpus access:
    ``python -m thread_archive._mine tool search|read``. Uses ``-m`` on the running
    interpreter (not a hardcoded repo path) so the agent resolves the package
    through whatever environment launched the run; it inherits
    ``THREAD_ARCHIVE_HOME`` (the snapshot) from that run's env."""
    return f"{sys.executable} -m thread_archive._mine tool"


# The most agent sessions one ``thread_archive mine`` command launches — a single
# miner's ``--target`` (or a topic survey's angle count), and the *total* a
# ``mine all`` sweep spends across its miners. A spend guard so a fat-fingered
# ``--target 500`` (or a wide sweep) runs bounded instead of running up a bill.
# "Like 25": generous for a real mining cadence, fatal only to mistakes. How many
# run *at once* is a separate ceiling — ``_agent.MAX_CONCURRENT_SESSIONS``.
MAX_SESSIONS_PER_RUN = 25


def clamp_sessions(requested: int) -> int:
    """A per-run agent-session count, clamped to :data:`MAX_SESSIONS_PER_RUN`. A
    non-positive count (a batch miner that carries no ``--target``) passes through
    untouched — there is nothing to bound."""
    return min(requested, MAX_SESSIONS_PER_RUN) if requested > 0 else requested


def clamp_jobs(requested: int) -> int:
    """A concurrent-agent count, clamped to the global session ceiling and floored
    at one. More workers than ``_agent.MAX_CONCURRENT_SESSIONS`` would only block on
    the shared semaphore, so cap them where they're requested rather than spawn
    threads that idle."""
    from ._agent import MAX_CONCURRENT_SESSIONS

    return max(1, min(requested, MAX_CONCURRENT_SESSIONS))


def mined_queries(path: Path) -> set[str]:
    """Queries already in a case file — re-runs append only new ones. Robust to a
    half-written or hand-edited file: junk lines are skipped, not fatal."""
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.add(json.loads(line)["query"])
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def mined_gold_ids(path: Path) -> set[str]:
    """Every thread id that already appears as gold in a case file — the resume key
    for miners whose unit is a *thread* (query-gen), not a query, so a re-run does
    not re-mine a thread already represented."""
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        for tid in row.get("gold", []) or []:
            out.add(str(tid))
    return out


def validate_gold(gold: list[str], *, sessions: set[str], resolve) -> list[str]:
    """Golds that hold up: resolvable to a canonical thread id and not the
    originating session (which quotes the query verbatim). ``resolve(ref) ->
    canonical tid | None``. Existence needs no separate check — the agent searched
    the snapshot, so any gold it returns is already in the frozen corpus. Order
    preserved, dupes dropped."""
    out: list[str] = []
    for ref in gold:
        tid = resolve(ref)
        if tid is None or tid in sessions or tid in out:
            continue
        out.append(tid)
    return out


def now_iso() -> str:
    """A UTC ISO timestamp for the ``mined_at`` provenance stamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prompt_sha(prompt: str) -> str:
    """First 12 hex of the SHA-256 of the exact agent prompt. Stamped on each
    mined case so a re-mint under a changed prompt is *detectable* rather than
    silently redefining a benchmark beneath an old floor keyed only by filename —
    the prompt is half of what produced the judgment, and it changes without the
    corpus snapshot changing."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


@functools.lru_cache(maxsize=1)
def miner_commit() -> str | None:
    """The short SHA of the mining code, cached for the process (a run doesn't
    straddle a commit). ``None`` outside a git checkout — best-effort provenance,
    never a hard dependency. Shares the one git-commit reader with the gold-run
    ledger so both ledgers name the code the same way."""
    from search_lab.gold_runs import git_commit

    return git_commit()


@dataclass
class MineContext:
    """The resolved common config a miner's :meth:`Miner.run` receives. Output
    paths and the already-mined set are the miner's own to resolve (topic needs
    the topic slug; query-gen dedupes on thread, not query), so they live behind
    the miner's helpers, not here."""

    snapshot_id: str
    target: int
    model: str
    jobs: int
    tool_cmd: str
    args: argparse.Namespace
    # The agent seam a run drives — defaults to the real ``run_claude`` when None.
    # A test supplies a fake so a miner's full ``run`` path (sampling, executor,
    # case writing) is exercised without spawning headless agents.
    agent_run: object = None


@dataclass
class MineResult:
    """What a run produced — the summary the CLI prints. ``notes`` carries
    per-run remarks (e.g. a ``none-of-pool`` rate, an ``--all`` skip reason).

    ``attempted`` (units drawn — queries judged, threads sampled) and ``outcomes``
    (the per-unit disposition breakdown, e.g. ``{"ok": 8, "none-of-pool": 2}``) are
    the denominator behind ``written``/``failed``: the CLI persists them to the
    mining ledger (:mod:`thread_archive._ops.mine_runs`) so an abstention/drop rate
    is a recorded timeseries, not a number that lived only in the run's console
    line. A miner that leaves them at their defaults simply records no breakdown."""

    written: int = 0
    failed: int = 0
    cases_path: Path | None = None
    detail_path: Path | None = None
    notes: list[str] = field(default_factory=list)
    attempted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


class CaseWriter:
    """Appends validated case rows and detail rows to the standard files, stamping
    each case with its ``miner`` provenance. Miners build the domain fields of a
    row (gold, grades, protocol, snapshot_id); the writer only guarantees the
    provenance stamp and the append discipline, so no miner forgets it."""

    def __init__(self, miner: str, cases_path: Path, detail_path: Path):
        self.miner = miner
        self.cases_path = cases_path
        self.detail_path = detail_path

    def write_case(self, row: dict) -> None:
        row.setdefault("miner", self.miner)
        with self.cases_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def write_detail(self, row: dict) -> None:
        with self.detail_path.open("a") as f:
            f.write(json.dumps(row) + "\n")


class Miner:
    """The miner contract. A concrete miner sets the descriptor attributes (read
    by the list view), adds any miner-specific CLI arguments, and implements
    :meth:`run`. The framework owns the common arguments and the snapshot / claude
    guards; the miner owns its sampling, its agents, and its output path."""

    #: registry key and CLI name (short, lowercase)
    name: str = ""
    #: one-line description for the list view
    summary: str = ""
    #: what the miner's golds measure — "precision (in-pool)", "recall
    #: (findability)", "confound ranking", "precision+recall (grounded)"
    measures: str = ""
    #: the unit a target counts, human-readable ("query", "thread", "run")
    unit: str = ""
    #: rough token cost, human-readable ("1 opus agent / query (~min each)")
    cost: str = ""
    #: "per-case" (``--target`` bounds the run) or "batch" (count is the miner's
    #: own call — e.g. a survey decides how many angles a topic warrants)
    target_kind: str = "per-case"
    #: how ``--target`` is interpreted, for the list view / help
    target_help: str = ""
    #: default ``--target`` for a per-case miner
    default_target: int = 5
    #: default output basename (no extension); keep ``cases`` in it so the gold
    #: gate discovers it. May be resolved dynamically (topic embeds a slug).
    cases_stem: str = ""
    #: can ``mine all N`` drive this miner with only a count? False when it needs
    #: a mandatory argument (topic needs ``--topic``).
    runnable_in_all: bool = True

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Add miner-specific CLI arguments. The framework has already added the
        common ones (``--model``, ``--jobs``, ``--out``, ``--seed``, and
        ``--target`` for per-case miners)."""

    def run(self, ctx: MineContext) -> MineResult:
        """Mine against the snapshot, appending validated cases + detail. Prints
        its own per-case progress (like the shipped scripts it replaces) and
        returns the run summary."""
        raise NotImplementedError


def add_common_arguments(parser: argparse.ArgumentParser, miner: Miner) -> None:
    """The arguments every miner accepts. ``--target`` is added only for per-case
    miners (a batch miner's count is not the operator's to set)."""
    from ._agent import DEFAULT_MODEL, MAX_CONCURRENT_SESSIONS

    if miner.target_kind == "per-case":
        parser.add_argument(
            "--target", type=int, default=miner.default_target, metavar="N",
            help=f"how many to mine (unit: {miner.unit}; default "
            f"{miner.default_target}; capped at {MAX_SESSIONS_PER_RUN}/run)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="claude CLI model alias for the mining agents")
    parser.add_argument("--jobs", type=int, default=2,
                        help=f"concurrent agents (capped at {MAX_CONCURRENT_SESSIONS})")
    parser.add_argument("--seed", type=int, default=7, help="sampling seed")
    parser.add_argument("--out", type=Path, default=None, metavar="PATH",
                        help="case file to append to (default: under "
                        "~/.thread/archive); the detail sidecar lands beside it")
