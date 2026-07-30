import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import {
  api,
  DEFAULT_HOURS,
  type BenchRunRecord,
  type BenchRuns,
  type Benchmark,
  type LabInventory,
  type LatencyBand,
  type RetrievalReport,
  type TelemetryReport,
} from '../api'
import { Pill, measure } from './labRuns'

// The three instruments at a glance.
//
// Each panel is one page's headline and nothing else — the question it answers
// is "is anything worth opening", not "what happened". Every panel therefore
// links to the page that can actually explain it, and none of them restate that
// page's reasoning: the prose lives where the detail does.
//
// One window drives retrieval and telemetry together. Read side by side they
// have to cover the same stretch of time or the two halves of the screen are
// answering about different afternoons — and it opens on the same window both
// instruments do (`DEFAULT_HOURS`), so a panel and the page behind it quote the
// same numbers until somebody moves one of them.
//
// What the panels may not do is pool. The retrieval page's whole argument is
// that a median across front doors is a mixture nobody waited on, and a
// dashboard is exactly where that number would get quoted as the headline — so
// the door table comes across intact, per door, and the pooled figures stay off
// this page entirely.

const WINDOWS = [
  { hours: 6, label: '6 hours' },
  { hours: 24, label: '24 hours' },
  { hours: 72, label: '3 days' },
  { hours: 7 * 24, label: '7 days' },
  { hours: 14 * 24, label: '14 days' },
  { hours: 30 * 24, label: '30 days' },
]

/** Rows a half-width panel shows before it says how many it left. Eight is what
 *  fits beside its neighbour without the two panels' footers drifting apart. */
const CAP = 8

function ms(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v < 1) return '<1ms'
  if (v < 1000) return `${Math.round(v)}ms`
  return `${(v / 1000).toFixed(v < 10_000 ? 2 : 1)}s`
}

/** A duration in seconds, at the scale it is read on: warm-up paid across a
 *  window runs to hours, and a page that prints 8044s makes the reader divide. */
function seconds(s: number | null | undefined): string {
  if (s == null) return '—'
  if (s < 90) return `${Math.round(s)}s`
  if (s < 5400) return `${(s / 60).toFixed(0)} min`
  return `${(s / 3600).toFixed(1)} hr`
}

function bytes(value: number | null | undefined): string {
  if (!value) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(0)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}

function integer(value: number | null | undefined): string {
  return value == null ? '—' : new Intl.NumberFormat().format(value)
}

/** A percentile off a band, or null when the band has no samples — the server
 *  reports an empty distribution as zeros, and a zero would render as the
 *  fastest number on the page. */
function band(b: LatencyBand | undefined, key: 'p50' | 'p90' | 'p99'): number | null {
  return b && b.n > 0 ? (b[key] ?? null) : null
}

/** A run's instant, short enough for a compact cell and in the reader's own
 *  zone; the recorded UTC stays exactly available as the cell's title. */
function stamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

/** One fetch, its error kept beside its data. Every panel loads independently
 *  and fails independently: three of these read different ledgers through
 *  different code, and a dashboard that blanks entirely because one of them is
 *  mid-rebuild is worse than the page it replaced. */
interface Loaded<T> {
  data: T | null
  error: string | null
}

function useReport<T>(load: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ data: null, error: null })
  useEffect(() => {
    let live = true
    setState({ data: null, error: null })
    load()
      .then((data) => live && setState({ data, error: null }))
      .catch((reason) => live && setState({ data: null, error: String(reason) }))
    return () => {
      live = false
    }
  }, deps)
  return state
}

/** A panel's body while its fetch is out or after it failed. The subject is
 *  named, because four panels load separately and "could not load" alone leaves
 *  the reader to work out which ledger is unreadable. */
function Body<T>({
  state,
  subject,
  children,
}: {
  state: Loaded<T>
  subject: string
  children: (data: T) => React.ReactNode
}) {
  if (state.error) return <p className="error small">Could not load {subject}: {state.error}</p>
  if (!state.data) return <p className="muted small">Loading…</p>
  return <>{children(state.data)}</>
}

/** One panel: a heading, a body, and the way to the page that explains it. The
 *  link is not optional — a number with nowhere to go is where a dashboard
 *  starts being the thing people read instead of the instrument. */
function Panel({
  title,
  to,
  linkText,
  wide,
  children,
}: {
  title: string
  to: string
  linkText: string
  wide?: boolean
  children: React.ReactNode
}) {
  return (
    <section className={'dash-panel' + (wide ? ' wide' : '')}>
      <div className="dash-head">
        <h2>{title}</h2>
        <Link to={to}>{linkText}</Link>
      </div>
      {children}
    </section>
  )
}

/** A panel's headline numbers, two across. Deliberately not `.stat-tile`: a
 *  bordered tile inside a bordered panel reads as a box in a box, and these are
 *  the panel's own figures rather than a row of cards. */
function Metrics({ items }: { items: { label: string; value: string; sub?: string }[] }) {
  return (
    <div className="dash-metrics">
      {items.map((item) => (
        <div key={item.label} className="dash-metric">
          <span className="n">{item.value}</span>
          <span className="l">{item.label}</span>
          {item.sub && <span className="s">{item.sub}</span>}
        </div>
      ))}
    </div>
  )
}

/** What a bench row or a run is worth, in one figure: the first metric its row
 *  declares. The rest are on the run's own page — a cell wide enough for eight
 *  of them is a cell nothing else fits beside. */
function Lead({ keys, measures }: { keys: string[]; measures: Record<string, number | null> }) {
  const key = keys.find((k) => typeof measures[k] === 'number')
  if (!key) return <span className="muted">—</span>
  return (
    <span className="lab-measures">
      <span>
        <span className="muted">{key}</span> {measure(measures[key])}
      </span>
    </span>
  )
}

/** The state a bench row is in, in the lab's own words and tones. */
const BENCH_STATE: Record<string, { label: string; tone: string }> = {
  fresh: { label: 'fresh', tone: 'good' },
  stale: { label: 'stale', tone: 'warn' },
  'never-run': { label: 'never run', tone: 'busy' },
  missing: { label: 'no corpus', tone: 'bad' },
}

function BenchRows({ rows }: { rows: Benchmark[] }) {
  if (!rows.length) return <p className="muted small">No benchmark rows on this box.</p>
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>row</th>
            <th>state</th>
            <th>last measured</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const state = BENCH_STATE[row.state] ?? BENCH_STATE.missing
            return (
              <tr key={row.name}>
                <td><code>{row.name}</code></td>
                <td><Pill tone={state.tone}>{state.label}</Pill></td>
                <td>
                  <Lead keys={row.measure_keys} measures={row.last?.measures ?? {}} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function RecentRuns({ runs }: { runs: BenchRunRecord[] }) {
  if (!runs.length) return <p className="muted small">No runs recorded on this box yet.</p>
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>when</th>
            <th>row</th>
            <th>measured</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td title={run.at}>
                <Link className="lab-run-link" to={`/lab/run/${run.id}`}>
                  {stamp(run.at)}
                </Link>
              </td>
              <td>
                <code>{run.row}</code>
                {run.status !== 'ok' && (
                  <>
                    {' '}
                    <Pill tone="bad">{run.status}</Pill>
                  </>
                )}
              </td>
              <td>
                <Lead keys={run.measure_keys} measures={run.measures} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function DashboardView() {
  const [hours, setHours] = useState(DEFAULT_HOURS)
  const retrieval = useReport<RetrievalReport>(() => api.retrieval(hours), [hours])
  const telemetry = useReport<TelemetryReport>(() => api.telemetry(hours), [hours])
  // The inventory and the ledger are windowless — an inventory describes the box
  // now, and the run history is the box's whole history — so neither refetches
  // when the window moves.
  const lab = useReport<LabInventory>(() => api.searchLab(), [])
  const ledger = useReport<BenchRuns>(() => api.searchLabRuns(), [])

  return (
    <div className="dash-page">
      <header className="rv-head">
        <h1>Overview</h1>
        <label className="rv-range">
          window
          <select value={hours} onChange={(event) => setHours(Number(event.target.value))}>
            {WINDOWS.map((window) => (
              <option key={window.hours} value={window.hours}>
                {window.label}
              </option>
            ))}
          </select>
        </label>
      </header>
      <p className="dash-lede muted">
        What each instrument leads with, over one window. Every panel opens onto the
        page that explains it.
      </p>

      <div className="dash-grid">
        <Panel title="Front doors" to="/retrieval" linkText="retrieval →">
          <Body state={retrieval} subject="the retrieval report">
            {(report) => {
              const doors = [...(report.served?.by_surface ?? [])].sort((a, b) => b.n - a.n)
              if (!doors.length)
                return <p className="muted small">No searches recorded in this window.</p>
              return (
                <div className="stat-table-wrap">
                  <table className="stat-table">
                    <thead>
                      <tr>
                        <th>door</th>
                        <th className="num">searches</th>
                        <th className="num">typical</th>
                        <th className="num">cold</th>
                      </tr>
                    </thead>
                    <tbody>
                      {doors.slice(0, CAP).map((door) => (
                        <tr key={door.surface}>
                          <td><code>{door.surface}</code></td>
                          <td className="num">{integer(door.n)}</td>
                          <td className="num">{ms(band(door.warm_interactive, 'p50'))}</td>
                          <td className="num">{ms(band(door.cold, 'p50'))}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }}
          </Body>
          <p className="dash-note">
            Per door, because the doors are not one population — a shared server warms
            once, a terminal search is its own process every time. <em>Typical</em> is a
            warm first-page ask; <em>cold</em> is a search that paid a model load itself.
            {retrieval.data?.restarts && (
              <>
                {' '}
                {integer(retrieval.data.restarts.n)} process starts cost{' '}
                {seconds(retrieval.data.restarts.total_s)} of warm-up in this window.
              </>
            )}
          </p>
        </Panel>

        <Panel title="Where the time goes" to="/retrieval" linkText="retrieval →">
          <Body state={retrieval} subject="the retrieval report">
            {(report) => {
              const stages = [...(report.stages?.stages ?? [])]
                .sort((a, b) => b.p50 - a.p50)
                .slice(0, 6)
              const top = stages[0]?.p50 ?? 0
              if (!stages.length)
                return <p className="muted small">No staged searches in this window.</p>
              return (
                <div className="stat-table-wrap">
                  <table className="stat-table">
                    <thead>
                      <tr>
                        <th>stage</th>
                        <th className="bar-col">median</th>
                        <th className="num">slow 1 in 10</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stages.map((stage) => (
                        <tr key={stage.stage}>
                          <td><code>{stage.stage}</code></td>
                          <td className="bar-col">
                            <span className="stat-bar">
                              <span
                                className="stat-bar-fill"
                                style={{ width: `${top ? (100 * stage.p50) / top : 0}%` }}
                              />
                            </span>
                            <span className="bar-num">{ms(stage.p50)}</span>
                          </td>
                          <td className="num">{ms(stage.p90)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }}
          </Body>
          <p className="dash-note">
            The six slowest stages, median across served searches. The lexical and
            semantic arms run at the same time, so the stages do not add up to a search
            and none of them is a share of one.
          </p>
        </Panel>

        <Panel title="Web requests" to="/telemetry" linkText="telemetry →">
          <Body state={telemetry} subject="telemetry">
            {(report) => (
              <>
                <Metrics
                  items={[
                    {
                      label: 'requests',
                      value: integer(report.web.requests),
                      sub: `${bytes(report.web.bytes)} served`,
                    },
                    {
                      label: 'errors',
                      value: integer(report.web.errors),
                      sub: `${integer(report.web.concurrent)} saw concurrency`,
                    },
                    { label: 'median', value: ms(report.web.p50) },
                    {
                      label: 'slow 1 in 20',
                      value: ms(report.web.p95),
                      sub: `p99 ${ms(report.web.p99)}`,
                    },
                  ]}
                />
                {report.web.endpoints.length > 0 && (
                  <div className="stat-table-wrap">
                    <table className="stat-table">
                      <thead>
                        <tr>
                          <th>slowest endpoint</th>
                          <th className="num">calls</th>
                          <th className="num">p95</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.web.endpoints.slice(0, 3).map((endpoint) => (
                          <tr key={`${endpoint.method} ${endpoint.path}`}>
                            <td><code>{endpoint.path}</code></td>
                            <td className="num">{integer(endpoint.n)}</td>
                            <td className="num">{ms(endpoint.p95)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </Body>
          <p className="dash-note">
            The archive's own viewer and this server, off the request ledger. A response
            at or above 400 is an error; no search text or conversation content is
            recorded here.
          </p>
        </Panel>

        <Panel title="Ingest" to="/telemetry" linkText="telemetry →">
          <Body state={telemetry} subject="telemetry">
            {(report) => {
              const sources = Object.entries(report.ingest.sources).sort(
                (a, b) => b[1].events - a[1].events,
              )
              const totals = sources.reduce(
                (sum, [, row]) => ({
                  passes: sum.passes + row.passes,
                  events: sum.events + row.events,
                  bytes: sum.bytes + row.bytes,
                }),
                { passes: 0, events: 0, bytes: 0 },
              )
              return (
                <>
                  <Metrics
                    items={[
                      { label: 'passes', value: integer(totals.passes), sub: 'that found work' },
                      { label: 'events written', value: integer(totals.events) },
                      { label: 'read', value: bytes(totals.bytes) },
                      {
                        label: 'fault signatures',
                        value: integer(report.faults.length),
                        sub: report.faults.length ? report.faults[0].signature : 'none retained',
                      },
                    ]}
                  />
                  {sources.length > 0 && (
                    <div className="stat-table-wrap">
                      <table className="stat-table">
                        <thead>
                          <tr>
                            <th>source</th>
                            <th className="num">passes</th>
                            <th className="num">events</th>
                            <th className="num">errors</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sources.slice(0, 3).map(([name, row]) => (
                            <tr key={name}>
                              <td><code>{name}</code></td>
                              <td className="num">{integer(row.passes)}</td>
                              <td className="num">{integer(row.events)}</td>
                              <td className={'num' + (row.errors ? ' telemetry-bad' : '')}>
                                {integer(row.errors)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )
            }}
          </Body>
          <p className="dash-note">
            Only polls that found work are counted. Faults are folded by signature and
            retained across the whole ledger, so that count is not bounded by this
            window — and it is a lower bound, since repeats are written at powers of ten.
          </p>
        </Panel>

        <Panel title="The bench" to="/lab" linkText="lab →" wide>
          <div className="dash-split">
            <div>
              <h3 className="dash-h3">Rows</h3>
              <Body state={lab} subject="the bench inventory">
                {(inventory) => (
                  <>
                    <BenchRows rows={inventory.benchmarks.slice(0, CAP)} />
                    {inventory.benchmarks.length > CAP && (
                      <p className="muted small">
                        <Link to="/lab">
                          +{inventory.benchmarks.length - CAP} more rows
                        </Link>
                      </p>
                    )}
                  </>
                )}
              </Body>
            </div>
            <div>
              <h3 className="dash-h3">Newest runs</h3>
              <Body state={ledger} subject="the benchmark ledger">
                {(runs) => (
                  <>
                    <RecentRuns runs={runs.runs.slice(0, CAP)} />
                    {runs.total > CAP && (
                      <p className="muted small">
                        <Link to="/lab">+{runs.total - CAP} more runs</Link>
                      </p>
                    )}
                  </>
                )}
              </Body>
            </div>
          </div>
          <p className="dash-note">
            What the bench measured last, beside the ledger it measured into. A{' '}
            <em>stale</em> row was measured under ranking code the working tree has since
            changed, so its numbers describe a configuration rather than the present one.
            These are public benchmarks: they say whether retrieval is competitive in
            general, never whether search got better on this archive.
          </p>
        </Panel>
      </div>

      <p className="dash-facts">
        {lab.data && (
          <>
            <span>
              corpora{' '}
              {lab.data.datasets.filter((d) => d.homes.some((h) => h.built)).length}/
              {lab.data.datasets.length} built
            </span>
            <span>{bytes(lab.data.cache.bytes)} of bench corpora on disk</span>
            <span>
              ranking code <code>{lab.data.code_id.slice(0, 8)}</code>
            </span>
          </>
        )}
        {ledger.data && <span>{integer(ledger.data.total)} runs recorded</span>}
        {telemetry.data && (
          <>
            <span>
              {bytes(telemetry.data.ledgers.reduce((sum, l) => sum + l.bytes, 0))} of
              ledgers retained
            </span>
            <span><code>{telemetry.data.home}</code></span>
          </>
        )}
      </p>
    </div>
  )
}
