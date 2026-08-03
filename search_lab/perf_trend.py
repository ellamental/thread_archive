#!/usr/bin/env python3
"""perf_trend — the bench pass's wall-clock cost, calibrated for foreign hardware.

    python -m search_lab perf --json perf-trend.json

One JSON blob per bench pass: every row's recorded ``elapsed_s`` from the run
ledger, two synthetic calibrator timings, and the machine facts a reader needs
to compare across runs. The bench workflow uploads it as an artifact on every
CI pass, which turns timings that already exist (the ledger records them; the
logs show them) into a series someone can put a trend line through.

**This is a measurement lane, not a gate — deliberately.** Hosted CI runners
vary in hardware run to run, so a raw wall-clock band tight enough to catch a
real slowdown would flake on a slow machine draw, and one wide enough never to
flake would only catch catastrophe. Two things have to be true before a gate is
worth having: the cross-run variance is *known* (this artifact, accumulated, is
how it becomes known), and the machine-speed component is *removed* (the
calibrators). Until both hold, a verdict here would be a verdict about the
runner pool. ``ci.toml`` states the same stance for the box-local suite:
latency against the live archive is watched, not gated.

**The calibrators are hardware probes, and they must not be the code under
test.** Normalizing a row by a reference *benchmark* row would cancel a real
regression — the same slow code slows both sides of the ratio. So each
calibrator is a fixed synthetic workload built from parts the ranking code
cannot touch, one per arm, matched to what that arm is actually bound on:

* ``matvec`` — a pinned-shape float32 ``corpus @ query`` product through
  numpy's BLAS, the shape of the semantic arm's scoring pass. Tracks the
  SIMD/memory-bandwidth capability of the machine.
* ``fts`` — a pinned synthetic corpus in an in-memory SQLite FTS5 table,
  queried with a fixed term set, the shape of the lexical arm's scan. Tracks
  single-thread and B-tree/tokenizer throughput.

The two are separate because machines do not scale uniformly: measured across
GH runners, a machine can be proportionally faster at matvec than at FTS, so
one scalar would leave arm-shaped noise in every normalized row. Each round is
timed whole and the *minimum* over rounds is the calibration value — the
minimum is the least-contended estimate of what the hardware can do, and
contention is exactly what a calibrator must not absorb.

``normalized`` divides each row's seconds by its arm's calibration value:
dimensionless "calibrator units" that are comparable across machines to the
extent the calibrator resembles the row's bottleneck. Both raw and normalized
travel in the blob — the raw number is the honest record, the normalized one
is the comparable one, and which band (if any) each can carry is a decision
for when the series exists.

Reads the ledger as it stands. In CI that is exactly one gate pass (the state
root is not cached between runs); on a dev box it is whatever history the box
has, most recent run per row.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from random import Random
from time import perf_counter
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_runs  # noqa: E402
import run_meta  # noqa: E402

#: Rounds per calibrator. The value is the minimum over rounds, so rounds buy
#: robustness against a noisy neighbor mid-round; five is enough for the min to
#: stabilize while keeping the whole calibration pass under ~10 s.
ROUNDS = 5

#: The semantic arm's probe: score a batch of queries against a corpus matrix,
#: repeated. (16384 docs × 768 dims, 32 queries, 40 reps) ≈ 50 MB resident and
#: about a second a round through any BLAS — big enough to leave cache and
#: exercise memory bandwidth, small enough to run five rounds without moving
#: the workflow's runtime.
MATVEC_DOCS = 16384
MATVEC_DIMS = 768
MATVEC_QUERIES = 32
MATVEC_REPS = 40

#: The lexical arm's probe: a synthetic FTS5 corpus and a fixed query rotation.
#: Deterministic by construction (seeded PRNG over a closed vocabulary), so
#: every machine builds and scans byte-identical work.
FTS_DOCS = 3000
FTS_WORDS_PER_DOC = 60
FTS_VOCAB = 400
FTS_QUERY_TERMS = 24
FTS_REPS = 30

#: Which calibrator normalizes which row, keyed by the arm tag every manifest
#: row name carries ("beam:100K[vectors]", "perltqa[lexical]~1200"). A row
#: without a recognized tag gets no normalized value rather than a wrong one.
ARM_CALIBRATORS = {"vectors": "matvec", "lexical": "fts"}


def arm_of(row_name: str) -> Optional[str]:
    """The arm a manifest row's name declares, or None when it declares none."""
    for arm in ARM_CALIBRATORS:
        if f"[{arm}]" in row_name:
            return arm
    return None


# ── the calibrators ──────────────────────────────────────────────────────────


def calibrate_matvec(*, rounds: int = ROUNDS, reps: int = MATVEC_REPS) -> dict[str, Any]:
    """Seconds for one round of the pinned matrix–vector workload (min over rounds)."""
    import numpy as np

    rng = np.random.default_rng(0)
    corpus = rng.standard_normal((MATVEC_DOCS, MATVEC_DIMS), dtype=np.float32)
    queries = rng.standard_normal((MATVEC_DIMS, MATVEC_QUERIES), dtype=np.float32)
    corpus @ queries  # untimed warmup: first call pays BLAS thread spin-up
    times = []
    for _ in range(rounds):
        start = perf_counter()
        for _ in range(reps):
            scores = corpus @ queries
        times.append(perf_counter() - start)
    del scores
    return {"seconds": round(min(times), 4), "rounds": [round(t, 4) for t in times]}


def _synthetic_docs() -> list[str]:
    """The fixed lexical corpus: seeded draws over a closed vocabulary, so the
    workload is byte-identical on every machine that builds it."""
    rng = Random(0)
    vocab = [f"w{i:03d}" for i in range(FTS_VOCAB)]
    return [" ".join(rng.choice(vocab) for _ in range(FTS_WORDS_PER_DOC))
            for _ in range(FTS_DOCS)]


def calibrate_fts(*, rounds: int = ROUNDS, reps: int = FTS_REPS) -> dict[str, Any]:
    """Seconds for one round of the pinned FTS5 workload (min over rounds).

    FTS5 is a hard assumption, not a feature probe: the archive's own index is
    FTS5, so any machine that can run the bench has it."""
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
        db.executemany("INSERT INTO docs(body) VALUES (?)",
                       [(d,) for d in _synthetic_docs()])
        db.commit()
        terms = [f"w{i:03d}" for i in range(0, FTS_VOCAB, FTS_VOCAB // FTS_QUERY_TERMS)]
        times = []
        for _ in range(rounds):
            start = perf_counter()
            for _ in range(reps):
                for term in terms:
                    db.execute(
                        "SELECT rowid, rank FROM docs WHERE docs MATCH ? "
                        "ORDER BY rank LIMIT 10", (term,),
                    ).fetchall()
            times.append(perf_counter() - start)
    finally:
        db.close()
    return {"seconds": round(min(times), 4), "rounds": [round(t, 4) for t in times]}


# ── machine facts ────────────────────────────────────────────────────────────


def _cpu_model() -> Optional[str]:
    """The CPU's marketing name, best-effort — the fact that explains most of the
    run-to-run spread on hosted runners, so worth a platform-specific reach."""
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        for line in cpuinfo.splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except OSError:
            pass
    return platform.processor() or None


def environment() -> dict[str, Any]:
    return {
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }


# ── the blob ─────────────────────────────────────────────────────────────────


def latest_rows(home: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """The most recent successful run per row, straight off the ledger."""
    rows: dict[str, dict[str, Any]] = {}
    for record in bench_runs.read_runs(home):  # newest first
        name = record.get("row")
        if not name or name in rows or record.get("status") != "ok":
            continue
        if not isinstance(record.get("elapsed_s"), (int, float)):
            continue
        rows[name] = {
            "elapsed_s": record["elapsed_s"],
            "arm": arm_of(name),
            "at": record.get("at"),
            "code_id": record.get("code_id"),
            "corpus_id": record.get("corpus_id"),
        }
    return rows


def emit(*, home: Optional[Path] = None, rounds: int = ROUNDS) -> dict[str, Any]:
    """The whole blob: rows, calibration, normalization, machine facts."""
    calibration = {
        "matvec": calibrate_matvec(rounds=rounds),
        "fts": calibrate_fts(rounds=rounds),
    }
    rows = latest_rows(home)
    normalized = {}
    for name, row in rows.items():
        probe = ARM_CALIBRATORS.get(row["arm"] or "")
        if probe and calibration[probe]["seconds"] > 0:
            normalized[name] = round(row["elapsed_s"] / calibration[probe]["seconds"], 1)
    return {
        "kind": "perf-trend",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": run_meta.git_commit(),
        "env": environment(),
        "calibration": calibration,
        "rows": rows,
        "normalized": normalized,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", type=Path, default=None, metavar="FILE",
                    help="write the blob here as well as printing it")
    ap.add_argument("--rounds", type=int, default=ROUNDS,
                    help="calibrator rounds (the value is the min over them)")
    args = ap.parse_args(argv)

    blob = emit(rounds=args.rounds)
    rendered = json.dumps(blob, indent=1) + "\n"
    print(rendered, end="")
    if not blob["rows"]:
        print("(no recorded bench runs on this box — calibration only)",
              file=sys.stderr)
    if args.json:
        args.json.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
