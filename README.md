# thread-archive

[![PyPI](https://img.shields.io/pypi/v/thread-archive)](https://pypi.org/project/thread-archive/)
[![CI](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml?query=branch%3Amain)
[![Python](https://img.shields.io/pypi/pyversions/thread-archive)](https://pypi.org/project/thread-archive/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-black)](https://github.com/ellamental/thread_archive)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/ellamental/thread_archive/blob/main/LICENSE)

A note from the human in the loop: This app is an experiment to see if vibe coding can produce something robust. If that makes you not trust the project, welcome to the club! I have over 10 years of professional dev experience, but I'll be honest, I haven't read any of the code. Nevertheless I get a lot of benefit from the functionality, and we have pretty good tests, so 🤷🏻‍♀️ (review the project yourself, or ask your AI).

**A local-first archive for the AI agents that work on your machine — built by
Claude Code, for Claude Code.** Every session your agent harnesses record lands
in one durable, append-only archive you own, on your own disk — and your agents
can search and read all of it, mid-conversation, over MCP.

**Your agent's transcripts are temporary by default.** Claude Code writes each
session to `~/.claude` as JSONL, keeps it for 30 days, and deletes it — a
sensible default for a harness's working files, a lossy one for the only record
of how the work got decided. Nothing searches them in the meantime either.

## Quickstart

```bash
pip install thread-archive
thread-archive setup
```

`setup` discovers this machine's conversation stores and shows what it found —
counts, sizes, date ranges — *before* touching anything. Whatever is still on
disk imports as a head start; from then on a background watcher keeps the
archive current and the 30-day window stops mattering. It also wires the MCP
server into your client. Every choice is skippable.

Minutes after setup, ask mid-conversation:

> *"what did we decide about the auth flow?"*

Your agent searches, reads around the hits, and comes back with what you
decided and why — a few `thread_search` calls and a couple of `thread_read`s,
the way it would work through a codebase it hadn't seen. What it reads is an
efficient representation of the conversation itself, not a summary someone made
of one. No workflow to adopt,
no notes you were supposed to be taking — from here on the record is being
written.

## What you get

- **Search, two doors, one implementation** — MCP tools (`thread_search`,
  `thread_read`) for agents, and the same verbs on the CLI
  (`thread-archive search "auth flow" --since 30d`). What you get at a prompt is
  what your agent gets.
- **Full-text and semantic search**, fused and re-ranked, filterable by time,
  source, tool, and content type; an empty query browses recent activity.
- **Indexed by code, not just by words** — ask which conversations worked on a
  file (`thread_search(path='rank.py')`), which sessions a commit is made of
  (`thread_search(commit='31bade5')`) or a pull request was built from
  (`thread_search(pr=4)`), or what a session actually changed.
- **Built like a database, not a folder of exports** — plain JSONL as the
  source of truth, crash-safe writes with intent journaling, built-in backup
  with nightly restore drills. The index is a disposable projection that
  rebuilds at any time.
- **Import drift is loud and repairable** — when a provider changes its
  transcript format, coverage checks catch it, raw files are preserved before
  the provider prunes them, and `thread-archive source fix` scaffolds the
  repair for you (or your agent) to finish.
- **No hosted backend. No cloud. No subscription to lose your history to.**
  Every process runs locally, on your machine.

Claude Code is the supported, first-class source. The other harnesses it reads
— Codex, Cursor, OpenCode, Grok, and friends — are best-effort and
community-maintainable via the plugin API. Web chats (claude.ai, ChatGPT, xAI)
import from account exports you download by hand.

## Documentation

This is the manual, and it ships inside the package: `thread-archive docs` lists
these pages and `thread-archive docs <page>` prints one, offline. From a clone,
the web viewer serves the same pages at `/docs`. The links below are those pages
on GitHub. (`docs/*.md`, one level up from them, is the other half — the release
process, the bench landscape, the maintainer's dev panels — written for whoever
works on this repo, and in no install.)

- [Install](https://github.com/ellamental/thread_archive/blob/main/docs/public/install.md) — the setup wizard, optional semantic search, updating, from-source, uninstall
- [Search and retrieval](https://github.com/ellamental/thread_archive/blob/main/docs/public/retrieval.md) — the two tools, filters, and the code index
- [CLI](https://github.com/ellamental/thread_archive/blob/main/docs/public/cli.md) — every verb, grouped by what it acts on
- [MCP](https://github.com/ellamental/thread_archive/blob/main/docs/public/mcp.md) — server modes and client wiring
- [Web viewer](https://github.com/ellamental/thread_archive/blob/main/docs/public/web-viewer.md) — the dev-only local UI, run from a clone
- [How it works](https://github.com/ellamental/thread_archive/blob/main/docs/public/architecture.md) — the event model, durability, platform assumptions, repo layout
- [Stability](https://github.com/ellamental/thread_archive/blob/main/docs/public/stability.md) — the four public interfaces and what may change
- [When an import drifts](https://github.com/ellamental/thread_archive/blob/main/docs/public/import-drift.md) — the support tier and the repair loop
- [Not supported](https://github.com/ellamental/thread_archive/blob/main/docs/public/scope.md) — the deliberate scope: one machine, one user, macOS/Linux
- [On-disk format](https://github.com/ellamental/thread_archive/blob/main/docs/public/format.md) · [Provider plugin API](https://github.com/ellamental/thread_archive/blob/main/docs/public/providers.md) · [Search quality](https://github.com/ellamental/thread_archive/blob/main/docs/public/search-quality.md) · [Related projects](https://github.com/ellamental/thread_archive/blob/main/docs/public/related.md)

## Scope

Deliberately narrow: one machine, one user, macOS and Linux only. The archive
preserves and retrieves — it never writes back to a harness store and never
sends a message. Live capture of web chats is out; account exports are the
path. The reasoning behind each line is in
[docs/public/scope.md](https://github.com/ellamental/thread_archive/blob/main/docs/public/scope.md).

## Similar and related projects

Preserving and searching AI conversation history is a crowded space, and a lot
of the work in it is good — session-search neighbors (CASS, ctx, deja-vu,
episodic-memory, synty, and more), agent memory layers (mem0, Letta, Zep), and
the prior art outside AI (notmuch). The annotated survey lives in
[docs/public/related.md](https://github.com/ellamental/thread_archive/blob/main/docs/public/related.md).
The short version of the difference: most tools treat the harness's own files
as the record and their index as a cache over it; archive treats preservation
as the product — its own append-only truth log, backup with restore drills, and
unmodeled provider fields preserved verbatim.

## License

MIT — see [LICENSE](https://github.com/ellamental/thread_archive/blob/main/LICENSE).
An install carries no third-party source: the web viewer's bundle, which is what
vendors any, is dev-only and excluded from the wheel. Its dependencies (all
MIT/ISC/BSD) are attributed in `src/thread_archive/_web/THIRD_PARTY_NOTICES.md`
in the repo (regenerate with `scripts/gen_third_party_notices.py` when frontend
dependencies change).

## Origin

thread-archive is the standalone member of a larger personal project ("thread"), built to
stand on its own — self-contained, no hosted backend or external services. It is
alpha software, and the version number says so.
