"""The synthetic search-quality corpus — the fast tier's fixed laboratory.

A small, fully deterministic corpus of claude-code-shaped threads with known
relevance structure: focused threads, decoys that share vocabulary with them,
a low-density log dump, a term-frequency spam paste (bm25's favourite; density
normalization's job), a contiguous-phrase thread vs a scattered one, a recency
pair whose old twin is the lexically stronger match, and code identifiers. ``CASES`` maps queries to the thread(s)
that should win. Both are checked in — no real usage data, so unlike the
mined case files this corpus belongs in the repo.

``build_corpus`` imports the corpus into the current (test-isolated) archive
home and returns the name→thread_id map; ``run_cases`` scores a search
callable against the case set with the same ``evaluate`` loop the live-archive
harness uses (MRR, recall@k), so numbers here read on the same scale as the
CI retrieval gate. ``search=`` swaps in a candidate ranker — the hook for
measuring a ranking experiment against the incumbent on identical cases.

Tests over this corpus (``test_search_quality.py``, and the opt-in
``-m quality_models`` tier) are relevance evals, not unit tests: they assert
*ordering* under the production pipeline, so a weight change that reshuffles
results shows up here in seconds, offline, before the live-archive tiers see
it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import search as _production_search
from thread_archive._store import init_db

# ── the corpus ───────────────────────────────────────────────────────────────
# name → list of (user, assistant) turns. Content is distinct per thread
# (claude-code continuation detection merges same-content sessions), and each
# thread has one clear subject so gold assignments are unambiguous.

_NOISE = (
    "svc worker heartbeat ok queue depth nominal cache warm retry budget "
    "intact gc pause short scheduler tick steady fd count flat socket pool "
    "idle dns resolve fast tls handshake ok route table stable shard map "
    "unchanged compaction quiet wal fsync ok lease renewed clock skew low "
)

THREADS: dict[str, list[tuple[str, str]]] = {
    "auth": [
        ("how does jwt authentication work in our login flow",
         "Login issues a short-lived JWT access token; the refresh token rotates on every use."),
        ("where is the token refresh handled",
         "The auth middleware refreshes the JWT when it sees an expired access token."),
        ("and on logout",
         "Logout revokes the whole refresh-token family."),
    ],
    # Decoy: an unrelated thread that mentions auth vocabulary once in passing.
    "css-decoy": [
        ("why is the sidebar overflowing on small screens",
         "The flexbox container needs min-width zero. Unrelated to authentication, "
         "though the login page happens to share this stylesheet."),
    ],
    # Low-density decoy: a long dump where several corpus terms appear once,
    # buried in noise. Focused threads must outrank it on their own queries.
    "dump": [
        ("here is the full service log, see if anything looks odd",
         _NOISE * 3 + "authentication check passed once here " + _NOISE * 2
         + "postgres briefly slow " + _NOISE * 3),
    ],
    # TF-spam decoy: "authentication" repeated dozens of times in a mid-length
    # paste. Raw bm25 rewards the term frequency; per-length density is what
    # keeps the focused auth thread on top — so this thread is what separates
    # the weighted ranker from pool order on the leaderboard.
    "auth-spam": [
        ("anything odd in this gateway access log? "
         + " ".join(f"req-{i} authentication ok latency nominal cache hit" for i in range(24)),
         "Nothing anomalous; the gateway is healthy."),
    ],
    "db-pool": [
        ("what database does get_session use",
         "get_session hands out connections from the postgres connection pool; the pool caps at ten."),
        ("what happens on pool exhaustion",
         "Exhausting the connection pool queues callers; raise the cap or close leaked sessions."),
    ],
    "db-migrate": [
        ("how do we run a database schema migration",
         "Alembic drives the schema migration: autogenerate a revision, review it, then upgrade to head."),
    ],
    "deploy": [
        ("how do i restart the daemon after an edit",
         "Run launchctl kickstart -k on the service label; the monitor brings it back in seconds."),
    ],
    "react": [
        ("why does the react component rerender constantly",
         "The useEffect hook is missing its dependency array, so every state update loops."),
    ],
    "embeddings": [
        ("how does semantic search work in the archive",
         "The embeddings arm encodes events with a local model and fuses cosine neighbours with the lexical hits."),
    ],
    "backup": [
        ("how do i run a restore drill",
         "The backup kit encrypts a bundle; restore-drill unpacks it into a scratch home and verifies it."),
    ],
    "ci-flaky": [
        ("the flaky test keeps timing out in ci",
         "Bump the pytest timeout or fix the polling deadline; the flakiness comes from a loaded runner."),
    ],
    # Contiguous phrase vs the same words scattered across sentences.
    "phrase": [
        ("add a graceful shutdown handler to the watcher",
         "The graceful shutdown handler drains the queue before exit."),
    ],
    "phrase-scatter": [
        ("notes on process lifecycle",
         "Shutdown should always be graceful. The signal handler runs first. "
         "Handler cleanup follows the shutdown notice."),
    ],
    "identifier": [
        ("refactor save_vectors_sidecar to batch its writes",
         "save_vectors_sidecar now flushes in batches of fifty rows."),
    ],
    # Recency pair: same subject and near-equal term density, months apart.
    "recency-new": [
        ("weekly metrics review for the ingest pipeline",
         "Ingest pipeline metrics this week: throughput steady, lag flat."),
    ],
    # The old twin is the lexically STRONGER match (its terms repeat, so bm25
    # prefers it); only the recency tiebreaker resolves the pair toward the
    # thread from this week.
    "recency-old": [
        ("january metrics review notes, second metrics review pass for the ingest pipeline",
         "January metrics review complete: throughput steady, lag flat."),
    ],
}

#: (query, gold thread names). Multi-gold rows score the best-ranked gold,
#: matching the live harness's thread-level protocol.
CASES: list[tuple[str, list[str]]] = [
    ("jwt authentication login flow", ["auth"]),
    ("token refresh", ["auth"]),
    ("authentication", ["auth"]),                       # decoys mention it in passing
    ("get_session", ["db-pool"]),
    ("postgres connection pool", ["db-pool"]),
    ("database schema migration", ["db-migrate"]),
    ("alembic", ["db-migrate"]),
    ("restart the daemon", ["deploy"]),
    ("launchctl kickstart", ["deploy"]),
    ("react rerender useEffect", ["react"]),
    ("semantic search embeddings", ["embeddings"]),
    ("backup restore drill", ["backup"]),
    ("flaky test timeout", ["ci-flaky"]),
    ('"graceful shutdown handler"', ["phrase"]),
    ("graceful shutdown handler", ["phrase"]),           # unquoted: the phrase bonus decides
    ("save_vectors_sidecar", ["identifier"]),
    ("migration | alembic", ["db-migrate"]),
    ("ingest pipeline metrics", ["recency-new", "recency-old"]),
    ("metrics review", ["recency-new"]),                 # the recency tiebreaker decides
    ("database", ["db-pool", "db-migrate"]),
]


def _timestamps(name: str, thread_index: int, turn: int) -> tuple[str, str]:
    """Deterministic January dates, except the recency pair: ``recency-new``
    is stamped near now so the exponential recency signal (half-life ~3 days)
    is actually alive for its case; everything else is equally cold."""
    if name == "recency-new":
        base = datetime.now(timezone.utc) - timedelta(hours=2)
    else:
        base = datetime(2026, 1, 2 + thread_index, 10, 0, tzinfo=timezone.utc)
    u = base + timedelta(minutes=2 * turn)
    a = u + timedelta(seconds=30)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return u.strftime(fmt), a.strftime(fmt)


def build_corpus(archive_home: Path) -> dict[str, str]:
    """Import the corpus into ``archive_home``; return name → thread id."""
    init_db()
    ids: dict[str, str] = {}
    for idx, (name, turns) in enumerate(THREADS.items()):
        lines = []
        for i, (user, assistant) in enumerate(turns):
            t_u, t_a = _timestamps(name, idx, i)
            lines.append({
                "type": "user", "uuid": f"u-{name}-{i}", "timestamp": t_u,
                "sessionId": f"s-{name}", "cwd": "/proj",
                "message": {"role": "user", "content": user},
            })
            lines.append({
                "type": "assistant", "uuid": f"a-{name}-{i}", "timestamp": t_a,
                "message": {"role": "assistant", "model": "claude-opus-4",
                            "content": [{"type": "text", "text": assistant}]},
            })
        path = archive_home / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n",
                        encoding="utf-8")
        ids[name] = import_session_incremental(path, f"quality:{name}").thread_id
    return ids


def load_eval_harness():
    """The live harness's scoring module (``evals/retrieval_eval.py``),
    imported by path — same MRR/recall loop for every tier."""
    mod = sys.modules.get("retrieval_eval")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "retrieval_eval",
            Path(__file__).resolve().parent.parent / "evals" / "retrieval_eval.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["retrieval_eval"] = mod
        spec.loader.exec_module(mod)
    return mod


def run_cases(name_to_id: dict[str, str], *, search=None, params=None,
              limit: int = 10, rerank=None, cases=None) -> dict:
    """Score a search callable against the case set. Default: the production
    pipeline as configured by the current process (the model arms honor the
    ``THREAD_ARCHIVE_EMBED`` / ``_RERANK`` switches, so the model-free suite
    measures the lexical stack and the ``quality_models`` tier the fused one).
    ``params`` scores the production pipeline under an alternative
    :class:`~thread_archive._retrieval.SearchParams` configuration; ``search``
    swaps in an arbitrary candidate ranker (mutually exclusive with it).
    """
    if search is not None and params is not None:
        raise ValueError("pass search= or params=, not both")
    if search is None:
        if params is not None:
            def search(query, **kw):
                return _production_search(query, params=params, **kw)
        else:
            search = _production_search
    harness = load_eval_harness()
    resolved = [{"query": q, "gold": [name_to_id[g] for g in golds], "sessions": []}
                for q, golds in (cases or CASES)]
    return harness.evaluate(
        resolved, limit=limit, rerank=rerank, content_type=None,
        exclude_content_types=None, search=search)


def top_threads(query: str, *, limit: int = 10, **kw) -> list[str]:
    """Ranked thread ids for a query — the ordering the invariant tests assert on."""
    return [h["thread_id"] for h in _production_search(query, limit=limit, **kw)]
