# archive-librarian — the curation plugin

The curatorial agent for [thread-archive](../../README.md), packaged as a Claude
Code plugin. The core archive preserves and searches your conversations; this
plugin is the part that *organizes* them — install it and an agent can curate
the archive into a topic graph with cited evidence and search-boosting
summaries.

What's in the box:

- **the `/librarian` skill** — works the review queue: per conversation, ~3+
  message→topic citations plus a short search-first stored summary.
- **the `/gardener` skill** — tends the topic graph itself: merges
  near-duplicate topics, connects or retires singleton islands, grows the
  part-of hierarchy, archives uncited husks. The structural counterpart to
  the librarian; ungated (its work isn't the per-thread commit loop).
- **the librarian gate** (hooks) — enforces the librarian's one-thread-at-a-time
  loop mechanically: a thread must be cited *and* summarized before the next
  one may be opened. Sessions that never invoke `/librarian` are unaffected.
- **the librarian write MCP server** (`thread-archive-librarian`) — the
  curation write surface: topic/link/citation writes, stored summaries, and
  the reads the librarian needs (`review_queue`, `topic_search`,
  `thread_user_messages`).

## Requirements

- A thread-archive install — the plugin launches the `archive-librarian-mcp`
  console script from the archive's venv. The launcher (`bin/librarian-mcp`)
  finds it on PATH, via `$THREAD_ARCHIVE_LIBRARIAN_MCP`, or automatically in
  the clone layout.
- The read server (`archive-mcp`, serving `thread_search` / `thread_read`)
  wired into the same client — the skill reads full transcripts through it.
  `thread_archive setup` does this wiring.

## Install

From the thread-archive clone (the repo is its own plugin marketplace):

```bash
claude plugin marketplace add /path/to/thread-archive
claude plugin install archive-librarian@thread-archive
```

Then, in any session: `/librarian` drains the review queue until it's empty
(`/librarian 25` caps a run), and `/gardener` tends the topic graph
(`/gardener 30` caps its write actions).

## Scheduled self-curation (optional)

The archive core ships the headless drains — they don't need this plugin (the
drain prompts are packaged with the archive itself):

```bash
archive curate librarian          # one librarian drain now
archive curate gardener           # one gardener drain now
archive daemon install --librarian   # hourly librarian (macOS LaunchAgent)
archive daemon install --gardener    # daily gardener
```

Each scheduled fire gates on work left and skips cheaply when the queues are
empty. Requires the `claude` CLI.
