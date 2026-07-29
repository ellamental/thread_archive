import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type BenchPoint,
  type Bucket,
  type LatencyBand,
  type RetrievalReport,
  type ServedBucket,
} from '../api'

// How search itself is doing. Every other view here is about the corpus; this one
// is about the pipeline that reads it.
//
// The charts are plain inline SVG rather than a charting library: this bundle is
// served by a local daemon to an operator watching a handful of series, and a
// dependency that renders them is more code than the series are.

const WARM = 'var(--accent)'
const COLD = '#d1604a'
const QUIET = 'var(--muted)'

/** What each front door is, in the reader's terms. The ledger's names are
 *  process-shaped; the question being asked of them is "who felt this". An
 *  unlisted surface renders as itself rather than being dropped — a new front
 *  door must show up as traffic nobody has labelled yet, not as no traffic. */
const SURFACES: Record<string, string> = {
  'mcp-http': 'shared server — the door agents use',
  'mcp-stdio': 'per-client stdio server — one process per agent, never warmed',
  cli: 'terminal — one process per search, never warmed',
  web: 'web viewer',
  mcp: 'unattributed — recorded before the doors were named',
}

function ms(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v < 1) return '<1ms'
  if (v < 1000) return `${Math.round(v)}ms`
  return `${(v / 1000).toFixed(v < 10_000 ? 2 : 1)}s`
}

/** A percentile off a band, or null when the band has no samples. The server
 *  reports an empty distribution as zeros, and a zero here would render as
 *  "<1ms" — an empty regime claiming to be the fastest thing on the page. */
function band(b: LatencyBand | undefined, key: 'p50' | 'p90' | 'p99'): number | null {
  return b && b.n > 0 ? (b[key] ?? null) : null
}

function day(iso: string): string {
  return iso.slice(5, 10).replace('-', '/')
}

/**
 * A bucket's axis label.
 *
 * Hourly buckets are rendered in the operator's own clock — "when did it get
 * slow" is a question about the wall in this room, and the ledger's UTC would put
 * this afternoon's spike five hours from where it felt like it happened. Daily
 * buckets keep their UTC date: shifting a calendar day into local time would name
 * a different day than the one it aggregates.
 */
function bucketLabel(at: string, bucket: Bucket, full = false): string {
  if (bucket !== 'hour') return day(at)
  const t = new Date(at + ':00:00Z')
  const hh = `${String(t.getHours()).padStart(2, '0')}:00`
  const date = `${t.getMonth() + 1}/${t.getDate()}`
  // On the axis, midnight carries the date instead of an hour so a multi-day
  // hourly axis says which day it crossed into. In a table or a tooltip there is
  // room for both, and no neighbouring label to read the date off.
  if (full) return `${date} ${hh}`
  return t.getHours() === 0 ? date : hh
}

/** The selectable windows, in hours. Everything at or under 72h comes back
 *  bucketed hourly (the server decides — see `default_bucket`). */
const WINDOWS: { hours: number; label: string }[] = [
  { hours: 6, label: '6 hours' },
  { hours: 24, label: '24 hours' },
  { hours: 72, label: '3 days' },
  { hours: 7 * 24, label: '7 days' },
  { hours: 14 * 24, label: '14 days' },
  { hours: 30 * 24, label: '30 days' },
  { hours: 90 * 24, label: '90 days' },
]

interface Series {
  label: string
  color: string
  /** `title` is the point's native tooltip. It carries the sample count, which
   *  matters most at hourly resolution: a median over one search is that search,
   *  and the chart cannot say so on its own. */
  points: { x: number; y: number | null; title?: string }[]
  /** Drawn behind the others, thin and dashed: a series that is *present* but not
   *  the subject. The unmeasured-age line is 260-odd legacy searches blending cold
   *  starts with warm ones, and at equal weight it is the only line long enough to
   *  form a shape — so the eye reads its 10s trend as the page's answer while the
   *  headline says 104ms. Weight is what says which number is the current one. */
  muted?: boolean
}

/** A small multi-series line chart on a log y-axis.
 *
 *  Log, not linear, and that is load-bearing: these series span from ~30ms to
 *  ~30s, and on a linear axis every warm number is a flat line pinned to zero
 *  under one cold spike. The whole question this page answers lives in the bottom
 *  2% of a linear chart. */
function LineChart({
  series,
  labels,
  height = 150,
  measuredFrom,
}: {
  series: Series[]
  labels: string[]
  height?: number
  /** Index where the un-muted series begin. Everything left of it is history the
   *  page cannot classify, and without the rule the chart's *shape* is that
   *  history however faintly it is drawn — the reader sees a 10s trend and a
   *  headline of 104ms and reasonably concludes one of them is wrong. */
  measuredFrom?: number | null
}) {
  const width = 640
  const padL = 46
  // Room for the last x label, which is centred on the final point: at 8px the
  // right-hand date is half outside the box and reads as clipped.
  const padR = 30
  const padB = 20
  const padT = 8
  const all = series.flatMap((s) => s.points.map((p) => p.y)).filter((y): y is number => y != null && y > 0)
  if (!all.length) return <p className="muted">Nothing recorded yet.</p>
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const l10 = Math.log10(Math.max(lo * 0.8, 1))
  const h10 = Math.log10(hi * 1.25)
  const span = Math.max(h10 - l10, 0.2)
  const n = Math.max(labels.length - 1, 1)
  const x = (i: number) => padL + (i / n) * (width - padL - padR)
  const y = (v: number) => padT + (1 - (Math.log10(Math.max(v, 1)) - l10) / span) * (height - padT - padB)

  // Ticks at decade boundaries — 10ms, 100ms, 1s, 10s — so the reader can price a
  // point without reading the axis arithmetic.
  const ticks: number[] = []
  for (let d = Math.floor(l10); d <= Math.ceil(h10); d++) {
    const v = Math.pow(10, d)
    if (v >= lo * 0.5 && v <= hi * 2) ticks.push(v)
  }

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="rv-chart" role="img">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={padL} x2={width - padR} y1={y(t)} y2={y(t)} className="rv-grid" />
          <text x={padL - 6} y={y(t) + 4} className="rv-axis" textAnchor="end">
            {ms(t)}
          </text>
        </g>
      ))}
      {[...series].sort((a, b) => Number(b.muted) - Number(a.muted)).map((s) => {
        // Gaps are real: a day with no searches of that kind has no point, and
        // joining across it would draw a trend nobody measured.
        const segments: string[] = []
        let current: string[] = []
        s.points.forEach((p, i) => {
          if (p.y == null || p.y <= 0) {
            if (current.length) segments.push(current.join(' '))
            current = []
            return
          }
          current.push(`${current.length ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`)
        })
        if (current.length) segments.push(current.join(' '))
        return (
          <g key={s.label}>
            {segments.map((d, i) => (
              <path
                key={i}
                d={d}
                fill="none"
                stroke={s.color}
                strokeWidth={s.muted ? 1 : 2.5}
                strokeDasharray={s.muted ? '3 4' : undefined}
                opacity={s.muted ? 0.45 : 1}
              />
            ))}
            {s.points.map((p, i) =>
              p.y != null && p.y > 0 ? (
                // A series with one sample has no line to read, so the point has to
                // carry it — the current regimes are often exactly that thin.
                <circle
                  key={i}
                  cx={x(i)}
                  cy={y(p.y)}
                  r={s.muted ? 1.6 : 3.5}
                  fill={s.color}
                  opacity={s.muted ? 0.45 : 1}
                >
                  {p.title && <title>{p.title}</title>}
                </circle>
              ) : null,
            )}
          </g>
        )
      })}
      {measuredFrom != null && measuredFrom > 0 && measuredFrom < labels.length && (
        <g>
          <line
            x1={x(measuredFrom)}
            x2={x(measuredFrom)}
            y1={padT}
            y2={height - padB}
            className="rv-mark"
          />
          {/* Flipped to the rule's left once it sits in the last third: measurement
              usually begins near *now*, so anchored right the label runs off the box.
              The gap clears a point sitting on the rule — the first measured bucket
              often has one, and butted up against the text it reads as a bullet. */}
          <text
            x={x(measuredFrom) + (measuredFrom > n * 0.66 ? -9 : 9)}
            y={padT + 9}
            className="rv-axis"
            textAnchor={measuredFrom > n * 0.66 ? 'end' : 'start'}
          >
            measured from here
          </text>
        </g>
      )}
      {labels.map((lab, i) =>
        i % Math.ceil(labels.length / 8) === 0 ? (
          // The first label is anchored left, not centred: centred it sits on top
          // of the bottom y-axis tick, which shares its baseline.
          <text
            key={lab + i}
            x={x(i)}
            y={height - 4}
            className="rv-axis"
            textAnchor={i === 0 ? 'start' : 'middle'}
          >
            {lab}
          </text>
        ) : null,
      )}
    </svg>
  )
}

function Tile({ value, label, sub }: { value: string; label: string; sub?: string }) {
  return (
    <div className="stat-tile">
      <div className="n">{value}</div>
      <div className="l">{label}</div>
      {sub && <div className="s">{sub}</div>}
    </div>
  )
}

function Legend({ items }: { items: { label: string; color: string; muted?: boolean }[] }) {
  return (
    <p className="rv-legend">
      {items.map((i) => (
        <span key={i.label} className={i.muted ? 'rv-muted-key' : undefined}>
          <span
            className={'rv-swatch' + (i.muted ? ' muted' : '')}
            style={{ background: i.color }}
          />{' '}
          {i.label}
        </span>
      ))}
    </p>
  )
}

export function RetrievalView() {
  const [report, setReport] = useState<RetrievalReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hours, setHours] = useState(14 * 24)

  useEffect(() => {
    let live = true
    setReport(null)
    setError(null)
    api
      .retrieval(hours)
      .then((r) => live && setReport(r))
      .catch((e) => live && setError(String(e)))
    return () => {
      live = false
    }
  }, [hours])

  const servedSeries = useMemo(() => {
    const buckets: ServedBucket[] = report?.served?.buckets ?? []
    const unit: Bucket = report?.served?.bucket ?? 'day'
    const pick = (b: ServedBucket, k: 'warm' | 'cold' | 'unknown', i: number) => {
      const band = b[k]
      return {
        x: i,
        y: band && band.n > 0 ? band.p50 : null,
        title: band
          ? `${bucketLabel(b.at, unit, true)} · ${band.n} ${k} ${band.n === 1 ? 'search' : 'searches'} · p50 ${ms(band.p50)}`
          : undefined,
      }
    }
    return {
      unit,
      labels: buckets.map((b) => bucketLabel(b.at, unit)),
      series: [
        { label: 'warm p50', color: WARM, points: buckets.map((b, i) => pick(b, 'warm', i)) },
        { label: 'cold p50', color: COLD, points: buckets.map((b, i) => pick(b, 'cold', i)) },
        {
          // Not a third regime — a blend of the other two, from before the stage
          // probe recorded what a search paid. Kept so the window has history,
          // drawn subordinate so it cannot be mistaken for the current number.
          label: 'before the stage probe (cold and warm mixed)',
          color: QUIET,
          muted: true,
          points: buckets.map((b, i) => pick(b, 'unknown', i)),
        },
      ].filter((s) => s.points.some((p) => p.y != null)),
      measuredFrom: buckets.findIndex((b) => b.warm != null || b.cold != null),
    }
  }, [report])

  if (error)
    return (
      <div className="retrieval-page">
        <p className="error">Could not load retrieval health: {error}</p>
      </div>
    )
  if (!report)
    return (
      <div className="retrieval-page">
        <p className="muted">Loading…</p>
      </div>
    )

  const served = report.served
  const stages = report.stages
  const restarts = report.restarts
  const bench = report.bench ?? {}
  const topStage = stages?.stages?.[0]

  const benchSeries: Series[] = Object.entries(bench).map(([set, points], i) => ({
    label: set,
    color: i === 0 ? WARM : COLD,
    points: (points as BenchPoint[]).map((p, j) => ({ x: j, y: p.p50 })),
  }))
  const benchLen = Math.max(0, ...Object.values(bench).map((p) => p.length))

  return (
    <div className="retrieval-page">
      <header className="rv-head">
        <h1>Retrieval</h1>
        <label className="rv-range">
          window
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {WINDOWS.map((w) => (
              <option key={w.hours} value={w.hours}>
                {w.label}
              </option>
            ))}
          </select>
        </label>
      </header>

      <div className="stat-tiles">
        <Tile
          value={ms(band(served?.warm_interactive, 'p50'))}
          label="typical search"
          sub={
            served?.warm_interactive.n
              ? `warm first-page · ${served.warm_interactive.n} calls`
              : 'no search yet on a settled process'
          }
        />
        <Tile
          value={ms(band(served?.warm_interactive, 'p90'))}
          label="slow 1 in 10"
          sub={`p99 ${ms(band(served?.warm_interactive, 'p99'))}`}
        />
        <Tile
          value={ms(band(served?.warm_bulk, 'p50'))}
          label="bulk & paged"
          sub={`${served?.warm_bulk.n ?? 0} wide or paged calls`}
        />
        <Tile
          value={ms(band(served?.cold, 'p50'))}
          label="first search after a restart"
          sub={`${served?.cold.n ?? 0} of ${served?.n ?? 0} calls`}
        />
        <Tile
          value={String(restarts?.n ?? 0)}
          label="process starts"
          sub={`${ms(restarts?.p50_ms)} each to warm up`}
        />
      </div>

      <section>
        <h2>What agents got</h2>
        <p className="muted">
          Median served latency per {servedSeries.unit}. Warm and cold are drawn apart on
          purpose — every cache search leans on is process-local, so a restart resets them
          and the first search pays the reload. Averaging the two tracks the restart rate,
          not the code. A search counts as cold when it recorded paying that load itself
          (a model loaded or a vector pack built on the request thread), never because its
          process happened to be young.
          {servedSeries.unit === 'hour' &&
            ' At this resolution a point is often a handful of searches, sometimes one; hover for the count.'}
        </p>
        <LineChart
          series={servedSeries.series}
          labels={servedSeries.labels}
          measuredFrom={servedSeries.measuredFrom}
        />
        <Legend
          items={servedSeries.series.map((s) => ({
            label: s.label,
            color: s.color,
            muted: s.muted,
          }))}
        />
        {served && served.n_unknown_regime > 0 && (
          <p className="muted small">
            {served.n_unknown_regime} of {served.n} searches predate the stage probe and
            cannot be sorted into either regime.
          </p>
        )}
      </section>

      {/* One door is not a comparison, and every section here comes back
          independently — a report assembled without this one must render the rest
          rather than take the page down with it. */}
      {(served?.by_surface?.length ?? 0) > 1 && (
        <section>
          <h2>Which door they came through</h2>
          <p className="muted">
            Only the shared server warms at startup and keeps its models resident across
            calls. A stdio server and a terminal search are one process per call, so every
            search they serve is that process's first — cold by construction, and nothing
            a warm pass could fix. Read the cold column against the shared server's row;
            pooled with the others it is a chart of how often somebody used a one-shot.
          </p>
          <div className="stat-table-wrap">
            <table className="stat-table rv-surfaces">
              <thead>
                <tr>
                  <th>surface</th>
                  <th className="num">searches</th>
                  <th className="num">cold</th>
                  <th className="num">median</th>
                  <th className="num">slow 1 in 10</th>
                </tr>
              </thead>
              <tbody>
                {served!.by_surface.map((s) => (
                  <tr key={s.surface}>
                    <td>
                      <code>{s.surface}</code>
                      <span className="muted small"> {SURFACES[s.surface] ?? ''}</span>
                    </td>
                    <td className="num">{s.n}</td>
                    <td className="num">
                      {s.n_cold} <span className="muted">({Math.round((100 * s.n_cold) / s.n)}%)</span>
                    </td>
                    <td className="num">{ms(s.p50)}</td>
                    <td className="num">{ms(s.p90)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <h2>Where the time goes</h2>
        <p className="muted">
          Median per stage across served searches. The two arms — lexical (<code>fts_ms</code>)
          and semantic (<code>semantic_ms</code>) — run at the same time, so they do not add up
          to the total and neither is a share of it.
        </p>
        {stages?.stages?.length ? (
          <div className="stat-table-wrap">
            <table className="stat-table rv-stages">
              <thead>
                <tr>
                  <th>stage</th>
                  <th className="bar-col">median</th>
                  <th className="num">slow 1 in 10</th>
                  <th className="num">searches</th>
                </tr>
              </thead>
              <tbody>
                {stages.stages.map((s) => (
                  <tr key={s.stage}>
                    <td>
                      <code>{s.stage}</code>
                    </td>
                    <td className="bar-col">
                      <span className="stat-bar">
                        <span
                          className="stat-bar-fill"
                          style={{ width: `${topStage ? (100 * s.p50) / topStage.p50 : 0}%` }}
                        />
                      </span>
                      <span className="bar-num">{ms(s.p50)}</span>
                    </td>
                    <td className="num">{ms(s.p90)}</td>
                    <td className="num">{s.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No staged searches recorded in this window.</p>
        )}
        {stages && stages.n_unproven > 0 && (
          <p className="muted small">
            Known-cold searches are excluded; {stages.n_unproven} of {stages.n} predate the
            stage probe, so they are included without proof of being warm.
          </p>
        )}
      </section>

      <section>
        <h2>The bench</h2>
        <p className="muted">
          The same queries replayed under control — warm, pool cache off — so a change is
          comparable across days in a way served latency is not. <code>observed</code> is
          the queries agents actually ran, which is the only population measured here; a
          second query set would be a second line, never averaged into this one.
        </p>
        {benchSeries.length ? (
          <>
            <LineChart
              series={benchSeries}
              labels={Array.from({ length: benchLen }, (_, i) => String(i + 1))}
            />
            <Legend items={benchSeries.map((s) => ({ label: s.label, color: s.color }))} />
          </>
        ) : (
          <p className="muted">
            No bench runs recorded. <code>search_lab/latency_replay.py</code> writes this
            series.
          </p>
        )}
        <p className="muted small">
          Speed only, and that is a limit rather than a gap: every cheap way to make search
          faster is a way to make it worse, but the missing half is a quality number about
          this archive, and a relevance label about this archive would have to be made by
          searching it. The quality claims live on public benchmarks instead — the{' '}
          <a href="/lab">search lab</a> lists them.
        </p>
      </section>

      {restarts && restarts.buckets.length > 0 && (
        <section>
          <h2>Restarts</h2>
          <p className="muted">
            Process starts per {restarts.bucket}, and what they cost. This is here because
            it is the largest single influence on what agents feel: {ms(restarts.p50_ms)} of
            warm-up per start, {restarts.total_s.toFixed(0)}s across the window. Only
            {' '}{restarts.bucket}s with a start are listed.
            {(restarts.by_surface?.length ?? 0) > 0 && (
              <>
                {' '}Counted across every daemon that warms — this window:{' '}
                {restarts.by_surface.map((s) => `${s.surface} ${s.n}`).join(' · ')}.
              </>
            )}
          </p>
          <div className="stat-table-wrap">
            <table className="stat-table">
              <thead>
                <tr>
                  <th>{restarts.bucket}</th>
                  <th className="bar-col">starts</th>
                </tr>
              </thead>
              <tbody>
                {restarts.buckets.map((b) => (
                  <tr key={b.at}>
                    <td>{bucketLabel(b.at, restarts.bucket, true)}</td>
                    <td className="bar-col">
                      <span className="stat-bar">
                        <span
                          className="stat-bar-fill"
                          style={{
                            width: `${(100 * b.n) / Math.max(...restarts.buckets.map((x) => x.n))}%`,
                          }}
                        />
                      </span>
                      <span className="bar-num">{b.n}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
