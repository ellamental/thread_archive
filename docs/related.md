# Similar and related projects

Preserving and searching AI conversation history is a crowded space, and a lot
of the work in it is good. If thread-archive isn't what you want, one of these
probably is.

**Session search over local agent transcripts** — the nearest neighbors, all
local-first, all reading the same harness stores:

- [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) —
  Rust, the widest provider coverage in the space and the closest retrieval
  stack to this one: BM25, local ONNX embeddings, rank fusion, and a
  cross-encoder rerank over an append-only store. Its MCP surface is
  inter-agent messaging; search is CLI and TUI.
- [ctx](https://github.com/ctxrs/ctx) — Rust, many harnesses into local SQLite,
  a read-only MCP server, session replay with windowing, and read-only SQL over
  the index. Lexical retrieval, tuned for spending few tokens.
- [deja-vu](https://github.com/vshulcz/deja-vu) — Go single binary; MCP `recall`
  and `blame` tools, optional semantic search against a local Ollama or LM Studio
  endpoint, credential redaction at index time. Its
  [format registry](https://github.com/vshulcz/deja-vu/tree/main/docs/registry)
  documents each harness's on-disk shape and quirks, and is the best public
  reference for these formats.
- [episodic-memory](https://github.com/obra/episodic-memory) — TypeScript;
  copies transcripts into its own archive so search outlives harness pruning,
  local embeddings, MCP `search` and `read`. Claude Code and Codex.
- [synty](https://github.com/superlinked/synty) — Rust; a login-time tracker
  daemon, append-only JSONL as its corpus, a rebuildable index, late-interaction
  retrieval, and emergent topic clustering. Architecturally the closest cousin.
- [agentsview](https://github.com/kenn-io/agentsview) — Go; a local SQLite
  archive with full-text and optional semantic search, a web dashboard, and
  token/cost analytics. Operator-facing rather than agent-facing.
- [Agent Sessions](https://github.com/jazzyalex/agent-sessions) — a polished
  native macOS browser across many harnesses. Reads in place: a viewer, not a
  store.
- Smaller and sharper: [ccrider](https://github.com/neilberkman/ccrider) (TUI
  plus an MCP search server), [claude-historian](https://github.com/Vvkmnn/claude-historian-mcp)
  (an MCP server that deliberately keeps no index at all),
  [threadlens](https://github.com/moinulmoin/threadlens) (a lexical index that
  is explicitly disposable), and the transcript renderers
  [claude-code-log](https://github.com/daaain/claude-code-log) and
  [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts).

**Multi-provider chat archives.** [MyChatArchive](https://github.com/1ch1n/mychatarchive)
comes at the same premise from the consumer side: ChatGPT, Claude, and Grok
exports alongside local Claude Code and Cursor sessions, in one SQLite archive
with full-text search, embeddings, and MCP.

**Agent memory layers** — [mem0](https://github.com/mem0ai/mem0),
[Zep / Graphiti](https://github.com/getzep/graphiti),
[Letta](https://github.com/letta-ai/letta),
[Cognee](https://github.com/topoteretes/cognee),
[supermemory](https://github.com/supermemoryai/supermemory),
[Basic Memory](https://github.com/basicmachines-co/basic-memory) — solve an
adjacent problem: distilling conversation into facts, entities, or a knowledge
graph small enough to sit in context. They are complements rather than
alternatives. They optimize for a short, high-signal context; an archive
optimizes for keeping everything. Most also record conversations that flow
*through* them, rather than ingesting a harness's own store after the fact.

**Prior art outside AI.** [notmuch](https://notmuchmail.org/) is the
architectural precedent: immutable mail files that are never modified, all
mutable state in an index regenerable from them at any time, proven over
decades and millions of messages. [Piler](https://www.mailpiler.org/) is the
compliance-archive analogue — immutable storage, tamper verification, retention
policy.

**How thread-archive differs.** Most tools here treat the harness's own files
as the record and their index as a cache over it. Archive treats preservation
as the product: its own append-only truth log, backup with restore drills and
integrity verification, crypto-shredding redaction that stays reversible, and
unmodeled provider fields preserved verbatim so a format change costs fidelity
instead of data. The retrieval stack and topic graph are built to be read by an
agent mid-conversation rather than browsed by a person. Where these projects
lead: broader provider coverage, platforms beyond macOS and Linux, and more
mileage.
