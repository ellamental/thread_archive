# Search and retrieval

Two tools — `thread_search` and `thread_read` — are the whole retrieval
surface, implemented once and served three ways: over MCP for agents (see
[mcp.md](mcp.md)), as the `thread-archive search` / `thread-archive read` CLI
verbs for a terminal (see [cli.md](cli.md)), and read through the
[web viewer](web-viewer.md).

- Full-text and semantic search, fused and re-ranked, filterable by time,
  source, tool, and content type; an empty query browses recent activity.
- Exposed over MCP (`thread_search`, `thread_read`), so Claude (or any MCP
  client) can search and read your entire history mid-conversation.
- The same two tools are CLI verbs — `thread-archive search "auth flow" --since
  30d`, `thread-archive read <id>` — one implementation behind both, so what
  you get at a prompt is what your agent gets.
- Search is the access layer over the archive, not the archive itself — an
  agent typically fires several searches, reformulates, and reads around a hit,
  and the archive underneath guarantees the conversation is *there* to find.
  Quality is measured against public benchmarks somebody else labeled, read
  beside the baseline their own leaderboard publishes — a deliberate run on a
  ranking change, and a gate at release time that holds those numbers to a
  checked-in bar, but not a CI row; what rides CI is a probe that the search
  arms still load at all. No protocol that labels this archive's own corpus
  certifies that search is good, and nothing gates on one. The numbers, the
  protocol, and its limits live in [search-quality.md](search-quality.md). Your
  install reports whether search is *degraded* (`thread-archive status`, the
  viewer's health page) rather than a score — a metric with no baseline beside
  it isn't something you can act on.

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
