import { useEffect, useState } from 'react'
import { api, type Status } from '../api'

export function StatusBar() {
  const [st, setSt] = useState<Status | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.status().then(setSt).catch((e) => setErr(String(e.message ?? e)))
  }, [])

  if (err) return <div className="statusbar err">archive unavailable: {err}</div>
  if (!st) return <div className="statusbar">loading…</div>
  return (
    <div className="statusbar">
      {st.threads.toLocaleString()} threads · {st.events.toLocaleString()} events ·{' '}
      {st.fts_indexed.toLocaleString()} indexed
      {st.vectors_indexed ? ` · ${st.vectors_indexed.toLocaleString()} vectors` : ''}
    </div>
  )
}
