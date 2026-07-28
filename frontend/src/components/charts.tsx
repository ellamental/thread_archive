import { useState } from 'react'
import type { ReactNode } from 'react'
import { catColor, seqColor } from '../chartColor'
import { colorStyle } from '../modelColor'
import type { ModelColor } from '../modelColor'

// Inline-SVG chart primitives for the stats page. Hand-rolled rather than pulled from a
// charting library: the viewer ships as a static bundle the watcher serves, and these
// four forms are less code than the dependency that would draw them.
//
// House rules, applied by every chart here so a page of them reads as one system:
//   · marks are thin, grid and axes are hairline and solid, and nothing is drawn that
//     isn't data or the scaffolding to read it;
//   · touching fills are separated by a gap in the panel color (GAP), never by a stroke
//     — a stroke is ink that isn't data;
//   · a column's top corners round (RADIUS), its baseline stays square;
//   · text wears text tokens, never a series color; identity comes from a colored swatch
//     beside the label. A stacked chart always ships its legend.
//   · every chart's numbers are also reachable as text — a tooltip is never the only way
//     to read a value.

const GAP = 2 // panel-colored gap between touching fills
const RADIUS = 4 // rounded data-end on a column
const MAX_BAR = 24 // a column never fills its band; the leftover is air

/** A rect with rounded top corners and a square base — the column mark. Collapses to a
 *  plain rect below twice the radius, where the rounding would swallow the bar. */
function columnPath(x: number, y: number, w: number, h: number, r = RADIUS): string {
  const rr = Math.min(r, w / 2, h)
  if (h <= rr * 2) return `M${x},${y} h${w} v${h} h${-w} Z`
  return `M${x},${y + rr} a${rr},${rr} 0 0 1 ${rr},${-rr} h${w - rr * 2} a${rr},${rr} 0 0 1 ${rr},${rr} v${h - rr} h${-w} Z`
}

/** Round a scale's top up to a 1/2/5×10ⁿ boundary, and the tick step with it, so the
 *  axis reads 0 / 50k / 100k rather than 0 / 47.3k / 94.6k. */
export function niceScale(max: number, ticks = 4): { max: number; step: number } {
  if (!(max > 0)) return { max: 1, step: 1 }
  const raw = max / ticks
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag
  return { max: Math.ceil(max / step) * step, step }
}

/** Month labels for a dense monthly axis: each January carries its year, and the two
 *  ends carry month + year. Every month between them is a tick without a name — 40 of
 *  them spelled out would overlap into a gray smear. */
function monthTicks(months: string[]): Array<{ i: number; label: string }> {
  const out: Array<{ i: number; label: string }> = []
  months.forEach((m, i) => {
    const isEnd = i === 0 || i === months.length - 1
    if (!isEnd && m.slice(5) !== '01') return
    // Keep an end label from landing on top of the January beside it.
    if (!isEnd && (i <= 1 || i >= months.length - 2)) return
    out.push({ i, label: isEnd ? fmtMonthShort(m) : m.slice(0, 4) })
  })
  return out
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** 'YYYY-MM' → 'Feb 2026', off the string: parsing it as a Date would shift the
 *  month-start instant across timezones and can name the month before. */
export function fmtMonthShort(month: string): string {
  const m = MONTH_NAMES[Number(month.slice(5, 7)) - 1]
  return m ? `${m} ${month.slice(0, 4)}` : month
}

export interface Series {
  key: string
  values: number[]
  /** Only a faceted series carries its own hue (a model's, from modelColor). Series
   *  stacked together take categorical slots in order, so a filter that drops one
   *  cannot repaint the rest. */
  color?: ModelColor
}

/** The identity key for a set of stacked series. Always rendered for two or more —
 *  color matching alone is never the only way to tell them apart. */
export function ChartLegend({ series }: { series: Series[] }) {
  return (
    <ul className="chart-legend">
      {series.map((s, i) => (
        <li key={s.key}>
          <span className="chart-swatch" style={{ background: catColor(i) }} aria-hidden="true" />
          {s.key}
        </li>
      ))}
    </ul>
  )
}

/** The hover readout. Rendered as HTML over the plot rather than as SVG text so it can
 *  wrap, and so it never has to fit inside the mark it describes. */
function Tooltip({ x, side, children }: { x: number; side: 'left' | 'right'; children: ReactNode }) {
  return (
    <div
      className={'chart-tip ' + side}
      style={{ left: side === 'left' ? `${x}%` : undefined, right: side === 'right' ? `${100 - x}%` : undefined }}
      role="status"
    >
      {children}
    </div>
  )
}

// ── stacked columns over a month axis ───────────────────────────────────────────
// Absolute or normalized to each month's own total (`share`). The two answer different
// questions over the same series — how much, and what the mix was — and a run that spans
// three orders of magnitude needs both: on the absolute chart the early months are a
// hairline, which is true but says nothing about what they were made of.

export function StackedMonths({
  months,
  series,
  share = false,
  fmt,
  unit,
  height = 190,
}: {
  months: string[]
  series: Series[]
  share?: boolean
  fmt: (n: number) => string
  unit: string
  height?: number
}) {
  const [hover, setHover] = useState<number | null>(null)
  const width = 720
  const padL = 44
  const padR = 8
  const padT = 10
  const padB = 22
  const plotW = width - padL - padR
  const plotH = height - padT - padB

  const totals = months.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] || 0), 0))
  const scale = share ? { max: 1, step: 0.25 } : niceScale(Math.max(1, ...totals))
  const band = plotW / Math.max(months.length, 1)
  const barW = Math.min(MAX_BAR, Math.max(3, band * 0.72))
  const x = (i: number) => padL + band * i + (band - barW) / 2
  const y = (v: number) => padT + plotH * (1 - v / scale.max)

  const ticks: number[] = []
  for (let t = 0; t <= scale.max + 1e-9; t += scale.step) ticks.push(t)

  const tipMonth = hover != null ? months[hover] : null
  const tipRows =
    hover == null
      ? []
      : series
          .map((s, si) => ({ s, si, v: s.values[hover] || 0 }))
          .filter((r) => r.v > 0)
          .reverse()

  return (
    <div className="chart-plot">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="chart-svg"
        role="img"
        aria-label={`${unit} per month, ${months[0]} to ${months[months.length - 1]}, split by ${series
          .map((s) => s.key)
          .join(', ')}`}
        onMouseLeave={() => setHover(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={width - padR} y1={y(t)} y2={y(t)} className="chart-grid" />
            <text x={padL - 7} y={y(t) + 3.5} className="chart-axis" textAnchor="end">
              {share ? `${Math.round(t * 100)}%` : fmt(t)}
            </text>
          </g>
        ))}

        {months.map((month, i) => {
          const total = totals[i]
          if (!total) return null
          const denom = share ? total : 1
          let acc = 0
          // Bottom-up, biggest series first, so a band keeps its place in every column.
          return (
            <g key={month} opacity={hover == null || hover === i ? 1 : 0.55}>
              {series.map((s, si) => {
                const v = (s.values[i] || 0) / denom
                if (v <= 0) return null
                const y0 = y(acc)
                acc += v
                const y1 = y(acc)
                const isTop = acc >= (share ? 1 : total) - 1e-9
                // The gap is taken off the *top* of every segment but the topmost, so
                // the stack still lands flush on the baseline.
                const h = Math.max(1, y0 - y1 - (isTop ? 0 : GAP))
                return (
                  <path
                    key={s.key}
                    d={columnPath(x(i), y1 + (isTop ? 0 : GAP), barW, h, isTop ? RADIUS : 0)}
                    fill={catColor(si)}
                    className="chart-fill"
                  />
                )
              })}
            </g>
          )
        })}

        {/* Hit targets are the full band and the full plot height — far bigger than the
            marks, so a 3px-wide early month is still catchable. */}
        {months.map((month, i) => (
          <rect
            key={month}
            x={padL + band * i}
            y={padT}
            width={band}
            height={plotH}
            fill="transparent"
            onMouseEnter={() => setHover(i)}
          >
            <title>{`${fmtMonthShort(month)}: ${fmt(totals[i])} ${unit}`}</title>
          </rect>
        ))}

        <line x1={padL} x2={width - padR} y1={y(0)} y2={y(0)} className="chart-axis-line" />
        {monthTicks(months).map(({ i, label }) => (
          <text
            key={label + i}
            x={padL + band * i + band / 2}
            y={height - 6}
            className="chart-axis"
            textAnchor={i === 0 ? 'start' : i === months.length - 1 ? 'end' : 'middle'}
          >
            {label}
          </text>
        ))}
      </svg>

      {tipMonth && (
        <Tooltip
          x={((padL + band * (hover as number) + band / 2) / width) * 100}
          side={(hover as number) > months.length * 0.6 ? 'right' : 'left'}
        >
          <div className="chart-tip-head">
            {fmtMonthShort(tipMonth)}
            <span>{fmt(totals[hover as number])} {unit}</span>
          </div>
          {tipRows.map(({ s, si, v }) => (
            <div className="chart-tip-row" key={s.key}>
              <span className="chart-swatch" style={{ background: catColor(si) }} aria-hidden="true" />
              <span className="chart-tip-key">{s.key}</span>
              <span className="chart-tip-val">
                {share
                  ? `${Math.round((v / totals[hover as number]) * 100)}%`
                  : fmt(v)}
              </span>
            </div>
          ))}
        </Tooltip>
      )}
    </div>
  )
}

// ── small multiples: one facet per series, shared axes ──────────────────────────
// The answer to a roster that turns over. Eight models on one plot converge into a
// thicket no legend can untangle — and four of them being Claudes, they would be four
// greens. Faceted, each model's run reads on its own while the shared y scale keeps the
// facets comparable, and identity comes from the label rather than the color.

export function SmallMultiples({
  months,
  series,
  fmt,
  unit,
  label,
}: {
  months: string[]
  series: Series[]
  fmt: (n: number) => string
  unit: string
  label: (s: Series) => ReactNode
}) {
  const width = 300
  const height = 46

  // Trim the axis to the span that has any data. The month range this shares with the
  // page's other charts starts years before the first source recorded a token, and
  // keeping those months would spend most of every panel drawing a flat line and cram
  // the whole roster into the last sliver. The span is stated below the panels, so the
  // shortened axis is declared rather than implied.
  const live = months.map((_, i) => series.some((s) => (s.values[i] || 0) > 0))
  const from = Math.max(live.indexOf(true), 0)
  const to = live.lastIndexOf(true)
  const span = months.slice(from, to + 1)
  const cut = series.map((s) => ({ ...s, window: s.values.slice(from, to + 1) }))

  const peak = Math.max(1, ...cut.flatMap((s) => s.window))
  const n = Math.max(span.length - 1, 1)
  const x = (i: number) => (i / n) * width
  const y = (v: number) => height * (1 - v / peak)

  return (
    <>
      <div className="chart-facets">
        {cut.map((s) => {
          const total = s.values.reduce((a, b) => a + b, 0)
          const top = s.window.reduce((a, b) => Math.max(a, b), 0)
          const topAt = s.window.indexOf(top)
          const line = s.window.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
          return (
            <figure className="chart-facet" key={s.key} style={s.color ? colorStyle(s.color) : undefined}>
              <figcaption>
                {label(s)}
                <span className="chart-facet-total">{fmt(total)}</span>
              </figcaption>
              <svg
                viewBox={`0 0 ${width} ${height}`}
                className="chart-facet-svg"
                role="img"
                aria-label={`${s.key}: ${fmt(total)} ${unit}, peaking at ${fmt(top)} in ${fmtMonthShort(
                  span[topAt] ?? '',
                )}`}
                preserveAspectRatio="none"
              >
                <path d={`${line} L${width},${height} L0,${height} Z`} className="chart-facet-fill" />
                <path d={line} className="chart-facet-line" fill="none" />
              </svg>
              {/* The one direct label a facet gets: its peak. The shared scale means a
                  quiet model's trace hugs the axis, and without this its numbers would
                  live only in the table. */}
              <div className="chart-facet-peak">
                peak {fmt(top)} · {fmtMonthShort(span[topAt] ?? '')}
              </div>
            </figure>
          )
        })}
      </div>
      <p className="chart-span">
        {fmtMonthShort(span[0] ?? '')} – {fmtMonthShort(span[span.length - 1] ?? '')}, left to
        right · shared scale, peak {fmt(peak)} {unit}
      </p>
    </>
  )
}

// ── distribution bars ───────────────────────────────────────────────────────────
// One series, so no legend — the heading names what is plotted. Every bar carries its
// count: eight of them is few enough that labelling all is legible, and it keeps the
// values readable without a tooltip.

export function DistBars({
  bars,
  markers = [],
  height = 168,
}: {
  bars: Array<{ label: string; count: number }>
  /** `at` is a fractional bar index, so a marker lands where the value actually falls
   *  inside its bucket rather than always at the bucket's middle. */
  markers?: Array<{ at: number; label: string }>
  height?: number
}) {
  const width = 720
  const padL = 8
  const padR = 8
  const padT = 34 // a band above the plot for the marker labels, clear of the bar values
  const padB = 34
  const plotW = width - padL - padR
  const plotH = height - padT - padB
  const max = Math.max(1, ...bars.map((b) => b.count))
  const band = plotW / Math.max(bars.length, 1)
  const barW = Math.min(MAX_BAR, band - GAP * 4)

  return (
    <div className="chart-plot">
      <svg viewBox={`0 0 ${width} ${height}`} className="chart-svg" role="img" aria-label="Sessions by token size">
        {/* Markers first, bars over them: the rule then reads as a pointer descending to
            the bar it names instead of a line ruled across it. */}
        {markers.map((m) => (
          <line
            key={m.label}
            x1={padL + band * (m.at + 0.5)}
            x2={padL + band * (m.at + 0.5)}
            y1={padT - 12}
            y2={padT + plotH}
            className="chart-marker"
          />
        ))}
        {bars.map((b, i) => {
          const h = (b.count / max) * plotH
          const bx = padL + band * i + (band - barW) / 2
          return (
            <g key={b.label}>
              <path d={columnPath(bx, padT + plotH - h, barW, Math.max(h, 1))} className="chart-fill accent" />
              <text x={bx + barW / 2} y={padT + plotH - h - 6} className="chart-value" textAnchor="middle">
                {b.count.toLocaleString()}
              </text>
              <text x={bx + barW / 2} y={height - 20} className="chart-axis" textAnchor="middle">
                {b.label}
              </text>
            </g>
          )
        })}
        <line x1={padL} x2={width - padR} y1={padT + plotH} y2={padT + plotH} className="chart-axis-line" />
        <text x={width / 2} y={height - 4} className="chart-axis-title" textAnchor="middle">
          tokens per session
        </text>
        {markers.map((m, i) => {
          const bx = padL + band * (m.at + 0.5)
          // Anchor away from the edge the marker is near, so a label at either end of
          // the axis stays inside the box.
          const end = m.at > bars.length - 1.5
          return (
            <text
              key={m.label}
              x={bx + (end ? -5 : 5)}
              // Two markers can land in the same bucket (a tight distribution); the
              // second one steps up a line rather than printing over the first.
              y={13 + (i % 2) * 13}
              className="chart-axis"
              textAnchor={end ? 'end' : 'start'}
            >
              {m.label}
            </text>
          )
        })}
      </svg>
    </div>
  )
}

// ── weekday × hour heatmap ──────────────────────────────────────────────────────
// A real table, not an SVG grid: the cells are colored by magnitude, and the row and
// column headers plus each cell's own text make it its own table view — so the value is
// never gated behind a hover, which is the failure mode a color-only grid usually has.

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export function RhythmGrid({ grid, max }: { grid: number[][]; max: number }) {
  return (
    <div className="chart-heat-wrap">
      <table className="chart-heat">
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">day</span>
            </th>
            {Array.from({ length: 24 }, (_, h) => (
              <th scope="col" key={h}>
                {h % 3 === 0 ? h : ''}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.map((row, d) => (
            <tr key={DAYS[d]}>
              <th scope="row">{DAYS[d]}</th>
              {row.map((n, h) => (
                <td key={h}>
                  <span
                    className="chart-cell"
                    style={{ background: seqColor(max ? n / max : 0) }}
                    title={`${DAYS[d]} ${String(h).padStart(2, '0')}:00 — ${n.toLocaleString()} ${
                      n === 1 ? 'session' : 'sessions'
                    }`}
                  >
                    <span className="sr-only">{n}</span>
                  </span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="chart-scale">
        <span>none</span>
        {Array.from({ length: 7 }, (_, i) => (
          <span key={i} className="chart-cell scale" style={{ background: seqColor(i / 6) }} aria-hidden="true" />
        ))}
        <span>{max.toLocaleString()}</span>
      </div>
    </div>
  )
}
