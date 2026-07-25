#!/usr/bin/env python3
"""Materialize a corpus grouping as topics — the input the topic gold miner needs.

``thread_archive mine topic`` starts from a **topic**: it hands a survey agent the
subject's member conversations and asks which angles are worth testing. That makes
it the one miner an eval corpus cannot run out of the box — a freshly built corpus
home has conversations and nothing else, and the topic graph an operator's archive
accumulates through curation does not exist. This writes one, from a grouping the
corpus already carries.

Two sources, deliberately different in what they assume:

- **``--groups PATH``** — an explicit ``{title: [member, ...]}`` map, members named
  by ``source_id`` or thread id. The grouping is external ground truth (for
  SWE-chat, ``swechat_corpus.py`` writes ``repo-groups.json`` — one group per
  repository), so the topics owe nothing to any model the search stack also uses.
  A repository is genuinely confound-dense in the way the miner wants: every
  session in it shares file names, module names, and domain vocabulary, so a
  query that names any of them has many near-misses and one right answer.
- **``--propose PATH``** — writes a groups-shaped file from the corpus-native
  embedding communities (``thread_archive._retrieval.embed_graph``: thread
  centroids → cosine kNN → Leiden), each entry carrying sampled user turns and a
  placeholder title, for a human or an agent to name and cull before it is fed
  back through ``--groups``. Two-step on purpose: a community is not a subject.
  On a corpus of agent sessions the partition readily clusters on harness
  boilerplate — every session opening with the same system preamble or persona
  header lands together — and a topic minted from one of those measures nothing
  but boilerplate matching. It also inherits a mild circularity the explicit
  grouping does not: those clusters come from the same embedding model the vector
  arm ranks with.

Citations are what establish membership (``topic_messages`` is the projection
``topic_members`` reads), so each member thread gets one representative message
cited — its first substantial user turn, the closest thing to "what this session
was about" that needs no model to pick.

**This does not move a snapshot's id.** ``corpus_fingerprint`` hashes per-thread
rows of ``events``; topics are threads and citations are ``kg_events``, so a
corpus stamped before this runs scores identically after, and golds already mined
against it stay valid. That is the same separation the retrieval stack keeps — the
topic graph is not a ranking input — read from the other side.

Topics are written through ``thread_librarian.write`` — the validating writer that
owns this event shape (the topic exists, the cited event exists and belongs to the
thread claimed, the title is not a duplicate) — rather than re-emitting kg events
from here. A second writer onto a truth log is a worse problem than a bench script
with a sibling dependency. Nothing about that dependency reaches the shipped
package: ``thread_archive`` still never imports the librarian, and this is a bench
script, like ``swechat_corpus.py`` needing the pyarrow dev extra.

Writes, and needs the librarian importable. Refuses the live archive unless forced::

    THREAD_ARCHIVE_HOME=<corpus> .venv/bin/python evals/corpus_topics.py \\
        --groups ~/dev/swe-chat-data/gold/repo-groups.json
    THREAD_ARCHIVE_HOME=<corpus> .venv/bin/python evals/corpus_topics.py \\
        --propose /tmp/communities.json --samples 6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text as sa_text

from thread_archive import _api as api
from thread_archive._config import default_home, resolve_paths
from thread_archive._store import use_session

try:
    from thread_librarian import write
except ImportError:  # pragma: no cover - a bench script's dependency, not the package's
    raise SystemExit(
        "corpus_topics needs thread_librarian importable — it owns the validating "
        "topic writer. Install the sibling checkout into this venv: "
        "pip install -e ../librarian")

# A cited quote is evidence, not a summary — enough to see what was cited without
# copying the message into the kg log.
QUOTE_CHARS = 300
# Below this a "user turn" is a slash command or an acknowledgement, not a
# statement of what the session is about.
MIN_TURN_CHARS = 40


def resolve_members(names: list[str]) -> dict[str, str]:
    """``{given name: thread id}`` for members named by either thread id or
    ``source_id``. Unknown names are dropped by the caller, which reports them —
    a grouping file outlives the corpus it describes."""
    with use_session() as s:
        rows = s.execute(sa_text(
            "SELECT id, source_id FROM threads WHERE thread_type = 'conversation'")).all()
    by_id = {str(i): str(i) for i, _ in rows}
    by_source = {str(sid): str(i) for i, sid in rows if sid}
    return {**by_source, **by_id}


def representative_event(thread_id: str) -> tuple[int, str] | None:
    """``(event_id, quote)`` for the message that best stands for a thread: its
    first substantial user turn, else its first indexed chunk of any kind."""
    with use_session() as s:
        row = s.execute(sa_text(
            "SELECT event_id, content FROM events_fts WHERE thread_id = :t "
            "AND content_type = 'user' AND length(content) >= :n "
            "ORDER BY event_id LIMIT 1"), {"t": thread_id, "n": MIN_TURN_CHARS}).first()
        if row is None:
            row = s.execute(sa_text(
                "SELECT event_id, content FROM events_fts WHERE thread_id = :t "
                "ORDER BY event_id LIMIT 1"), {"t": thread_id}).first()
    if row is None:
        return None
    return int(row[0]), " ".join(str(row[1]).split())[:QUOTE_CHARS]


def already_cited(topic_id: str) -> set[str]:
    """Thread ids already cited into ``topic_id``.

    ``add_topic_evidence`` folds idempotently — a repeat citation does not double
    the projection — but it still appends a kg event every time, and the log is
    append-only. Skipping here is what keeps a re-run from growing the truth log by
    one dead event per member."""
    with use_session() as s:
        return {str(r[0]) for r in s.execute(sa_text(
            "SELECT thread_id FROM topic_messages WHERE topic_id = :t"),
            {"t": topic_id}).all()}


def materialize(groups: dict[str, list[str]], *, actor: str) -> dict:
    """Create one topic per group and cite every member into it. A re-run completes
    a partial job rather than repeating it: an existing title is reused, and members
    already cited into it are skipped."""
    index = resolve_members([])
    made, cited, missing = 0, 0, []
    for title, members in groups.items():
        thread_ids = []
        for m in members:
            tid = index.get(str(m))
            (thread_ids.append(tid) if tid else missing.append(str(m)))
        if not thread_ids:
            print(f"  ! {title}: no member resolves in this corpus — skipped")
            continue
        try:
            topic = write.create_topic(
                title, f"{len(thread_ids)} conversations", actor=actor)
            topic_id = topic["topic_id"]
            made += 1
        except ValueError:  # already exists — reuse it, this is a re-run
            with use_session() as s:
                topic_id = str(s.execute(sa_text(
                    "SELECT id FROM threads WHERE thread_type = 'topic' AND title = :t"),
                    {"t": title}).scalar())
        seen = already_cited(topic_id)
        n = 0
        for tid in thread_ids:
            if tid in seen:
                continue
            rep = representative_event(tid)
            if rep is None:
                continue
            write.add_topic_evidence(topic_id, rep[0], tid, rep[1], actor=actor)
            n += 1
        cited += n
        print(f"  ✓ {title}: {n} new citation(s), {len(seen)} already there -> {topic_id}")
    return {"topics": made, "citations": cited, "unresolved": missing}


def propose_from_communities(samples: int) -> dict:
    """Groups-shaped proposal from the corpus-native embedding communities, each
    entry carrying sampled user turns so a reader can name it — or reject it as
    scaffolding — without opening the corpus."""
    from thread_archive._retrieval import embed_graph

    graph = embed_graph.build()
    if graph is None:
        raise SystemExit("no embedding graph: this corpus has no vectors "
                         "(build it with --vectors, or embed it first)")
    by: dict[int, list[str]] = {}
    for thread_id, community in graph.community.items():
        by.setdefault(community, []).append(thread_id)

    out = {}
    for community, members in sorted(by.items(), key=lambda kv: -len(kv[1])):
        turns = []
        for tid in members[:samples * 3]:
            rep = representative_event(tid)
            if rep is not None:
                turns.append(rep[1][:160])
            if len(turns) >= samples:
                break
        out[f"community-{community}"] = {
            "title": f"TODO name community {community} ({len(members)} threads)",
            "members": members,
            "sample_turns": turns,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--groups", type=Path, metavar="PATH",
                     help="JSON {title: [member, ...]} to materialize; members are "
                          "source_ids or thread ids")
    src.add_argument("--propose", type=Path, metavar="PATH",
                     help="write a groups-shaped proposal from the embedding "
                          "communities, for naming and culling before --groups")
    ap.add_argument("--samples", type=int, default=5, metavar="N",
                    help="sampled user turns per proposed community (default 5)")
    ap.add_argument("--actor", default="corpus-topics",
                    help="actor recorded on every kg event (default corpus-topics)")
    ap.add_argument("--force", action="store_true",
                    help="allow writing into the default archive home")
    args = ap.parse_args(argv)

    home = resolve_paths(None).home
    if not args.force and home.expanduser().resolve() == default_home().expanduser().resolve():
        raise SystemExit(
            f"refusing to write topics into the live archive at {home}: this mints a "
            "topic per group with no curation behind it. Point THREAD_ARCHIVE_HOME at "
            "an eval corpus, or pass --force.")
    api.open_archive()

    if args.propose:
        proposal = propose_from_communities(args.samples)
        args.propose.parent.mkdir(parents=True, exist_ok=True)
        args.propose.write_text(json.dumps(proposal, indent=1) + "\n")
        print(f"proposed {len(proposal)} community group(s) -> {args.propose}\n"
              "name each 'title', drop the ones that are only harness boilerplate, "
              "then feed {title: members} back through --groups")
        return 0

    raw = json.loads(args.groups.read_text())
    # Accept the proposal shape too, so a named proposal feeds straight back in.
    groups = {v["title"] if isinstance(v, dict) else k:
              (v["members"] if isinstance(v, dict) else v) for k, v in raw.items()}
    print(f"materializing {len(groups)} topic(s) into {home}")
    result = materialize(groups, actor=args.actor)
    print(f"done: {result['topics']} topic(s) created, {result['citations']} citation(s)"
          + (f", {len(result['unresolved'])} member(s) unresolved"
             if result["unresolved"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
