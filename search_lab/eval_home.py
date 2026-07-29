"""Shared plumbing for the harnesses that build a corpus home of their own.

Every calibration harness here (``beir_eval``, ``cdr_eval``, ``haystack_eval``)
and the ``haystack_corpus`` builder do the same four things before they can
measure anything: pick a home that is *not* the
operator's archive, pin the arm switches so the stack under test is the one being
claimed, name the arms it ended up with, and decide whether a cached build still
describes the corpus asked for. Each of those has exactly one right answer and
several plausible wrong ones, so they live here rather than being spelled out
per harness.

The guard is the load-bearing one. These harnesses ``rmtree`` the home they build
into, so a home that overlaps the real archive is not a wrong number, it is data
loss — and equality alone does not catch it: ``~/.thread`` is not equal to
``~/.thread/archive`` and wiping it destroys the archive anyway. :func:`guard_home`
refuses any path that overlaps a protected root in either direction, and protects
both the default home and whatever ``THREAD_ARCHIVE_HOME`` names (a session
pointed at a snapshot is pointed at something worth keeping too).

Stdlib only, and every import of the package is deferred into a function body:
callers pin ``THREAD_ARCHIVE_*`` before importing ``thread_archive`` so the store
bakes the right DSN and no model cold-loads unless asked, and a module-scope
import here would take that choice away from them.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Where built corpus homes and downloaded datasets live: ``<root>/homes/<name>``
#: for the homes, ``<root>/<dataset>`` for the downloads. One root for every
#: benchmark, so ``du`` on a single directory answers what the bench costs.
CACHE_ROOT = Path.home() / ".cache" / "thread-evals"


def state_root() -> Path:
    """Where the lab keeps what it must not lose — the run ledgers.

    Separate from :data:`CACHE_ROOT` because the two have opposite lifetimes.
    The corpora are derived: tens of GB, entirely rebuildable, and the whole tree
    is documented as safe to delete when disk gets tight. A measurement history
    is not rebuildable at all — the code and corpus that produced a row are gone
    the moment either changes — so it cannot share a directory whose stated
    contract is "delete this".

    Separate from the **archive home** for the more basic reason: nothing the
    bench measures is the archive. Every row scores a throwaway corpus built from
    a public dataset, so its history is the lab's own state, and writing it into
    the product's home would make an install's directory hold measurements of
    corpora it has never seen. It also keeps the whole quality stack readable
    without touching ``~/.thread``, which is what lets the suite stay sandboxed
    away from the operator's archive without cutting the bench off from its own
    records.

    Honors ``XDG_STATE_HOME`` — the base-dir spec's slot for exactly this
    (persistent, not portable, not precious enough for the data dir), and read at
    call time so a caller that redirects it is obeyed."""
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "thread-search-lab"


#: The archive home nothing here may build into, wipe, or overlap.
DEFAULT_ARCHIVE_HOME = Path.home() / ".thread" / "archive"


def protected_homes() -> list[Path]:
    """The archive homes a benchmark must never touch: the default one, plus
    whatever ``THREAD_ARCHIVE_HOME`` currently names.

    Both, because either can hold real data — a session pointed at a snapshot has
    ``THREAD_ARCHIVE_HOME`` set to a fixture that took hours to build, and the
    default home is the live archive whether or not the environment mentions it.
    Read at call time: the harnesses pin their own home into the environment a
    few lines later, so a value cached at import would protect the wrong path."""
    homes = [DEFAULT_ARCHIVE_HOME]
    env = os.environ.get("THREAD_ARCHIVE_HOME")
    if env:
        homes.append(Path(env))
    return [h.expanduser().resolve() for h in homes]


def guard_home(home: Path | str, *, what: str = "corpus home",
               protected: list[Path] | None = None) -> Path:
    """Resolve ``home`` and refuse it if it overlaps an archive home.

    Overlap in *either* direction is fatal: the home is a parent of a protected
    home (wiping it takes the archive with it), or it sits inside one (the build
    writes into an archive it does not own). Equality is only the narrowest case
    of the first. Returns the resolved path so a caller can use it as the checked
    value rather than re-resolving and hoping it matches.

    ``protected`` overrides what is defended, which is how this is exercised
    against directories that are not the operator's real archive."""
    path = Path(home).expanduser().resolve()
    for protected_home in (protected_homes() if protected is None else
                           [p.expanduser().resolve() for p in protected]):
        if (path == protected_home or protected_home in path.parents
                or path in protected_home.parents):
            raise SystemExit(
                f"refusing: {what} {path} overlaps the archive home "
                f"{protected_home} — this build wipes and rewrites its home. "
                f"Pass a different --home.")
    return path


def pin_arms(*, vectors: bool) -> None:
    """Pin the retrieval arms for a benchmark process.

    Call before importing ``thread_archive``. Sets the throttle off (a benchmark
    build is the foreground work of the process, not background maintenance), the
    embed switch from the requested arms, and — the one that is easy to forget —
    coherence off whenever the semantic arm is off. The community-coherence
    re-rank reads ``event_vectors``; with embeddings off that table is never
    populated, so it degrades to a swallowed exception on every conceptual query.
    A core lexical install has no coherence arm at all, so pinning it off is what
    makes "lexical" mean the same stack in every harness that reports one."""
    os.environ["THREAD_ARCHIVE_NO_THROTTLE"] = "1"
    os.environ["THREAD_ARCHIVE_EMBED"] = "on" if vectors else "off"
    if not vectors:
        os.environ["THREAD_ARCHIVE_COHERENCE"] = "off"


def arm_labels(*, vectors: bool) -> list[str]:
    """The arms a run measured, as report labels — ``lexical`` always (the FTS
    arm is never off), then whichever model arms were pinned on. Shared so the
    header line of every external report names a configuration the same way."""
    arms = ["lexical"]
    if vectors:
        arms.append("vectors")
    return arms


def home_name(base: str, *, max_docs: int | None) -> str:
    """The cache directory name for a corpus: ``<base>``, or ``<base>-max<N>`` when
    the run capped the document count.

    A capped build is a *different corpus*, so it gets a different home rather than
    overwriting the full one. Two properties fall out: a smoke run never destroys a
    corpus that took a ~15-min embed to build, and it is itself cached, so
    iterating on a harness with ``--max-docs 200`` re-ingests once instead of every
    run."""
    return base if not max_docs else f"{base}-max{max_docs}"


def marker_stale(marker: dict | None, want: dict) -> bool:
    """Whether a cached build's marker fails to describe the corpus asked for.

    A built home is expensive (a per-doc ingest, and with vectors a ~15-min embed),
    so it is cached and reused — which makes the *key* the whole safety property.
    Every field that changes what got ingested belongs in ``want``: the dataset,
    and the document cap. A smoke build under ``--max-docs 100`` that reads back
    as a full corpus is the failure this exists to stop; it does not announce
    itself, because a 100-document corpus scores perfectly plausible numbers.

    A key absent from an older marker compares as ``None``, which matches the
    ``None`` an uncapped run asks for — so tightening this rule does not
    invalidate a full build made before the key existed."""
    if not marker:
        return True
    return any(marker.get(key) != value for key, value in want.items())


def embed_corpus(home: Path | str | None = None) -> dict:
    """Embed the open corpus, then persist the vectors to the durable sidecar.
    Returns what ``api.embed`` returned.

    ``api.embed`` writes vectors into ``index.db``, which is the *disposable* half
    of an archive home — truth is the record and the index is a projection over it.
    So an index-format migration, a repair, or a ``--rebuild`` pass drops every
    vector, and on these corpora that is hours — the bench's built homes hold
    ~341K vectors, and beam embeds at a measured 160 docs/min.
    ``truth/vectors.sqlite`` is the copy that
    survives, keyed by the event ids truth fixes and tagged with the embedding
    space, so a rebuild restores the vectors instead of re-running the embed.

    A one-shot corpus build is the right place to pay for the write. The sidecar is
    rewritten whole, which is proportional right after a bulk embed and would not
    be on a live archive's incremental drain — which is why the save lives here and
    not inside ``api.embed``.

    Fail-soft: a corpus that embedded fine must still score when the cache cannot
    be written. The vectors are in the index either way; the sidecar only decides
    whether they survive losing it."""
    from thread_archive import _api as api
    from thread_archive._config import resolve_paths
    from thread_archive._retrieval.vectors import save_vectors_sidecar

    res = api.embed()
    try:
        save_vectors_sidecar(resolve_paths(str(home) if home else None).truth_dir)
    except Exception:  # noqa: BLE001 — an uncached embed beats a failed build
        pass
    return res or {}


def stamp_corpus(home: Path, *, restamp: bool = False) -> str | None:
    """Give a built benchmark corpus the same identity a mined corpus has, and
    return it.

    A benchmark home is born frozen — built from a fixed dataset, nothing appends
    to it — so ``snapshot.stamp_snapshot`` writes the manifest in place without
    copying anything. What that buys is a content fingerprint every recorded
    number can bind to: a rebuilt or re-capped corpus takes a new id, so a run
    measured against the old one reads as describing a different corpus instead of
    being silently compared across the change.

    ``restamp`` after an ingest; otherwise an existing id is returned untouched
    (the fingerprint is a scan, cheap but not free). Fail-soft — a corpus without
    an id still scores, it just cannot be told apart from its next rebuild."""
    from snapshot import read_snapshot_id, stamp_snapshot

    if not restamp:
        existing = read_snapshot_id(str(home))
        if existing:
            return existing
    try:
        return stamp_snapshot(str(home))["snapshot_id"]
    except Exception:  # noqa: BLE001 — an unidentified corpus beats a failed run
        return None


def warm(*, swapped_home: bool = False) -> None:
    """Build the corpus graph before the first scored query — see
    ``search_lab.eval_core.warm_for_scoring`` for why a scoring loop cannot leave
    that to the background build. Every harness that drives ``api.search`` in a
    loop calls this, so a benchmark number is a function of the code and the
    corpus rather than of when a build happened to land.

    ``swapped_home=True`` drops the graph cache first, for the harnesses that open
    a *series* of homes in one process. That cache is keyed by the id of the live
    engine object beside a content token; across a home swap the old engine is
    disposed and its id can be reused, so an entry from the previous corpus is
    reachable in principle. Dropping it costs one rebuild of a corpus that was
    just ingested and removes the question.

    Deferred import: the scoring core opens the package, which must not happen
    before the arms are pinned."""
    from eval_core import warm_for_scoring

    if swapped_home:
        from thread_archive._retrieval import embed_graph

        embed_graph.reset_cache()
    warm_for_scoring()
