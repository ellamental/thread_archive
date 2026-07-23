import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type ThreadListItem } from '../api'
import { SearchBox } from './SearchBox'

const RECENT_LIMIT = 18

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  return isNaN(date.getTime())
    ? ''
    : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function dayGroup(iso: string | null): 'Today' | 'Yesterday' | 'Earlier' {
  if (!iso) return 'Earlier'
  const date = new Date(iso)
  if (isNaN(date.getTime())) return 'Earlier'
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate())
  const difference = Math.round((today.getTime() - day.getTime()) / 86_400_000)
  if (difference <= 0) return 'Today'
  if (difference === 1) return 'Yesterday'
  return 'Earlier'
}

export function Landing() {
  const [threads, setThreads] = useState<ThreadListItem[] | null>(null)
  const [err, setErr] = useState(false)

  useEffect(() => {
    let live = true
    api
      .threads({ limit: RECENT_LIMIT })
      .then((items) => live && setThreads(items))
      .catch(() => live && setErr(true))
    return () => {
      live = false
    }
  }, [])

  const groups = useMemo(() => {
    const ordered: { label: ReturnType<typeof dayGroup>; threads: ThreadListItem[] }[] = []
    for (const thread of threads ?? []) {
      const label = dayGroup(thread.updated_at)
      let group = ordered.find((candidate) => candidate.label === label)
      if (!group) {
        group = { label, threads: [] }
        ordered.push(group)
      }
      group.threads.push(thread)
    }
    return ordered
  }, [threads])

  return (
    <div className="home">
      <section className="home-hero">
        <p className="eyebrow">Your preserved conversations</p>
        <h1>Find the conversation you remember.</h1>
        <p className="home-lede">
          Search across every harness, provider, and year without leaving your machine.
        </p>
        <SearchBox variant="hero" primary />
        <p className="search-hint">
          Press <kbd>/</kbd> anywhere to search.
        </p>
      </section>

      <section className="home-recents" aria-labelledby="recent-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Pick up where you left off</p>
            <h2 id="recent-heading">Recent conversations</h2>
          </div>
          <Link className="section-link" to="/threads">Browse all →</Link>
        </div>
        {threads === null && !err && <div className="home-loading">Loading recent conversations…</div>}
        {err && <div className="home-loading">Recent conversations are unavailable.</div>}
        {threads?.length === 0 && <div className="home-loading">No conversations yet.</div>}
        {groups.map((group) => (
          <div className="recent-group" key={group.label}>
            <h3>{group.label}</h3>
            <div className="recent-grid">
              {group.threads.map((thread) => (
                <Link className="recent-card" key={thread.id} to={'/archive/' + thread.id}>
                  <span className="recent-title">{thread.title || 'Untitled conversation'}</span>
                  {thread.first_user_message && (
                    <span className="recent-preview">{thread.first_user_message}</span>
                  )}
                  <span className="recent-meta">
                    {[thread.source, fmtDate(thread.updated_at)].filter(Boolean).join(' · ')}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        ))}
      </section>
    </div>
  )
}
