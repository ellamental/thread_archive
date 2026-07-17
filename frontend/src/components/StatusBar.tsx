import { useEffect, useState } from 'react'
import { api, type Status } from '../api'

// The status bar only shows archive counts, so a blip — a watcher restart cycles
// the cohosted server for a second — must not latch a permanent "unavailable"
// banner. It polls instead of fetching once: a good survey settles into a slow
// refresh, a failure retries soon and self-heals, and the last good counts stay on
// screen through a transient failure rather than flipping to red.
export const REFRESH_MS = 60_000
export const RETRY_MS = 3_000

export function StatusBar() {
  const [st, setSt] = useState<Status | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      try {
        const s = await api.status()
        if (!live) return
        setSt(s)
        setErr(null)
        timer = setTimeout(tick, REFRESH_MS)
      } catch (e) {
        if (!live) return
        setErr(e instanceof Error ? e.message : String(e))
        timer = setTimeout(tick, RETRY_MS)
      }
    }
    tick()
    return () => {
      live = false
      if (timer) clearTimeout(timer)
    }
  }, [])

  // A prior good survey wins over a later failure: counts stay put through a blip.
  if (st)
    return (
      <div className="statusbar">
        {st.threads.toLocaleString()} threads · {st.events.toLocaleString()} events ·{' '}
        {st.fts_indexed.toLocaleString()} indexed
        {st.vectors_indexed ? ` · ${st.vectors_indexed.toLocaleString()} vectors` : ''}
      </div>
    )
  if (err) return <div className="statusbar err">archive unavailable: {err}</div>
  return <div className="statusbar">loading…</div>
}
