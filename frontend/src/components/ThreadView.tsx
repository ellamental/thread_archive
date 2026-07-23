import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, type StructuredThread } from '../api'
import { Message } from './Message'
import { assignHues, hueStyle } from '../modelColor'

// The message holding the deep-linked event (?e= from a search hit). Exact
// membership wins; otherwise the last message that starts at-or-before the event —
// a hit on content the current toggles hide (e.g. thinking while thinking is off)
// still lands the reader at the right point in the conversation.
function targetMessageIndex(data: StructuredThread, eventId: number): number {
  if (isNaN(eventId)) return -1
  let nearest = -1
  for (let i = 0; i < data.messages.length; i++) {
    const ids = data.messages[i].event_ids ?? []
    if (ids.includes(eventId)) return i
    if (ids.some((id) => id <= eventId)) nearest = i
  }
  return nearest
}

// The distinct models that answered anywhere in the thread, in first-seen order —
// listed on the header line, color-coded to match their messages below.
function threadModels(data: StructuredThread): string[] {
  const seen: string[] = []
  for (const m of data.messages)
    for (const model of m.meta?.models ?? [])
      if (!seen.includes(model)) seen.push(model)
  return seen
}

// "Jan 3, 2026, 10:00 AM → 11:42 AM" — the end collapses to time-of-day when the
// thread starts and ends on the same date, which almost all do.
function fmtSpan(start?: string | null, end?: string | null): string | null {
  if (!start) return null
  const s = new Date(start)
  if (isNaN(s.getTime())) return null
  const from = s.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
  const e = end ? new Date(end) : null
  if (!e || isNaN(e.getTime()) || e.getTime() === s.getTime()) return from
  const sameDay = s.toDateString() === e.toDateString()
  const to = e.toLocaleString(
    [],
    sameDay ? { timeStyle: 'short' } : { dateStyle: 'medium', timeStyle: 'short' },
  )
  return `${from} → ${to}`
}

// The reader's local find covers everything currently rendered for a message:
// prose, tool inputs/results, hook context, and metadata-like block labels. It is
// deliberately client-side — instant navigation within an already-open thread
// must not turn into another archive search.
function messageSearchText(message: StructuredThread['messages'][number]): string {
  try {
    return JSON.stringify(message.blocks).toLocaleLowerCase()
  } catch {
    return message.blocks.map((block) => ('text' in block ? block.text : '')).join(' ').toLocaleLowerCase()
  }
}

export function ThreadView() {
  const { id } = useParams()
  const navigate = useNavigate()
  // A 26-char Crockford-base32 ULID is the archive thread PK itself; anything
  // else (a legacy integer id, or a claude-code/codex session uuid or stem
  // pasted straight into the URL) is a ref we resolve server-side and redirect
  // to its canonical ULID address.
  const isCanonical = !!id && /^[0-9a-hjkmnp-tv-z]{26}$/i.test(id)
  const threadId = isCanonical ? (id as string) : null
  const [params, setParams] = useSearchParams()
  const focusEvent = params.get('e') ? parseInt(params.get('e') as string, 10) : NaN
  const hitEvents = (params.get('hits') ?? '')
    .split(',')
    .map((value) => parseInt(value, 10))
    .filter((value) => !isNaN(value))
  const [thinking, setThinking] = useState(false)
  const [tools, setTools] = useState(true)
  const [data, setData] = useState<StructuredThread | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [findTerm, setFindTerm] = useState(params.get('q') ?? '')
  const [findPos, setFindPos] = useState(0)
  const readerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!id || isCanonical) return
    setErr(null)
    api
      .resolveLink(id)
      .then((r) => navigate('/archive/' + r.thread_id, { replace: true }))
      .catch((e) => setErr(String(e.message ?? e)))
  }, [id, isCanonical, navigate])

  useEffect(() => {
    if (!threadId) return
    setData(null)
    setErr(null)
    api
      .thread(threadId, { thinking, tools })
      .then(setData)
      .catch((e) => setErr(String(e.message ?? e)))
  }, [threadId, thinking, tools])

  const focusIdx = data ? targetMessageIndex(data, focusEvent) : -1
  const findMatches = useMemo(() => {
    const needle = findTerm.trim().toLocaleLowerCase()
    if (!data || !needle) return []
    return data.messages.flatMap((message, index) =>
      messageSearchText(message).includes(needle) ? [index] : [],
    )
  }, [data, findTerm])
  const findIdx = findMatches[findPos] ?? -1

  useEffect(() => {
    if (!data || focusIdx < 0) return
    document.getElementById('m-' + focusIdx)?.scrollIntoView({ block: 'center' })
  }, [data, focusIdx])

  useEffect(() => {
    setFindPos(0)
  }, [findTerm, threadId])

  useEffect(() => {
    if (findIdx < 0 || !findTerm.trim()) return
    document.getElementById('m-' + findIdx)?.scrollIntoView({ block: 'center' })
  }, [findIdx, findTerm])

  function moveFind(delta: number) {
    if (findMatches.length === 0) return
    setFindPos((current) => (current + delta + findMatches.length) % findMatches.length)
  }

  function moveSearchHit(delta: number) {
    if (hitEvents.length < 2) return
    const current = Math.max(0, hitEvents.indexOf(focusEvent))
    const next = (current + delta + hitEvents.length) % hitEvents.length
    const updated = new URLSearchParams(params)
    updated.set('e', String(hitEvents[next]))
    setParams(updated, { replace: true })
  }

  function setToolFolds(open: boolean) {
    readerRef.current?.querySelectorAll<HTMLDetailsElement>('details.tool').forEach((detail) => {
      detail.open = open
    })
  }

  if (err) return <div className="wrap"><div className="empty">read error: {err}</div></div>
  if (!isCanonical) return <div className="wrap"><div className="empty">resolving {id}…</div></div>
  if (!data) return <div className="wrap"><div className="empty">loading thread {threadId}…</div></div>

  const models = threadModels(data)
  const agents = data.agent_sessions ?? null
  // Assign hues over the thread's own models first (they keep their header order),
  // then any model only the subagents used — so a model shared by both reads the
  // same color on the header line and the agents line.
  const agentModels = agents?.by_model.map((b) => b.model) ?? []
  const hues = assignHues([...models, ...agentModels.filter((m) => !models.includes(m))])
  const span = fmtSpan(data.started_at, data.ended_at)
  const hitPos = hitEvents.indexOf(focusEvent)

  return (
    <div className="wrap thread-wrap" ref={readerRef}>
      <h1 className="title">{data.title || 'thread ' + threadId}</h1>
      <div className="submeta">
        thread {data.thread_id} · {data.source || 'unknown'}
        {models.length > 0 && (
          <span className="model-tags">
            {models.map((m) => (
              <span className="model-tag" key={m} style={hueStyle(hues[m])}>
                {m}
              </span>
            ))}
          </span>
        )}
      </div>
      {/* Provenance: this is an archive — say when it happened, which session it
          was, and how much of a record there is. */}
      <div className="submeta provenance">
        {span && <span>{span}</span>}
        {data.event_count != null && data.event_count > 0 && (
          <span>{data.event_count.toLocaleString()} events</span>
        )}
        {data.source_id && (
          <span className="session-id" title="provider session id">
            {data.source_id}
          </span>
        )}
      </div>
      {/* Agent sessions: the Task-tool subagents this thread spawned (their own
          transcripts, not shown inline here), and which model each ran on. */}
      {agents && agents.count > 0 && (
        <div className="submeta agent-sessions">
          <span>
            {agents.count} agent {agents.count === 1 ? 'session' : 'sessions'}
          </span>
          {agents.by_model.length > 0 && (
            <span className="model-tags">
              {agents.by_model.map(({ model, count }) => (
                <span className="model-tag" key={model} style={hueStyle(hues[model])}>
                  {model}
                  {count > 1 && <span className="agent-count"> ×{count}</span>}
                </span>
              ))}
            </span>
          )}
        </div>
      )}
      <div className="reader-toolbar" aria-label="thread reader controls">
        <div className="find-control">
          <input
            className="find-input"
            type="search"
            aria-label="find in thread"
            placeholder="find in thread…"
            value={findTerm}
            onChange={(event) => setFindTerm(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') moveFind(event.shiftKey ? -1 : 1)
            }}
          />
          <span className="find-status" aria-live="polite">
            {findTerm.trim()
              ? findMatches.length > 0
                ? `${findPos + 1} of ${findMatches.length}`
                : 'no matches'
              : ''}
          </span>
          <button
            className="toolbar-btn icon-btn"
            aria-label="previous in-thread match"
            disabled={findMatches.length === 0}
            onClick={() => moveFind(-1)}
          >
            ↑
          </button>
          <button
            className="toolbar-btn icon-btn"
            aria-label="next in-thread match"
            disabled={findMatches.length === 0}
            onClick={() => moveFind(1)}
          >
            ↓
          </button>
        </div>
        {hitEvents.length > 1 && (
          <div className="hit-nav" aria-label="search hits in this thread">
            <button className="toolbar-btn" onClick={() => moveSearchHit(-1)}>
              ← previous hit
            </button>
            <span>{Math.max(hitPos, 0) + 1} of {hitEvents.length}</span>
            <button className="toolbar-btn" onClick={() => moveSearchHit(1)}>
              next hit →
            </button>
          </div>
        )}
        <div className="reader-options">
          <label>
            <input type="checkbox" checked={thinking} onChange={(e) => setThinking(e.target.checked)} /> thinking
          </label>
          <label>
            <input type="checkbox" checked={tools} onChange={(e) => setTools(e.target.checked)} /> tools
          </label>
          <button className="toolbar-btn" onClick={() => setToolFolds(false)}>
            collapse details
          </button>
          <button className="toolbar-btn" onClick={() => setToolFolds(true)}>
            expand details
          </button>
        </div>
      </div>
      {data.messages.length === 0 ? (
        <div className="empty">(no renderable content)</div>
      ) : (
        data.messages.map((m, i) => {
          // A single assistant turn now arrives as one message per model inference
          // (a tool-call/response iteration). A run of same-role, same-model messages
          // merges into one grouped panel — only the first shows the role label — so a
          // mid-turn model switch (which breaks the run) stands out instead of hiding.
          const prev = i > 0 ? data.messages[i - 1] : undefined
          const continued =
            !!prev &&
            prev.role === m.role &&
            (m.role !== 'assistant' ||
              (m.meta?.models?.[0] ?? null) === (prev.meta?.models?.[0] ?? null))
          const firstEvent = m.event_ids?.[0]
          return (
            <Message
              key={i}
              message={m}
              hueForModel={hues}
              continued={continued}
              highlighted={i === focusIdx}
              findTarget={i === findIdx && findTerm.trim().length > 0}
              anchorId={'m-' + i}
              permalink={
                firstEvent != null ? `/archive/${data.thread_id}?e=${firstEvent}` : undefined
              }
            />
          )
        })
      )}
    </div>
  )
}
