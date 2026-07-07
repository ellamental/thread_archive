import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, type StructuredThread } from '../api'
import { Message } from './Message'
import { assignHues, hueStyle } from '../modelColor'

// The distinct models that answered anywhere in the thread, in first-seen order —
// listed on the header line, color-coded to match their messages below.
function threadModels(data: StructuredThread): string[] {
  const seen: string[] = []
  for (const m of data.messages)
    for (const model of m.meta?.models ?? [])
      if (!seen.includes(model)) seen.push(model)
  return seen
}

export function ThreadView() {
  const { id } = useParams()
  const navigate = useNavigate()
  // A purely numeric id is an archive thread PK; anything else (a cloth/claude-code/
  // codex session uuid or stem pasted straight into the URL) is a provider link id we
  // resolve to its numeric thread and redirect to. Parsing the uuid as an int would
  // silently truncate "27056da6-…" to 27056 and open the wrong thread.
  const isNumeric = !!id && /^\d+$/.test(id)
  const threadId = isNumeric ? parseInt(id as string, 10) : NaN
  const [thinking, setThinking] = useState(false)
  const [tools, setTools] = useState(true)
  const [data, setData] = useState<StructuredThread | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!id || isNumeric) return
    setErr(null)
    api
      .resolveLink(id)
      .then((r) => navigate('/archive/' + r.thread_id, { replace: true }))
      .catch((e) => setErr(String(e.message ?? e)))
  }, [id, isNumeric, navigate])

  useEffect(() => {
    if (isNaN(threadId)) return
    setData(null)
    setErr(null)
    api
      .thread(threadId, { thinking, tools })
      .then(setData)
      .catch((e) => setErr(String(e.message ?? e)))
  }, [threadId, thinking, tools])

  if (err) return <div className="wrap"><div className="empty">read error: {err}</div></div>
  if (!isNumeric) return <div className="wrap"><div className="empty">resolving {id}…</div></div>
  if (!data) return <div className="wrap"><div className="empty">loading thread {threadId}…</div></div>

  const models = threadModels(data)
  const hues = assignHues(models)

  return (
    <div className="wrap">
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
      <div className="toggles">
        <label>
          <input type="checkbox" checked={thinking} onChange={(e) => setThinking(e.target.checked)} /> thinking
        </label>
        <label>
          <input type="checkbox" checked={tools} onChange={(e) => setTools(e.target.checked)} /> tools
        </label>
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
          return <Message key={i} message={m} hueForModel={hues} continued={continued} />
        })
      )}
    </div>
  )
}
