import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type PatternActivity, type PatternMatches } from '../api'
import { fmtInt } from './StatsView'

const PAGE_SIZE = 50

function fmtDate(value: string | undefined | null): string {
  if (!value) return ''
  const date = new Date(value)
  return isNaN(date.getTime())
    ? value
    : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function PatternSequence({ detail }: { detail: PatternMatches }) {
  return (
    <div className="pattern-sequence pattern-detail-sequence" aria-label="pattern sequence">
      {detail.pattern.activities.map((id, index) => {
        const item: PatternActivity = detail.vocabulary[id] ?? {
          id, kind: 'event', detail: null, label: id.replaceAll(':', ' '),
        }
        return (
          <span className="pattern-step-wrap" key={`${id}-${index}`}>
            {index > 0 && <span className="pattern-arrow">→</span>}
            <span className={`pattern-step ${item.kind}`}>{item.label}</span>
          </span>
        )
      })}
    </div>
  )
}

export function PatternDetailView() {
  const { patternId = '' } = useParams()
  const [offset, setOffset] = useState(0)
  const [detail, setDetail] = useState<PatternMatches | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setDetail(null)
    setErr(null)
    api.patternMatches(patternId, offset, PAGE_SIZE)
      .then((value) => live && setDetail(value))
      .catch((error) => live && setErr(String(error.message ?? error)))
    return () => { live = false }
  }, [patternId, offset])

  if (err) return <div className="empty">pattern unavailable: {err}</div>
  if (!detail) return <div className="empty">loading matching threads…</div>

  const start = detail.matches.length ? offset + 1 : 0
  const end = offset + detail.matches.length
  return (
    <div className="wrap">
      <div className="submeta"><Link className="session-link" to="/experiments/patterns">← patterns experiment</Link></div>
      <h1 className="title">Pattern matches</h1>
      <PatternSequence detail={detail} />
      <div className="pattern-metrics pattern-detail-metrics">
        <span><b>{fmtInt(detail.pattern.support)}</b> matching threads</span>
        <span><b>{fmtInt(detail.pattern.occurrences)}</b> occurrences</span>
        <span><b>{detail.pattern.lift.toFixed(2)}×</b> lift</span>
        <span className="badge">{detail.pattern.abstraction === 'shape' ? 'behavioral shape' : 'tool detail'}</span>
        {detail.pattern.source_concentration && <span className={`badge pattern-concentration ${detail.pattern.source_concentration}`}>{detail.pattern.source_concentration.replace('-', ' ')}</span>}
      </div>

      {detail.pattern.sources && detail.pattern.sources.length > 0 && (
        <div className="pattern-source-summary">
          <b>Where it appears</b>
          <span>{detail.pattern.sources.map((source) => `${source.source} ${Math.round(source.ratio * 100)}%`).join(' · ')}</span>
          <span>{fmtDate(detail.pattern.first_matched_at)} – {fmtDate(detail.pattern.last_matched_at)}</span>
        </div>
      )}

      {detail.status === 'not_indexed' ? (
        <div className="pattern-stale">
          Matching-thread details have not been indexed. Run <code>thread_archive patterns</code> to build them.
        </div>
      ) : (
        <>
          <div className="pattern-results-head">
            Newest first · showing {fmtInt(start)}–{fmtInt(end)} of {fmtInt(detail.total)}
          </div>
          <div className="pattern-match-list">
            {detail.matches.map((match) => (
              <Link
                className="pattern-match"
                key={match.thread_id}
                to={`/archive/${match.thread_id}?e=${match.event_id}`}
              >
                <span className="pattern-match-title">{match.title || `thread ${match.thread_id}`}</span>
                <span className="pattern-match-meta">
                  {[match.source, fmtDate(match.matched_at)].filter(Boolean).join(' · ')}
                </span>
                <span className="pattern-match-anchor">
                  events {match.event_ids.map(fmtInt).join(' → ')}
                </span>
              </Link>
            ))}
            {detail.matches.length === 0 && <div className="empty">no matching threads</div>}
          </div>
          <div className="pattern-pages">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>← Newer</button>
            <button disabled={!detail.has_more} onClick={() => setOffset(offset + PAGE_SIZE)}>Older →</button>
          </div>
        </>
      )}
    </div>
  )
}
