import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api, type StructuredThread } from '../api'
import { Message } from './Message'

export function ThreadView() {
  const { id } = useParams()
  const threadId = id ? parseInt(id, 10) : NaN
  const [thinking, setThinking] = useState(false)
  const [tools, setTools] = useState(true)
  const [data, setData] = useState<StructuredThread | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (isNaN(threadId)) return
    setData(null)
    setErr(null)
    api
      .thread(threadId, { thinking, tools })
      .then(setData)
      .catch((e) => setErr(String(e.message ?? e)))
  }, [threadId, thinking, tools])

  if (isNaN(threadId)) return <div className="wrap"><div className="empty">bad thread id</div></div>
  if (err) return <div className="wrap"><div className="empty">read error: {err}</div></div>
  if (!data) return <div className="wrap"><div className="empty">loading thread {threadId}…</div></div>

  return (
    <div className="wrap">
      <h1 className="title">{data.title || 'thread ' + threadId}</h1>
      <div className="submeta">
        thread {data.thread_id} · {data.source || 'unknown'}
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
        data.messages.map((m, i) => <Message key={i} message={m} />)
      )}
    </div>
  )
}
