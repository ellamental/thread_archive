"""Reality-integrity incident harness — recorded search failures as permanent guards.

The principle this productizes: when the operator is confident a conversation
exists and search can't find it, the correct defaults are (1) the phrasing is
off — try variations — or (2) the search is broken or not indexing it; **not**
"you're misremembering." A false "not found" against a high-confidence memory
is the archive telling its operator that part of their own life didn't happen.
The day search fails you, record the incident in a catalogue and it becomes a
regression guard the archive must pass forever.

A **catalogue** is a JSONL file of incidents. It is user data, not product
data: it quotes real queries and real thread ids, so it lives wherever the
operator keeps private things and is pointed at by path (``archive incidents
<catalogue>``). The product ships the harness — tiers, pass conditions, runner
— and a documented format (``docs/incidents.md``); the cases stay yours.

One incident per line::

    {"slug": "doc_ock", "tier": "recall", "query": "doc ock",
     "must_surface": [3704990, 3706568],
     "confidence": "certain — 'IK I HAD THESE CONVERSATIONS'",
     "original_failure": "OR-split flood buried the real thread"}

Tiers, by what each can honestly assert:

- ``recall`` — a lost-conversation bug that was fixed. The original failing
  query must surface at least one ``must_surface`` thread in the top ``limit``
  hits. The bar is deliberately low: not "rank it first", just "the agent can
  see it at all" — each guarded case was once answered with "not found"
  against a conversation that was right there.
- ``capability`` — a whole content-type was invisible (e.g. tool-call events
  unindexed). The query must return *any* hits; ``must_surface`` is optional
  and informational, because pinning one old thread's rank is flaky under
  recency weighting. The real cliff is zero → many.
- ``record`` — the incident's own record. Exactly one ``must_surface`` id; the
  phrase must hit searched *thread-scoped* — "is this still indexed", not
  "does it win global ranking". A reality-integrity system must never lose the
  record of its own failures.
- ``unresolved`` — a sought memory never yet located. Guarded exactly like
  ``record`` (its record must stay findable) but reported as *open* so it
  can't be silently forgotten: it graduates to ``recall`` the day the memory
  is found, or leaves the catalogue if ruled never-said.

Annotation fields (``confidence``, ``original_failure``, ``notes``,
``thread``) ride into failure output so a breach fails loud with the human
stakes attached, not just a thread id. Unknown keys are rejected at load — a
typoed field must fail loud, not silently weaken a guard.

Evaluation issues no writes. The search under test is whatever callable the
caller injects; :func:`archive_search` builds the production pipeline —
exactly what an agent's ``thread_search`` gets, minus nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

TIERS = ("recall", "capability", "record", "unresolved")

# Generous: the guarantee is "surfaces at all, not buried under a flood", not
# "ranks #1". A reality-integrity miss is the agent never seeing it.
RECALL_LIMIT = 25

# Optional human-stakes context, echoed when a guard breaches. ``thread`` is
# provenance: the id of the conversation where the incident itself happened.
ANNOTATION_KEYS = ("confidence", "original_failure", "notes", "thread")

_REQUIRED = ("slug", "tier", "query")
_KNOWN = set(_REQUIRED) | set(ANNOTATION_KEYS) | {"must_surface", "content_type"}

# The evaluation seam: the production search narrowed to what a guard needs.
SearchFn = Callable[..., list]


class CatalogueError(ValueError):
    """A catalogue that cannot be trusted to guard anything (refused whole)."""


@dataclass(frozen=True)
class Incident:
    slug: str
    tier: str
    query: str
    must_surface: frozenset[int] = frozenset()
    content_type: Optional[str] = None
    annotations: dict = field(default_factory=dict)


def _parse_incident(obj: object, where: str) -> Incident:
    if not isinstance(obj, dict):
        raise CatalogueError(f"{where}: incident must be a JSON object, got {type(obj).__name__}")
    unknown = set(obj) - _KNOWN
    if unknown:
        raise CatalogueError(f"{where}: unknown field(s) {sorted(unknown)} (known: {sorted(_KNOWN)})")
    for key in _REQUIRED:
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            raise CatalogueError(f"{where}: {key!r} must be a non-empty string")
    tier = obj["tier"]
    if tier not in TIERS:
        raise CatalogueError(f"{where}: tier {tier!r} not one of {TIERS}")

    raw_ids = obj.get("must_surface", [])
    if not isinstance(raw_ids, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in raw_ids):
        raise CatalogueError(f"{where}: 'must_surface' must be a list of integer thread ids")
    if tier in ("record", "unresolved") and len(raw_ids) != 1:
        raise CatalogueError(
            f"{where}: a {tier} incident guards exactly one thread (its own record); "
            f"got must_surface={raw_ids}"
        )
    if tier == "recall" and not raw_ids:
        raise CatalogueError(f"{where}: a recall incident needs at least one 'must_surface' thread id")

    content_type = obj.get("content_type")
    if content_type is not None and (not isinstance(content_type, str) or not content_type.strip()):
        raise CatalogueError(f"{where}: 'content_type' must be a non-empty string or absent")

    return Incident(
        slug=obj["slug"].strip(),
        tier=tier,
        query=obj["query"],
        must_surface=frozenset(raw_ids),
        content_type=content_type,
        annotations={k: obj[k] for k in ANNOTATION_KEYS if k in obj},
    )


def load_catalogue(path: str | Path) -> list[Incident]:
    """Parse + validate a JSONL incident catalogue; any bad line refuses the file.

    Blank lines are allowed (trailing newlines, visual grouping); everything
    else must be a valid incident object. Slugs must be unique — two guards
    answering to one name means one of them can silently stop existing.
    """
    path = Path(path)
    incidents: list[Incident] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name}:{lineno}"
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CatalogueError(f"{where}: not valid JSON ({exc})") from exc
        case = _parse_incident(obj, where)
        if case.slug in seen:
            raise CatalogueError(f"{where}: duplicate slug {case.slug!r}")
        seen.add(case.slug)
        incidents.append(case)
    if not incidents:
        raise CatalogueError(f"{path}: catalogue holds no incidents")
    return incidents


def archive_search(home: Optional[str] = None) -> SearchFn:
    """The production search pipeline, narrowed to the evaluation seam.

    Whatever arms the install provides (lexical / semantic / rerank) run
    exactly as an agent's ``thread_search`` gets them.
    """
    from .. import _api

    def _search(query: str, *, content_type: Optional[str] = None,
                thread_id: Optional[int] = None, limit: int = RECALL_LIMIT) -> list:
        return _api.search(
            query,
            home=home,
            limit=limit,
            thread_id=thread_id,
            content_types=[content_type] if content_type else None,
        )

    return _search


def evaluate(case: Incident, search: SearchFn, *, limit: int = RECALL_LIMIT) -> tuple[bool, str, list]:
    """Run one incident's guard against a ``search`` callable.

    Returns ``(ok, detail, hits)`` — the single source of truth for the
    per-tier pass condition (the docstring tier table, in code).
    """
    if case.tier in ("recall", "capability"):
        hits = search(case.query, content_type=case.content_type, thread_id=None, limit=limit)
        found = {h["thread_id"] for h in hits}
        if case.tier == "recall":
            ok = bool(found & case.must_surface)
            detail = f"must_surface(any of)={sorted(case.must_surface)} got={sorted(found)}"
        else:
            ok = bool(hits)
            detail = (f"hits={len(hits)} "
                      f"incident_threads_surfaced={sorted(found & case.must_surface)}")
        return ok, detail, hits
    # record / unresolved: the incident's own thread stays findable, thread-scoped.
    (thread_id,) = case.must_surface
    hits = search(case.query, content_type=case.content_type, thread_id=thread_id, limit=limit)
    return bool(hits), f"thread_scoped={thread_id} hits={len(hits)}", hits


def run_catalogue(incidents: list[Incident], search: SearchFn, *, limit: int = RECALL_LIMIT) -> dict:
    """Evaluate every incident; returns a summary the CLI (or a test) renders.

    ``ok`` is the run verdict: every guard held. An ``unresolved`` case counts
    toward ``open`` whatever its guard did — open items are standing work, and
    the point of reporting them is that they can't be silently forgotten.
    """
    results = []
    for case in incidents:
        ok, detail, _hits = evaluate(case, search, limit=limit)
        results.append({
            "slug": case.slug,
            "tier": case.tier,
            "ok": ok,
            "detail": detail,
            "annotations": dict(case.annotations),
        })
    return {
        "ok": all(r["ok"] for r in results),
        "passed": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "open": sum(1 for r in results if r["tier"] == "unresolved"),
        "results": results,
    }
