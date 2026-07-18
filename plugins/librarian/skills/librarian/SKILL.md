---
name: librarian
description: "Curate the conversation archive: review conversation threads that need work — add a handful (~3+) of salient message→topic citations AND store a short search-first summary for each, creating/linking topics as needed. Triggers on: review threads, run the librarian, clear the curation backlog, summarize threads, index conversations, curate topics."
disable-model-invocation: true
---

# librarian

Curate the local conversation archive: review conversation threads that still need
work, and for each, do two things and nothing heavier:

- **Link** — add **~3+** salient message→topic citations (linking topics where the
  relationship is real). Coverage of the main threads of a conversation, not
  exhaustive per-message tagging.
- **Summarize** — store a short, dense, search-first summary. The summary joins the
  default search scope (user + title + summary) as a lexical search doc and gets
  embedded for the semantic arm, so the thread becomes findable by what it was
  *about* in vocabulary its user messages may never have used.

A thread is 'done' — and leaves the queue — once it has **both** a citation/link and
a stored summary.

This is the serverless archive's librarian. It runs **on demand** (you invoke it); the
write surface is the `thread-archive-librarian` MCP, the read surface is the
`thread-archive` MCP. There is no `db_query` / `backend_*` here — the archive is
serverless (JSONL truth + SQLite projection). Graph writes are event-sourced
(append-only `kg_event`s) and summaries overwrite rather than stack, so a partial pass
is simply redone.

## Tools

| Tool | Server | Purpose |
|------|--------|---------|
| `review_queue` | librarian | Pull the backlog (threads missing citations or a summary) |
| `thread_user_messages` | librarian | A thread's user messages as `{event_id, text}` (cheap, high-signal) |
| `thread_read` | thread-archive | The conversation (`mode='chat'`) when you need what was decided/built |
| `topic_search` | librarian | Find an existing topic before creating a duplicate |
| `topic_create` | librarian | Create a topic when a central concept genuinely has none |
| `topic_link` | librarian | Link two topics/threads (related / implements / contrast / …) |
| `topic_cite` | librarian | Cite a message as evidence for a topic (commit half 1) |
| `thread_set_summary` | librarian | Store the thread's summary (commit half 2) |

## The queue

```
review_queue(limit=20, exclude_source_id="<your session source_id>")
```

**Skip the thread you're in.** A librarian run is itself a `claude-code` thread, and the
watcher may ingest it live — so your own session can sit at the top of its own queue.
Get your session id from `CLAUDE_CODE_SESSION_ID` (`echo -n "$CLAUDE_CODE_SESSION_ID"`)
and pass its `source_id` as `exclude_source_id`. If that variable is empty (some headless
contexts), omit the argument.

`review_queue` already skips content-less stubs and holds back any thread that ingested
events within the last hour (a live session's citations would be premature and its
summary stale on arrival).

If another librarian instance may be running, pick a thread from the page by `id % N` or
at random rather than always taking the top row — avoids two instances re-doing one thread.

## Workflow

Keep **one thread open at a time**: finish a thread (≥1 citation + a stored summary)
before moving to the next — the gate hook enforces this. For each thread `id`:

### 1. Read it (user messages first)

```
thread_user_messages(thread_id=<id>)
```

Read **Ella's user messages** — the real signal of what the thread was about, and far
cheaper than the full transcript. Note the `event_id` of each — the citations anchor on
them. When the user turns don't carry what the thread *concluded, decided, or built*
(you need that for the summary), pull the conversation:

```
thread_read(thread_id=<id>, mode='chat')
```

`chat` mode is user turns + assistant text with tool noise stripped; page with the
CHUNKED footer's offset if the thread is long.

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
- Citations are idempotent — re-running is safe (no double-cite). If a pass dies mid-way,
  the thread is simply redone next time (no double-work).
- Create a topic only when a central concept genuinely has none:
  `topic_create(title="<concept>", description="<one line>")`. Prefer linking.
- When two topics are genuinely related, `topic_link(source_id=A, target_id=B,
  link_type="related")` — this is what densifies the graph (and feeds the Leiden
  communities). Use `implements` / `example-of` / `contrast` / `supersedes` /
  `works_on` when the relationship is more specific than "related".

### 3. Summarize — the other half of the commit

```
thread_set_summary(thread_id=<id>, summary="<2–5 dense sentences>")
```

The short summary is for **search first, reading second**. Contract:

- **2–5 sentences, specific and dense.** Name the actual systems, files, features,
  errors, decisions, and outcomes — the distinctive vocabulary someone would type into
  search. Generic paraphrase ("a discussion about improving the codebase") is worthless
  to retrieval.
- **State substance, not framing.** Not "Ella asked about X and Claude explained" —
  just the X and what came of it. Noun-phrase openings work well:
  *"A /debrief skill invocation in the dom project — the sciencing step that …"*
- **Cover the whole thread**, including where it ended up (shipped / decided /
  abandoned), not just how it opened.
- Write what the thread *establishes*, in past tense, as fact.

**Long or multi-part threads** (roughly: the read chunked, or clearly >1 distinct piece
of work) also get an `indexed_summary` — structured markdown, one section per part,
each heading anchored to a real event id from the transcript:

```
thread_set_summary(thread_id=<id>, indexed_summary="## <section title> (event <event_id>)\n<what this part covers>\n\n## …")
```

Both fields can be set in one call. Short threads: short summary only. A passed field
**overwrites** — re-summarizing is an update, never a stack. If the thread already has
a summary (it's in the queue for missing citations), leave the summary alone unless
it's clearly wrong or stale.

### 4. Next

Re-run `review_queue` and repeat. Stop when it returns no rows — the backlog is clear.

## Output contract

The librarian's durable output per thread is **citations + links + a stored summary**:

- **~3+ citations**, each a short verbatim `quote` anchored to a real `event_id` from
  `thread_user_messages`, spread across the thread's key messages — coverage of the main
  threads of the conversation, not every message.
- **Links** between topics where the relationship is real (`related` / `implements` /
  `example-of` / `contrast` / `supersedes` / `works_on`).
- **A stored summary** meeting the contract in step 3 (short always; indexed for
  long/multi-part threads).

## Arguments

`$ARGUMENTS` is an optional per-run cap. No argument → drain until the queue is empty. A
number → process up to that many threads, then stop.

## Model

Run on **opus** (the documented librarian exception; interactive sessions use fable).

## Stay lite (do NOT)

- No exhaustive per-message tagging — ~3+ citations per *conversation*, not per message.
- No observations or notes beyond the topic citations + links + the stored summary.
- No topic merges, reclassification, or description rewrites unless explicitly asked.
- No archiving, renaming, or otherwise modifying conversation threads (the summary
  fields are the one sanctioned write).
- No quoting sensitive content at length in summaries — a distillation, not an excerpt
  reel.
- Search before creating topics.
