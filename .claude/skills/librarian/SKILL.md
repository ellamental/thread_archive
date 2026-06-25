---
name: librarian
description: "Curate the conversation archive's topic graph. Review unreviewed conversation threads — write each one's summary + indexed_summary and add a handful (~3+) of salient message→topic citations, creating/linking topics as needed. Triggers on: review threads, summarize unreviewed threads, run the librarian, clear the summary backlog, index conversations, curate topics."
disable-model-invocation: true
---

# librarian

Curate the local conversation archive: review conversation threads that have no
`indexed_summary` yet, and for each, do two things and nothing heavier:

1. **Summarize** — write a short `summary` and an event-anchored `indexed_summary`.
2. **Link** — add **~3+** salient message→topic citations (linking topics where the
   relationship is real).

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
| `topic_cite` | librarian | Cite a message as evidence for a topic |
| `thread_set_summary` | librarian | Write the summary + indexed_summary (the review commit) |

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

Keep **one thread open at a time**: finish a thread (≥1 citation + summary written) before
moving to the next. For each thread `id`:

### 1. Read it (user messages first)

```
thread_user_messages(thread_id=<id>)
```

Read **Ella's user messages** — the real signal of what the thread was about, and far
cheaper than the full transcript. Note the `event_id` of each — both the citations and
the `indexed_summary` anchor on them. Use `thread_read(thread_id=<id>)` (thread-archive
MCP) only if the user turns are too thin to summarize.

### 2. Link (~3+ citations)

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
- Citations are idempotent — re-running is safe (no double-cite).
- Create a topic only when a central concept genuinely has none:
  `topic_create(title="<concept>", description="<one line>")`. Prefer linking.
- When two topics are genuinely related, `topic_link(source_id=A, target_id=B,
  link_type="related")` — this is what densifies the graph (and feeds the Leiden
  communities). Use `implements` / `example-of` / `contrast` / `supersedes` /
  `works_on` when the relationship is more specific than "related".

### 3. Write last (this is the commit point)

```
thread_set_summary(thread_id=<id>, summary="<200-300 chars>", indexed_summary="<sections>")
```

Setting `indexed_summary` marks the thread reviewed and drops it from `review_queue`.
Write the summary **after** linking — if the pass dies mid-way the thread stays
unreviewed and is simply redone next time (citations are idempotent, so no double-work).

### 4. Next

Re-run `review_queue` and repeat. Stop when it returns no rows — the backlog is clear.

## Output contract

**`summary`** — a 200–300 character scannable blurb. One paragraph, no headings. What
this thread was about, at a glance.

**`indexed_summary`** — 3–10 thematic sections. Each section:

```
## Topic name (events <start>-<end>)
What happened and why it matters.
```

Roughly 1K characters of indexed_summary per 1K messages. `<start>` / `<end>` are real
`event_id` values from `thread_user_messages`. Group thematically by what the thread was
doing — not a chronological dump.

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
