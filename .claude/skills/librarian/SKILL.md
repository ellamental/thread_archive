---
name: librarian
description: "Curate the conversation archive's topic graph. Review unreviewed conversation threads — add a handful (~3+) of salient message→topic citations each, creating/linking topics as needed. Triggers on: review threads, run the librarian, clear the citation backlog, index conversations, curate topics."
disable-model-invocation: true
---

# librarian

Curate the local conversation archive: review conversation threads that haven't been
curated yet, and for each, do one thing and nothing heavier:

- **Link** — add **~3+** salient message→topic citations (linking topics where the
  relationship is real). A thread is 'reviewed' — and leaves the queue — the moment it
  gains its first citation/link. (The librarian only links; it does not write per-thread
  summaries.)

Coverage of the main threads of a conversation, not exhaustive per-message tagging.

This is the serverless archive's librarian. It runs **on demand** (you invoke it); the
write surface is the `thread-archive-librarian` MCP, the read surface is the
`thread-archive` MCP. There is no `db_query` / `backend_*` here — the archive is
serverless (JSONL truth + SQLite projection), and every write below is event-sourced
(an append-only `kg_event`) and idempotent, so a partial pass is simply redone.

## Tools

| Tool | Server | Purpose |
|------|--------|---------|
| `review_queue` | librarian | Pull the unreviewed-conversation backlog |
| `thread_user_messages` | librarian | A thread's user messages as `{event_id, text}` (cheap, high-signal) |
| `thread_read` | thread-archive | Full transcript when you need more than user turns |
| `topic_search` | librarian | Find an existing topic before creating a duplicate |
| `topic_create` | librarian | Create a topic when a central concept genuinely has none |
| `topic_link` | librarian | Link two topics/threads (related / implements / contrast / …) |
| `topic_cite` | librarian | Cite a message as evidence for a topic (the review commit — first one drops the thread from the queue) |

## The queue

```
review_queue(limit=20, exclude_source_id="<your session source_id>")
```

**Skip the thread you're in.** A librarian run is itself a `claude-code` thread, and the
watcher may ingest it live — so your own session can sit at the top of its own queue.
Get your session id from `CLAUDE_CODE_SESSION_ID` (`echo -n "$CLAUDE_CODE_SESSION_ID"`)
and pass its `source_id` as `exclude_source_id`. If that variable is empty (some headless
contexts), omit the argument.

`review_queue` already skips content-less stubs (threads with no events) — it only
returns event-bearing conversations, so you never land on a thread with nothing to read.

If another librarian instance may be running, pick a thread from the page by `id % N` or
at random rather than always taking the top row — avoids two instances re-doing one thread.

## Workflow

Keep **one thread open at a time**: finish a thread (≥1 citation) before moving to the
next. For each thread `id`:

### 1. Read it (user messages first)

```
thread_user_messages(thread_id=<id>)
```

Read **Ella's user messages** — the real signal of what the thread was about, and far
cheaper than the full transcript. Note the `event_id` of each — the citations anchor on
them. Use `thread_read(thread_id=<id>)` (thread-archive MCP) only if the user turns are
too thin to identify the salient topics.

### 2. Link (~3+ citations) — this is the commit

Identify the 3+ most salient concepts the thread is actually about. For each, find an
**existing** topic first (the archive already has many):

```
topic_search(query="<concept>")
```

Then cite the representative message to that topic:

```
topic_cite(topic_id=<topic_id>, event_id=<event_id>, thread_id=<id>, quote="<short verbatim snippet>")
```

- Aim for **≥3 citations** spread across the thread's key messages.
- The **first** citation/link is the review commit: it marks the thread reviewed and drops
  it from `review_queue`. There is no separate write step.
- Citations are idempotent — re-running is safe (no double-cite). If a pass dies mid-way,
  the thread is simply redone next time (no double-work).
- Create a topic only when a central concept genuinely has none:
  `topic_create(title="<concept>", description="<one line>")`. Prefer linking.
- When two topics are genuinely related, `topic_link(source_id=A, target_id=B,
  link_type="related")` — this is what densifies the graph (and feeds the Leiden
  communities). Use `implements` / `example-of` / `contrast` / `supersedes` /
  `works_on` when the relationship is more specific than "related".

### 3. Next

Re-run `review_queue` and repeat. Stop when it returns no rows — the backlog is clear.

## Output contract

The librarian's only durable output is **citations + links** (no summaries — those were
retired, and nothing read them). For each reviewed thread:

- **~3+ citations**, each a short verbatim `quote` anchored to a real `event_id` from
  `thread_user_messages`, spread across the thread's key messages — coverage of the main
  threads of the conversation, not every message.
- **Links** between topics where the relationship is real (`related` / `implements` /
  `example-of` / `contrast` / `supersedes` / `works_on`).

## Arguments

`$ARGUMENTS` is an optional per-run cap. No argument → drain until the queue is empty. A
number → process up to that many threads, then stop.

## Model

Run on **opus** (the documented librarian exception; interactive sessions use fable).

## Stay lite (do NOT)

- No exhaustive per-message tagging — ~3+ citations per *conversation*, not per message.
- No observations or notes beyond the topic citations + links.
- No topic merges, reclassification, or description rewrites unless explicitly asked.
- No archiving, renaming, or otherwise modifying conversation threads.
- Search before creating topics.
