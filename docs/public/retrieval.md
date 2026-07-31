# Search and retrieval

Two tools — `thread_search` and `thread_read` — are the whole retrieval
surface, implemented once and served three ways: over MCP for agents (see
[mcp.md](mcp.md)), as the `thread-archive search` / `thread-archive read` CLI
verbs for a terminal (see [cli.md](cli.md)), and read through the
[web viewer](web-viewer.md).

- Full-text and semantic search, fused and re-ranked, filterable by time,
  source, tool, and content type; an empty query browses recent activity.
- Exposed over MCP (`thread_search`, `thread_read`), so Claude (or any MCP
  client) can search and read your entire history mid-conversation. Their MCP
  descriptions are compact on purpose — an agent pays for them in every session
  whether or not it searches — and `thread_help('search'|'read')` serves the
  full manual to the caller that wants it.
- The same two tools are CLI verbs — `thread-archive search "auth flow" --since
  30d`, `thread-archive read <id>` — one implementation behind both, so what
  you get at a prompt is what your agent gets.
- Search is the access layer over the archive, not the archive itself — an
  agent typically fires several searches, reformulates, and reads around a hit,
  and the archive underneath guarantees the conversation is *there* to find.
  Quality is measured against public benchmarks somebody else labeled and gated
  against those numbers at release; your install reports whether search is
  *degraded* (`thread-archive status`, the viewer's health page) rather than a
  score. [search-quality.md](search-quality.md) has the numbers and what they
  license.

## Indexed by code, not just by words

Every path your agents' tools named — each
`Edit`, `Read`, `Write`, `apply_patch` header, and path-shaped shell argument, in
every provider's spelling — is folded into a structural index. It arrives as two
new scopes on the tools that already exist, because the questions are the ones
search already had shapes for — list the conversations, or search inside them:

- `thread_search(path='rank.py')` — *which conversations worked on this file*. With
  an empty query it is the list, ordered changes-before-looks, each row carrying its
  op tally and opening at the touch rather than at the session's tail; with a query
  it scopes the search to those sessions. A directory asks about a whole repo or
  module (`path='/repo', path_ops='edit,write,delete'`), a glob about a file type.
- `thread_search(commit='31bade5')` — *which conversations this commit is made of*,
  the loop back from `git blame`. Not one session: a commit carries work from several
  sittings, so it resolves to every session whose edits fall inside the commit's
  authorship window — after each of its files was last committed, up to this commit —
  ranked by how much of it they account for. The session that *ran* `git commit` is
  flagged among them rather than standing in for them, which matters most where you
  commit by hand and it is nobody.
- `thread_read(thread_id, summary='files')` — the same index backwards: *what this
  session actually changed.*

The index is a disposable projection of the event log: it backfills itself over an
existing archive and rebuilds with `reindex`.
