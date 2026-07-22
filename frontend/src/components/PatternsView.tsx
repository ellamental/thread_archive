import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api,
  type MinedPattern,
  type PatternActivity,
  type PatternReport,
} from '../api'
import { fmtInt, Tile } from './StatsView'

type Sort = 'interestingness' | 'support' | 'lift'
type Lens = 'all' | 'shape' | 'detail'
const STALE_NOTICE_AGE_MS = 24 * 60 * 60 * 1000

function fmtDate(value: string | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  return isNaN(date.getTime())
    ? value
    : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function fmtDay(value: string | undefined | null): string {
  if (!value) return ''
  const date = new Date(value)
  return isNaN(date.getTime()) ? value.slice(0, 10) : date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
}

function provenance(pattern: MinedPattern): string | null {
  if (!pattern.dominant_source) return null
  const source = pattern.dominant_source
  const ratio = Math.round((pattern.dominant_source_ratio ?? 0) * 100)
  const sourceText = pattern.source_count === 1 ? `${source} only` : `${ratio}% ${source} · ${pattern.source_count} sources`
  const first = fmtDay(pattern.first_matched_at)
  const last = fmtDay(pattern.last_matched_at)
  const dates = first && last ? (first === last ? first : `${first}–${last}`) : ''
  return [sourceText, dates].filter(Boolean).join(' · ')
}

function isOlderThanStaleNoticeAge(value: string | undefined): boolean {
  if (!value) return false
  const generatedAt = new Date(value).getTime()
  return !isNaN(generatedAt) && Date.now() - generatedAt > STALE_NOTICE_AGE_MS
}

function activity(
  report: PatternReport,
  pattern: MinedPattern,
  id: string,
): PatternActivity {
  return report.vocabulary?.[pattern.abstraction]?.[id] ?? {
    id,
    kind: 'event',
    detail: null,
    label: id.replaceAll(':', ' '),
  }
}

function PatternCard({ report, pattern }: { report: PatternReport; pattern: MinedPattern }) {
  const directPct = pattern.occurrences
    ? Math.round((pattern.direct_occurrences / pattern.occurrences) * 100)
    : 0
  return (
    <article className="pattern-card">
      <div className="pattern-sequence" aria-label="pattern sequence">
        <Link className="pattern-detail-link" to={`/experiments/patterns/${pattern.id}`}>
          {pattern.activities.map((id, index) => {
            const item = activity(report, pattern, id)
            return (
              <span className="pattern-step-wrap" key={`${id}-${index}`}>
                {index > 0 && <span className="pattern-arrow">→</span>}
                <span className={`pattern-step ${item.kind}`}>{item.label}</span>
              </span>
            )
          })}
        </Link>
      </div>
      <div className="pattern-metrics">
        <span><b>{fmtInt(pattern.support)}</b> threads ({(pattern.support_ratio * 100).toFixed(1)}%)</span>
        <span><b>{fmtInt(pattern.occurrences)}</b> occurrences</span>
        <span><b>{pattern.lift.toFixed(2)}×</b> lift</span>
        <span><b>{directPct}%</b> adjacent</span>
        <span className="badge">{pattern.abstraction === 'shape' ? 'behavioral shape' : 'tool detail'}</span>
        {pattern.source_concentration && (
          <span className={`badge pattern-concentration ${pattern.source_concentration}`}>
            {pattern.source_concentration.replace('-', ' ')}
          </span>
        )}
      </div>
      {provenance(pattern) && <div className="pattern-provenance">{provenance(pattern)}</div>}
      {pattern.examples.length > 0 && (
        <div className="pattern-examples">
          <span>newest matches</span>
          {pattern.examples.map((example) => (
            <Link
              key={`${example.thread_id}-${example.event_id}`}
              to={`/archive/${example.thread_id}?e=${example.event_id}`}
              title={example.source ?? undefined}
            >
              {example.title || `thread ${example.thread_id}`}
            </Link>
          ))}
        </div>
      )}
      <Link className="pattern-open" to={`/experiments/patterns/${pattern.id}`}>
        View {fmtInt(pattern.support)} matching threads →
      </Link>
    </article>
  )
}

export function PatternsView() {
  const [report, setReport] = useState<PatternReport | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [lens, setLens] = useState<Lens>('all')
  const [sort, setSort] = useState<Sort>('interestingness')
  const [filter, setFilter] = useState('')

  useEffect(() => {
    api.patterns().then(setReport).catch((e) => setErr(String(e.message ?? e)))
  }, [])

  const shown = useMemo(() => {
    if (!report) return []
    const needle = filter.trim().toLocaleLowerCase()
    return [...report.patterns]
      .filter((pattern) => lens === 'all' || pattern.abstraction === lens)
      .filter((pattern) =>
        !needle
        || pattern.activities.some((id) => activity(report, pattern, id).label.toLocaleLowerCase().includes(needle))
        || pattern.sources?.some((source) => source.source.toLocaleLowerCase().includes(needle)),
      )
      .sort((a, b) => b[sort] - a[sort])
  }, [report, lens, sort, filter])

  if (err) return <div className="empty">patterns unavailable: {err}</div>
  if (!report) return <div className="empty">loading mined patterns…</div>
  if (report.status === 'not_run') {
    return (
      <div className="wrap pattern-empty">
        <h1 className="title">Patterns</h1>
        <p>No pattern report has been mined yet.</p>
        <code>thread_archive patterns</code>
        <p className="stat-note">The command surveys the corpus in the background-priority CLI and publishes this page’s report.</p>
      </div>
    )
  }
  if (report.status === 'invalid') {
    return <div className="empty">pattern report is unreadable: {report.error ?? 'unknown format'}</div>
  }

  const corpus = report.corpus!
  return (
    <div className="wrap">
      <h1 className="title">Patterns</h1>
      <div className="submeta">
        mined {fmtDate(report.generated_at)} · through event {fmtInt(report.through_event_id ?? 0)}
      </div>
      {report.stale && isOlderThanStaleNoticeAge(report.generated_at) && (
        <div className="pattern-stale">
          {fmtInt(report.stale_events)} newer events are not represented. Run <code>thread_archive patterns</code> to refresh.
        </div>
      )}

      <div className="stat-tiles pattern-tiles">
        <Tile label="threads mined" value={fmtInt(corpus.threads)} />
        <Tile label="behavioral events" value={fmtInt(corpus.sequence_events)} sub={`${fmtInt(corpus.events)} raw events`} />
        <Tile label="patterns shown" value={fmtInt(report.patterns.length)} />
        <Tile label="minimum support" value={fmtInt(report.config?.min_support ?? 0)} sub={`gap ≤ ${report.config?.max_gap ?? 0}`} />
      </div>

      <div className="pattern-controls">
        <input
          className="input pattern-filter"
          type="search"
          placeholder="filter activities or tools…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <select aria-label="pattern lens" value={lens} onChange={(e) => setLens(e.target.value as Lens)}>
          <option value="all">all patterns</option>
          <option value="shape">behavioral shapes</option>
          <option value="detail">tool detail</option>
        </select>
        <select aria-label="pattern sort" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
          <option value="interestingness">most interesting</option>
          <option value="support">most widespread</option>
          <option value="lift">highest lift</option>
        </select>
      </div>

      <p className="stat-note pattern-explainer">
        Paired tool outcomes and bounded-gap sequences across distinct threads. Lift compares observed support with independent activity frequencies; source labels expose provider-specific instrumentation motifs.
      </p>

      <div className="pattern-list">
        {shown.map((pattern) => <PatternCard key={pattern.id} report={report} pattern={pattern} />)}
        {shown.length === 0 && <div className="empty">no patterns match these filters</div>}
      </div>
    </div>
  )
}
