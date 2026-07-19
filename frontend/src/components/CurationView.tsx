import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Curation, type CurationDrain, type CurationRun } from '../api'
import { Tile, fmtInt, fmtTokens } from './StatsView'

// The curation page: what the librarian and gardener drains have done. The two
// drains run unattended on a schedule, each spending a headless Claude instance
// per fire, so the questions this page answers are "is it keeping up", "what is
// it costing", and "what is stuck" — see thread_archive._curation.stats for what
// each figure means and which are deliberately absent (there is no per-day
// summary count, and no dollar cost: neither is knowable from what's recorded).
//
// Every chart here is a single-series small multiple on its own scale. Curation
// output spans three orders of magnitude across its measures (a thousand
// citations a day beside tens of links), so one shared axis would flatten the
// small ones to nothing and a second axis would be a lie about comparability.

function fmtAgo(seconds: number | null): string {
  if (seconds == null) return 'never'
  if (seconds < 90) return `${Math.round(seconds)}s ago`
  const mins = seconds / 60
  if (mins < 90) return `${Math.round(mins)}m ago`
  const hours = mins / 60
  if (hours < 36) return `${Math.round(hours)}h ago`
  return `${Math.round(hours / 24)}d ago`
}

function fmtCadence(drain: CurationDrain): string {
  const c = drain.cadence
  if (c.kind === 'daily') return `daily at ${c.at}`
  const mins = Math.round(c.interval_s / 60)
  return mins === 60 ? 'hourly' : `every ${mins}m`
}

function fmtDay(iso: string): string {
  const d = new Date(iso + 'T00:00:00')
  return isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function fmtWhen(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z')
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

// A day-by-day column chart for one measure. Single series on its own scale, so
// it carries no legend — the heading names it. A zero day renders as an empty
// slot rather than a hairline, so a gap in a drain's record reads as a gap.
function DayChart<T extends { day: string }>({
  title, rows, value, format = fmtInt, note,
}: {
  title: string
  rows: T[]
  value: (row: T) => number
  format?: (n: number) => string
  note?: string
}) {
  const [hover, setHover] = useState<number | null>(null)
  const values = rows.map(value)
  const max = Math.max(1, ...values)
  const total = values.reduce((a, b) => a + b, 0)
  const active = hover != null ? rows[hover] : null

  return (
    <div className="day-chart">
      <div className="day-chart-head">
        <h3 className="day-chart-title">{title}</h3>
        <span className="day-chart-readout">
          {active
            ? `${fmtDay(active.day)} · ${format(value(active))}`
            : `${format(total)} over ${rows.length}d`}
        </span>
      </div>
      <div className="day-cols" onMouseLeave={() => setHover(null)}>
        {rows.map((r, i) => {
          const v = values[i]
          return (
            <div
              key={r.day}
              className={'day-col' + (hover === i ? ' hot' : '')}
              onMouseEnter={() => setHover(i)}
              // The bar is thin; the hit target is the full-height column.
              title={`${fmtDay(r.day)}: ${format(v)}`}
            >
              <div
                className="day-col-fill"
                style={{ height: v > 0 ? `max(3px, ${(v / max) * 100}%)` : '0' }}
              />
            </div>
          )
        })}
      </div>
      <div className="day-axis">
        <span>{rows.length ? fmtDay(rows[0].day) : ''}</span>
        <span>{rows.length ? fmtDay(rows[rows.length - 1].day) : ''}</span>
      </div>
      {note && <p className="stat-note">{note}</p>}
    </div>
  )
}

function DrainCard({ name, drain }: { name: string; drain: CurationDrain }) {
  // A failed gate query reads as unknown, never as drained: the daemon fails
  // open and launches on an unknown count, so "0" here would be a lie about
  // what the next fire will do.
  const backlog = drain.backlog
  const state =
    backlog == null ? 'unknown' : backlog === 0 ? 'drained' : `${fmtInt(backlog)} queued`
  return (
    <div className="drain-card">
      <div className="drain-head">
        <span className="drain-name">{name}</span>
        <span className={'drain-state' + (backlog === 0 ? ' ok' : backlog == null ? ' unknown' : '')}>
          {state}
        </span>
      </div>
      <dl className="drain-facts">
        <div><dt>last fired</dt><dd>{fmtAgo(drain.heartbeat_age_s)}</dd></div>
        <div><dt>cadence</dt><dd>{fmtCadence(drain)}</dd></div>
        <div><dt>per run</dt><dd>{fmtInt(drain.batch)} max</dd></div>
        <div>
          <dt>model</dt>
          <dd>{drain.model}{drain.effort ? ` · ${drain.effort}` : ''}</dd>
        </div>
      </dl>
    </div>
  )
}

function RunTable({ rows }: { rows: CurationRun[] }) {
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>started</th>
            <th>drain</th>
            <th className="num">requests</th>
            <th className="num">output</th>
            <th>transcript</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{fmtWhen(r.started_at)}</td>
              <td>{r.kind}</td>
              <td className="num">{r.requests ? fmtInt(r.requests) : '—'}</td>
              <td className="num">{r.output_tokens ? fmtTokens(r.output_tokens) : '—'}</td>
              <td>
                <Link to={'/archive/' + r.id}>{r.title ?? 'run'}</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function CurationView() {
  const [data, setData] = useState<Curation | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.curation().then(setData).catch((e) => setErr(String(e.message ?? e)))
  }, [])

  if (err) return <div className="empty">curation stats unavailable: {err}</div>
  if (!data) return <div className="empty">reading the curation queues… (the backlog gate walks every conversation)</div>

  const { drains, graph, coverage, uncuratable, activity, runs } = data
  const runTotal = runs.by_day.reduce((a, r) => a + r.librarian + r.gardener, 0)
  const tokenTotal = runs.by_day.reduce((a, r) => a + r.output_tokens, 0)
  const uncited = coverage.conversations - coverage.cited
  const unsummarized = coverage.conversations - coverage.summarized

  return (
    <div className="wrap">
      <h1 className="title">Curation</h1>
      <div className="submeta">
        the archive curating itself · {fmtInt(runTotal)} drain runs and{' '}
        {fmtTokens(tokenTotal)} output tokens over {data.days} days
      </div>

      <div className="drain-cards">
        <DrainCard name="librarian" drain={drains.librarian} />
        <DrainCard name="gardener" drain={drains.gardener} />
      </div>

      <div className="stat-section">
        <h2 className="stat-h">Output per day</h2>
        <div className="day-charts">
          <DayChart title="Citations" rows={activity} value={(r) => r.citations} />
          <DayChart title="Links" rows={activity} value={(r) => r.links} />
          <DayChart title="New topics" rows={activity} value={(r) => r.topics} />
        </div>
        <p className="stat-note">
          Counted from each row’s own creation time. Summaries have no set-time of
          their own — only the thread’s last-updated stamp, which any write touches
          — so they appear below as coverage rather than as a daily figure that
          would be a guess.
        </p>
      </div>

      <div className="stat-section">
        <h2 className="stat-h">What the drains spend</h2>
        <div className="day-charts">
          <DayChart title="Runs" rows={runs.by_day} value={(r) => r.librarian + r.gardener} />
          <DayChart title="Requests" rows={runs.by_day} value={(r) => r.requests} />
          <DayChart title="Output tokens" rows={runs.by_day} value={(r) => r.output_tokens} format={fmtTokens} />
        </div>
        <p className="stat-note">
          Read from the drains’ own archived transcripts — the runs are sessions
          like any other, so the archive reports its own curation cost. No dollar
          figure: these run on a subscription login that records no per-token
          price. Input tokens are omitted because the recorded figure excludes
          cached context and would understate the read volume several times over.
        </p>
      </div>

      <div className="stat-section">
        <h2 className="stat-h">Coverage</h2>
        <div className="stat-tiles">
          <Tile
            label="summarized"
            value={fmtInt(coverage.summarized)}
            sub={unsummarized > 0 ? `${fmtInt(unsummarized)} without a summary` : 'all conversations'}
          />
          <Tile
            label="cited"
            value={fmtInt(coverage.cited)}
            sub={uncited > 0 ? `${fmtInt(uncited)} never cited` : 'all conversations'}
          />
          <Tile label="citations" value={fmtInt(coverage.citations)} sub={`${fmtInt(coverage.links)} links`} />
          <Tile
            label="topics"
            value={fmtInt(coverage.topics_live)}
            sub={`${fmtInt(coverage.topics_archived)} archived`}
          />
        </div>
      </div>

      <div className="stat-section">
        <h2 className="stat-h">Graph health</h2>
        <div className="stat-tiles">
          <Tile
            label="in hierarchy"
            value={graph.hierarchy_pct != null ? `${graph.hierarchy_pct}%` : '—'}
            sub={`${fmtInt(graph.in_hierarchy)} of ${fmtInt(graph.topics)} topics`}
          />
          <Tile label="singletons" value={fmtInt(graph.singletons)} sub="linked to nothing live" />
          <Tile label="uncited" value={fmtInt(graph.uncited)} sub="no evidence left" />
          <Tile label="unparented" value={fmtInt(graph.unparented)} sub="linked, outside the hierarchy" />
          <Tile label="dupe pairs" value={fmtInt(graph.dupe_pairs)} sub="near-identical titles" />
        </div>
        <p className="stat-note">
          The gardener’s queues, and its backlog above is their sum. A topic leaves
          a queue the moment the graph no longer exhibits the issue — there is no
          ledger to keep in sync.
        </p>
      </div>

      {uncuratable.threads > 0 && (
        <div className="stat-section">
          <h2 className="stat-h">Not curatable</h2>
          <div className="stat-tiles">
            <Tile
              label="content-free threads"
              value={fmtInt(uncuratable.threads)}
              sub="events, but no message"
            />
          </div>
          <div className="stat-table-wrap">
            <table className="stat-table">
              <thead>
                <tr>
                  <th>thread</th>
                  <th>source</th>
                  <th>events present</th>
                </tr>
              </thead>
              <tbody>
                {uncuratable.sample.map((t) => (
                  <tr key={t.id}>
                    <td><Link to={'/archive/' + t.id}>{t.title ?? t.id}</Link></td>
                    <td>{t.source ?? '—'}</td>
                    <td>{t.event_types ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="stat-note">
            These threads carry events but no message — nothing to cite, nothing to
            summarize — so no drain can ever clear them. That makes them an ingest
            condition rather than curation backlog, and the queues exclude them by
            design. They are counted here because work that sits outside every
            queue is otherwise invisible: a rising number means sessions are
            registering threads without their content.
          </p>
        </div>
      )}

      {runs.recent.length > 0 && (
        <div className="stat-section">
          <h2 className="stat-h">Recent runs</h2>
          <RunTable rows={runs.recent} />
        </div>
      )}
    </div>
  )
}
