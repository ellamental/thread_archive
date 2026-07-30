import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api,
  type TelemetryFault,
  type TelemetryIngestSource,
  type TelemetryReport,
} from '../api'

const WINDOWS = [
  { hours: 6, label: '6 hours' },
  { hours: 24, label: '24 hours' },
  { hours: 72, label: '3 days' },
  { hours: 7 * 24, label: '7 days' },
  { hours: 30 * 24, label: '30 days' },
  { hours: 90 * 24, label: '90 days' },
]

function integer(value: number): string {
  return new Intl.NumberFormat().format(value)
}

function duration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`
}

function bytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(0)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}

function stamp(iso: string): string {
  const value = new Date(iso)
  if (isNaN(value.getTime())) return iso || '—'
  return value.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function Tile({ value, label, sub }: { value: string; label: string; sub: string }) {
  return (
    <div className="stat-tile">
      <div className="n">{value}</div>
      <div className="l">{label}</div>
      <div className="s">{sub}</div>
    </div>
  )
}

function faultMagnitude(fault: TelemetryFault): string {
  return `${integer(fault.count)}+`
}

export function TelemetryView() {
  const [hours, setHours] = useState(24)
  const [report, setReport] = useState<TelemetryReport | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setReport(null)
    setError(null)
    api
      .telemetry(hours)
      .then((value) => live && setReport(value))
      .catch((reason) => live && setError(String(reason)))
    return () => {
      live = false
    }
  }, [hours])

  const ingest = useMemo(() => {
    const sources = Object.entries(report?.ingest.sources ?? {})
    const totals = sources.reduce(
      (sum, [, row]) => ({
        passes: sum.passes + row.passes,
        events: sum.events + row.events,
        errors: sum.errors + row.errors,
      }),
      { passes: 0, events: 0, errors: 0 },
    )
    return { sources, totals }
  }, [report])

  const stages = useMemo(
    () =>
      Object.entries(report?.ingest.stages ?? {})
        .sort((a, b) => b[1] - a[1]),
    [report],
  )

  if (error)
    return (
      <div className="telemetry-page">
        <p className="error">Could not load telemetry: {error}</p>
      </div>
    )
  if (!report)
    return (
      <div className="telemetry-page">
        <p className="muted">Loading…</p>
      </div>
    )

  return (
    <div className="telemetry-page">
      <header className="rv-head">
        <div>
          <p className="eyebrow">Developer panels</p>
          <h1>Telemetry</h1>
        </div>
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
      <p className="telemetry-lede">
        The work archive did in this window, read directly from its retained local
        ledgers. This page is diagnostic: conversation content and search text are not
        recorded in the web-request data.
      </p>

      <div className="stat-tiles">
        <Tile
          value={integer(report.web.requests)}
          label="web requests"
          sub={`p50 ${duration(report.web.p50)} · p95 ${duration(report.web.p95)}`}
        />
        <Tile
          value={integer(report.web.errors)}
          label="web errors"
          sub={`${integer(report.web.concurrent)} requests saw concurrency`}
        />
        <Tile
          value={integer(ingest.totals.passes)}
          label="ingest passes"
          sub={`${integer(ingest.totals.events)} events written`}
        />
        <Tile
          value={integer(report.faults.length)}
          label="fault signatures"
          sub="retained history, folded across repeats"
        />
      </div>

      <section>
        <h2>Web requests</h2>
        <p className="muted">
          Endpoints are ordered by their slow 1-in-20 request. A response at or above
          400 is an error; “concurrent” means the request overlapped another request
          served by this process.
        </p>
        {report.web.endpoints.length ? (
          <div className="stat-table-wrap">
            <table className="stat-table telemetry-web-table">
              <thead>
                <tr>
                  <th>endpoint</th>
                  <th className="num">calls</th>
                  <th className="num">errors</th>
                  <th className="num">median</th>
                  <th className="num">p95</th>
                  <th className="num">p99</th>
                  <th className="num">max</th>
                  <th className="num">response</th>
                  <th className="num">concurrent</th>
                </tr>
              </thead>
              <tbody>
                {report.web.endpoints.map((endpoint) => (
                  <tr key={`${endpoint.method} ${endpoint.path}`}>
                    <td>
                      {endpoint.method !== 'GET' && (
                        <span className="telemetry-method">{endpoint.method}</span>
                      )}
                      <code>{endpoint.path}</code>
                    </td>
                    <td className="num">{integer(endpoint.n)}</td>
                    <td className={'num' + (endpoint.errors ? ' telemetry-bad' : '')}>
                      {integer(endpoint.errors)}
                    </td>
                    <td className="num">{duration(endpoint.p50)}</td>
                    <td className="num">{duration(endpoint.p95)}</td>
                    <td className="num">{duration(endpoint.p99)}</td>
                    <td className="num">{duration(endpoint.max)}</td>
                    <td className="num">{bytes(endpoint.bytes)}</td>
                    <td className="num">{integer(endpoint.concurrent)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No web requests recorded in this window.</p>
        )}
      </section>

      <section>
        <h2>Ingest</h2>
        <p className="muted">
          Only polls that found work are retained. Pass time is the wall clock around
          the source poll, including the directory walk and fingerprint checks outside
          the measured import stages.
        </p>
        {ingest.sources.length ? (
          <div className="stat-table-wrap">
            <table className="stat-table">
              <thead>
                <tr>
                  <th>source</th>
                  <th className="num">passes</th>
                  <th className="num">items</th>
                  <th className="num">events</th>
                  <th className="num">read</th>
                  <th className="num">median pass</th>
                  <th className="num">p95 pass</th>
                  <th className="num">wall time</th>
                  <th className="num">errors</th>
                </tr>
              </thead>
              <tbody>
                {ingest.sources.map(([source, row]: [string, TelemetryIngestSource]) => (
                  <tr key={source}>
                    <td><code>{source}</code></td>
                    <td className="num">{integer(row.passes)}</td>
                    <td className="num">{integer(row.items)}</td>
                    <td className="num">{integer(row.events)}</td>
                    <td className="num">{bytes(row.bytes)}</td>
                    <td className="num">{duration(row.pass_p50_ms)}</td>
                    <td className="num">{duration(row.pass_p95_ms)}</td>
                    <td className="num">{duration(row.total_s * 1000)}</td>
                    <td className={'num' + (row.errors ? ' telemetry-bad' : '')}>
                      {integer(row.errors)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No ingest work recorded in this window.</p>
        )}

        <div className="telemetry-split">
          <div>
            <h3>Where ingest time went</h3>
            {stages.length ? (
              <table className="stat-table">
                <thead>
                  <tr>
                    <th>stage</th>
                    <th className="num">total</th>
                  </tr>
                </thead>
                <tbody>
                  {stages.map(([stage, milliseconds]) => (
                    <tr key={stage}>
                      <td><code>{stage}</code></td>
                      <td className="num">{duration(milliseconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted">No stage timings in this window.</p>
            )}
          </div>
          <div>
            <h3>Background work</h3>
            <table className="stat-table">
              <thead>
                <tr>
                  <th>work</th>
                  <th className="num">passes</th>
                  <th className="num">p95</th>
                  <th className="num">total</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>maintenance</td>
                  <td className="num">{integer(report.ingest.maintenance.passes)}</td>
                  <td className="num">{duration(report.ingest.maintenance.p95_ms)}</td>
                  <td className="num">{duration(report.ingest.maintenance.total_s * 1000)}</td>
                </tr>
                <tr>
                  <td>
                    embed
                    <span className="muted small">
                      {' '}· {integer(report.ingest.embed.embedded ?? 0)} events
                    </span>
                  </td>
                  <td className="num">{integer(report.ingest.embed.passes)}</td>
                  <td className="num">{duration(report.ingest.embed.p95_ms)}</td>
                  <td className="num">{duration(report.ingest.embed.total_s * 1000)}</td>
                </tr>
              </tbody>
            </table>
            {report.ingest.idle && report.ingest.idle.passes > 0 && (
              // Outside the table on purpose: these passes imported nothing, so
              // they have no p95 of import work to sit in that column. What they
              // cost per pass is the number worth reading — it grows with how many
              // files the loop has to look at, not with how busy the machine is.
              <p className="muted small">
                idle · {integer(report.ingest.idle.passes)} passes found nothing ·{' '}
                {duration(report.ingest.idle.per_pass_ms)} each, worst{' '}
                {duration(report.ingest.idle.max_ms)} ·{' '}
                {duration(report.ingest.idle.total_s * 1000)} total
              </p>
            )}
          </div>
        </div>
      </section>

      <section>
        <h2>Ingest faults</h2>
        <p className="muted">
          Retained across the full ledger, not limited to the selected window. Repeated
          messages are folded by signature and written at powers of ten, so counts are
          lower bounds.
        </p>
        {report.faults.length ? (
          <div className="stat-table-wrap">
            <table className="stat-table telemetry-faults">
              <thead>
                <tr>
                  <th>source</th>
                  <th>signature</th>
                  <th className="num">seen</th>
                  <th>first</th>
                  <th>last</th>
                </tr>
              </thead>
              <tbody>
                {report.faults.map((fault) => (
                  <tr key={`${fault.signature}:${fault.first}`}>
                    <td><code>{fault.source}</code></td>
                    <td title={fault.sample}>{fault.signature}</td>
                    <td className="num telemetry-bad">{faultMagnitude(fault)}</td>
                    <td>{stamp(fault.first)}</td>
                    <td>{stamp(fault.last)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No ingest faults retained.</p>
        )}
      </section>

      <section>
        <h2>Retained ledgers</h2>
        <p className="muted">
          Rotation starts a new segment and keeps the old one. Size is therefore the
          full local history, not the current segment’s cap.
        </p>
        <div className="stat-table-wrap">
          <table className="stat-table">
            <thead>
              <tr>
                <th>ledger</th>
                <th>file</th>
                <th className="num">segments</th>
                <th className="num">retained</th>
                <th>deeper view</th>
              </tr>
            </thead>
            <tbody>
              {report.ledgers.map((ledger) => (
                <tr key={ledger.file}>
                  <td>{ledger.label}</td>
                  <td><code>{ledger.file}</code></td>
                  <td className="num">{integer(ledger.segments)}</td>
                  <td className="num">{bytes(ledger.bytes)}</td>
                  <td>
                    {ledger.view === 'retrieval' && <Link to="/retrieval">retrieval →</Link>}
                    {ledger.view === 'health' && <Link to="/health">health →</Link>}
                    {ledger.view === 'telemetry' && <span className="muted">this page</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="telemetry-path"><code>{report.home}</code></p>
      </section>
    </div>
  )
}
