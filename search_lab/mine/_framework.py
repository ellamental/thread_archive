"""The gold-mining framework — the contract every miner shares.

A *miner* turns real corpus signal into snapshot-bound eval ``--cases`` rows: a
graded relevance benchmark a later ``retrieval_eval.py --cases`` run scores
against for free. Each miner spends real agent tokens once to mint labels a
cheaper protocol can't. A miner's labels must
be fixed by something **outside** retrieval (see :mod:`search_lab.mine` for the
admission rule and :attr:`Miner.gold_source` for where each one declares it);
whatever the signal, they all:

- run against a frozen corpus **snapshot** (``python search_lab/snapshot.py <dir>``),
  so the golds bind to a corpus that can't change under them (``require_snapshot``);
- shell their agents into one snapshot-bound corpus seam
  (``… mine/__main__.py tool search|read`` — :func:`tool_cmd`);
- append validated rows to a case file under the archive **gold dir**
  (``~/.thread/archive``), each row stamped with its ``miner`` and ``snapshot_id``
  provenance, plus a ``-detail`` sidecar carrying the agent's reasoning;
- skip inputs already mined into that file, so re-running is an append cadence.

A miner is a :class:`Miner` subclass exposing one module-level ``MINER`` instance;
:func:`search_lab.mine.load_registry` collects them. The ``Miner`` class
attributes (``unit``, ``measures``, ``cost``, ``target_kind``) are what the
``mine`` list view reads, so the registry is the single source for
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
    """``<gold dir>/<stem>.jsonl`` — a miner's default output, discoverable by
    :mod:`search_lab.gold_files`' ``*cases*.jsonl`` rule (keep ``cases`` in every
    stem)."""
    return gold_dir() / f"{stem}.jsonl"


def detail_path_for(cases_path: Path) -> Path:
    """The reasoning sidecar beside a case file. The ``detail`` marker keeps it out
    of case-file discovery (it is not a scorable gold file)."""
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
    from ..snapshot import read_snapshot_id

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
    ``python <abs>/search_lab/mine/__main__.py tool search|read``.

    The running interpreter (so the agent gets the environment that launched the
    run) and an absolute script path (so it does not matter what directory the
    agent's Bash session starts in — ``-m search_lab.mine`` would need the
    checkout root on the path, which only holds when cwd happens to be it). It
    inherits ``THREAD_ARCHIVE_HOME`` — the snapshot — from the run's env.

    This string is also the agents' Bash allowlist prefix (``_agent.run_claude``),
    so it must stay a plain command: no environment assignments in front of it.
    """
    entry = Path(__file__).resolve().parent / "__main__.py"
    return f"{sys.executable} {entry} tool"


# The most agent sessions one ``python -m search_lab.mine`` command launches — a single
# miner's ``--target`` (or, for a batch miner, whatever count it sizes itself to),
# and the *total* a ``mine all`` sweep spends across its miners. A spend guard so a fat-fingered
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
    for miners whose unit is a *thread* rather than a query, so a re-run does not
    re-mine a thread already represented."""
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
    """First 12 hex of the SHA-256 of the exact *rendered* agent prompt — the
    per-unit fingerprint. Distinct for every case by construction, since an
    authoring prompt embeds that unit's own commit or diff, so it identifies one
    judgment and never a population. Use :func:`template_sha` for the question
    "were these cases minted under the same instructions"."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def template_sha(template: str) -> str:
    """First 12 hex of the SHA-256 of an *unrendered* prompt template.

    This is what makes a re-mint under changed instructions detectable rather than
    a silent redefinition of a benchmark under an unchanged filename. The rendered
    prompt cannot do it: it varies per unit whatever the instructions say, so a
    file of 25 cases carries 25 rendered hashes and any comparison across them is
    noise. The template is the half of the prompt that is the same for every case,
    which is exactly the half a reader needs to know did not move."""
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]


@functools.lru_cache(maxsize=1)
def miner_commit() -> str | None:
    """The short SHA of the mining code, cached for the process (a run doesn't
    straddle a commit). ``None`` outside a git checkout — best-effort provenance,
    never a hard dependency. Shares the one git-commit reader with every other
    ledger so they all name the code the same way."""
    from ..run_meta import git_commit

    return git_commit()


# ── the pipeline ────────────────────────────────────────────────────────────
#
# A miner is a funnel, not a function. Units enter from a corpus record, most of
# them are dropped somewhere, and what a mined number means depends entirely on
# *where* they were dropped and why: a unit refused because its provenance does
# not hold up is a broken label removed, a unit refused because its commit message
# is uninformative is a hard case removed, and the two move a benchmark in opposite
# directions. A flat count of terminal dispositions cannot tell them apart.
#
# So the shape below is a declared sequence of stages, each recording what it took
# in, what it passed on, and its reasons for the difference. That record is what
# the ledger persists and what a plan run prints before spending anything.


@dataclass
class Verdict:
    """One stage's decision about one unit.

    ``keep`` is the unit carried forward — a stage may transform it (attach the
    grades it built, the agent's reply) — or ``None`` to drop. ``reason`` is
    recorded either way, so a stage that passes a unit for a *particular* reason
    ("provenance confirmed") says so as loudly as one that refuses it."""

    keep: object | None = None
    reason: str = "ok"
    cost_usd: float = 0.0
    detail: dict | None = None

    @property
    def kept(self) -> bool:
        return self.keep is not None


@dataclass
class Stage:
    """One step of a miner's funnel.

    ``kind`` is load-bearing rather than descriptive: ``free`` stages read the
    corpus and cost nothing, ``agent`` stages spend tokens. A plan run executes
    every free stage and stops at the first agent one, which is only meaningful
    because the split is declared here — so put the cheap gate first and let it
    narrow what the expensive one is asked about."""

    name: str
    fn: object                      # (unit, ctx) -> Verdict
    kind: str = "free"              # "free" (costs nothing) | "agent" (spends)
    summary: str = ""


@dataclass
class StageStat:
    """What one stage did to one run's units — the funnel row."""

    stage: str
    kind: str
    n_in: int
    n_out: int
    reasons: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    seconds: float = 0.0

    @property
    def dropped(self) -> int:
        return self.n_in - self.n_out

    def as_row(self) -> dict:
        row = {"stage": self.stage, "kind": self.kind, "in": self.n_in,
               "out": self.n_out, "reasons": self.reasons}
        if self.cost_usd:
            row["cost_usd"] = round(self.cost_usd, 4)
        if self.seconds:
            row["seconds"] = round(self.seconds, 1)
        return row


class Funnel:
    """A run's stage-by-stage record, in order.

    Set-level narrowing (a supply read, a stratified draw) is recorded with
    :meth:`note` rather than by running a stage per item, because those steps have
    no per-unit verdict to give — but they belong on the same funnel, since "1,284
    linkage rows, 1,149 resolve in this snapshot, 12 sampled" is most of what a
    reader wants before any agent runs."""

    def __init__(self) -> None:
        self.stats: list[StageStat] = []

    def note(self, stage: str, *, n_in: int, n_out: int,
             reasons: dict[str, int] | None = None, kind: str = "free") -> None:
        self.stats.append(StageStat(stage=stage, kind=kind, n_in=n_in, n_out=n_out,
                                    reasons=reasons or {}))

    def record(self, stat: StageStat) -> None:
        self.stats.append(stat)

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.stats)

    def rows(self) -> list[dict]:
        return [s.as_row() for s in self.stats]

    def text(self, *, indent: str = "  ") -> str:
        """The funnel as an aligned block — what a run prints and a plan shows."""
        if not self.stats:
            return f"{indent}(no stages ran)"
        width = max(len(s.stage) for s in self.stats)
        lines = []
        for s in self.stats:
            marker = "$" if s.kind == "agent" else " "
            line = f"{indent}{marker} {s.stage:<{width}}  {s.n_in:>5} → {s.n_out:<5}"
            if s.dropped:
                why = ", ".join(f"{r} {n}" for r, n in sorted(s.reasons.items())
                                if r != "ok")
                line += f"  −{s.dropped}" + (f"  ({why})" if why else "")
            if s.cost_usd:
                line += f"   ${s.cost_usd:.2f}"
            lines.append(line)
        return "\n".join(lines)


def run_stage(stage: Stage, units: list, ctx: "MineContext", *,
              jobs: int = 1, on_verdict=None) -> tuple[list, StageStat]:
    """Apply one stage to every unit, returning the survivors and the funnel row.

    A stage that raises on a unit drops that unit as ``stage-error`` rather than
    ending the run: one malformed row out of a thousand is a data fact, and a miner
    that dies on it has spent everything before it for nothing. ``on_verdict`` sees
    every verdict, kept or dropped, so the caller can write the detail sidecar
    without the stage knowing where details go."""
    import time

    started = time.monotonic()
    stat = StageStat(stage=stage.name, kind=stage.kind, n_in=len(units), n_out=0)
    kept: list = []

    def apply(unit):
        try:
            return stage.fn(unit, ctx)
        except Exception as exc:                      # noqa: BLE001 — see docstring
            return Verdict(reason="stage-error",
                           detail={"error": f"{type(exc).__name__}: {exc}"[:300]})

    if jobs > 1 and units:
        with concurrent_futures().ThreadPoolExecutor(max_workers=jobs) as ex:
            verdicts = list(ex.map(apply, units))
    else:
        verdicts = [apply(u) for u in units]

    for unit, verdict in zip(units, verdicts):
        stat.reasons[verdict.reason] = stat.reasons.get(verdict.reason, 0) + 1
        stat.cost_usd += verdict.cost_usd
        if on_verdict is not None:
            on_verdict(stage, unit, verdict)
        if verdict.kept:
            kept.append(verdict.keep)
    stat.n_out = len(kept)
    stat.seconds = time.monotonic() - started
    return kept, stat


def run_pipeline(stages: list[Stage], units: list, ctx: "MineContext", *,
                 funnel: Funnel, on_verdict=None, echo: bool = True) -> list:
    """Drive units through the declared stages, recording each into ``funnel``.

    Under :attr:`MineContext.plan` this stops at the first ``agent`` stage. That is
    the whole value of declaring ``kind``: every free stage can run for nothing, so
    the narrowing they do is knowable *before* any spend, and what survives them is
    the bill. A plan is therefore not an estimate of the funnel — it is the real
    funnel, truncated where the money starts."""
    for stage in stages:
        if ctx.plan and stage.kind == "agent":
            if echo:
                print(f"  — plan stops here: {len(units)} unit(s) would enter "
                      f"{stage.name!r} and every stage after it")
            break
        jobs = ctx.jobs if stage.kind == "agent" else 1
        units, stat = run_stage(stage, units, ctx, jobs=jobs, on_verdict=on_verdict)
        funnel.record(stat)
        if echo:
            line = f"  {stage.name}: {stat.n_in} → {stat.n_out}"
            if stat.dropped:
                line += "  (−" + str(stat.dropped) + ": " + ", ".join(
                    f"{r} {n}" for r, n in sorted(stat.reasons.items())
                    if r != "ok") + ")"
            print(line)
    return units


def planned_spend(stages: list[Stage], n_units: int) -> list[str]:
    """The agent sessions a plan says the run would cost, per remaining stage.

    Stated as sessions rather than dollars because a session's price depends on
    what the agent reads, and a made-up dollar figure would be quoted back as
    though it were measured. What a stage *actually* cost is on the funnel of every
    run that has happened."""
    out: list[str] = []
    remaining = n_units
    for stage in stages:
        if stage.kind != "agent":
            continue
        out.append(f"{stage.name}: up to {remaining} agent session(s)")
    return out


def concurrent_futures():
    """Imported through a function so the module stays import-light for the corpus
    tool seam (see :mod:`search_lab.mine`)."""
    import concurrent.futures

    return concurrent.futures


@dataclass
class MineContext:
    """The resolved common config a miner's :meth:`Miner.run` receives. Output
    paths and the already-mined set are the miner's own to resolve (a stem may
    embed a slug, and the resume key is the miner's unit — thread or query), so
    they live behind the miner's helpers, not here."""

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
    # A dry run: execute every free stage, stop at the first spending one, write no
    # cases and record no ledger row. Not a simulation — the free stages really run,
    # so the funnel it prints is the true one up to the point money starts.
    plan: bool = False


@dataclass
class MineResult:
    """What a run produced — the summary the CLI prints. ``notes`` carries
    per-run remarks (e.g. a ``none-of-pool`` rate, an ``--all`` skip reason).

    ``attempted`` (units drawn — queries judged, threads sampled) and ``outcomes``
    (the per-unit disposition breakdown, e.g. ``{"ok": 8, "none-of-pool": 2}``) are
    the denominator behind ``written``/``failed``: the CLI persists them to the
    mining ledger (:mod:`search_lab.mine_runs`) so an abstention/drop rate is a
    recorded timeseries, not a number that lives only in the run's console line.
    A miner that leaves them at their defaults simply records no breakdown."""

    written: int = 0
    failed: int = 0
    cases_path: Path | None = None
    detail_path: Path | None = None
    notes: list[str] = field(default_factory=list)
    attempted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    #: The stage-by-stage record (:class:`Funnel`). ``outcomes`` is the terminal
    #: slice of it and stays for the miners and readers that only want that; the
    #: funnel is what says *where* a unit died, which is the difference between a
    #: broken label removed and a hard case removed. Empty for a miner that has not
    #: declared stages — the ledger simply records no funnel for that run.
    funnel: Funnel | None = None


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
    #: default output basename (no extension); keep ``cases`` in it so case-file
    #: discovery finds it. May be resolved dynamically (a slug can be embedded).
    cases_stem: str = ""
    #: what fixes this miner's gold. Name the artifact that decides the answer
    #: ("commit provenance"), not the metric it feeds. A label established by
    #: searching the corpus with the engine under test describes only what the
    #: incumbent already reaches: a systematic blind spot is invisible to the
    #: labeler and the ranker alike, so it can never score as a miss, and the
    #: resulting number is an upper bound on itself by an unknown margin.
    gold_source: str = ""
    #: whether **no** retrieval touched the labels. ``True`` is the strong rung:
    #: membership is decided by a record outside the search stack (a commit, an
    #: edit in the tool-use trail), so the gold is what it is however the ranker
    #: behaves. ``False`` means retrieval helped assemble the pool that was
    #: judged — legitimate when the pool draws on *several* independent systems,
    #: which bounds the bias at "what no pooled system finds" instead of "what
    #: the incumbent misses", but it is a weaker claim and the reader has to know
    #: which one they are holding. Default ``False``: a miner earns the strong
    #: rung explicitly, and a new miner that forgets to think about it is
    #: described conservatively rather than flatteringly.
    retrieval_free: bool = False
    #: can ``mine all N`` drive this miner with only a count? False when it needs
    #: a mandatory argument (topic needs ``--topic``).
    runnable_in_all: bool = True

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Add miner-specific CLI arguments. The framework has already added the
        common ones (``--model``, ``--jobs``, ``--out``, ``--seed``, and
        ``--target`` for per-case miners)."""

    def stages(self, args: argparse.Namespace) -> list[Stage]:
        """The funnel this miner declares, given its resolved arguments.

        Empty by default: a miner that has not been converted runs as one opaque
        step, and a reader sees "no funnel recorded" rather than a fabricated
        single-stage one. The list is a function of ``args`` because a flag can
        remove a gate, and a run's funnel has to describe the run that happened."""
        return []

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
    parser.add_argument("--plan", action="store_true",
                        help="run the free stages only and print the funnel: what "
                             "this corpus supplies, what the cheap gates refuse, "
                             "and how many agent sessions the rest would cost. "
                             "Writes nothing and records no run")
