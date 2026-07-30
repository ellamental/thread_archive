import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import {
  api,
  type Stats,
  type StatsModel,
  type StatsSessionSizes,
  type StatsSource,
  type StatsTimeline,
} from '../api'
import { assignColors, colorStyle } from '../modelColor'
import type { ModelColor } from '../modelColor'
import { ChartLegend, DistBars, RhythmGrid, SmallMultiples, StackedMonths } from './charts'
import type { Series } from './charts'

// The stats page: token & cost analytics over the whole archive, read from an
// incrementally-maintained rollup (server-side). Cost is only present for the
// pay-per-token sources that record it; subscription tools log tokens but no
// dollar figure, so their cost shows as '—' rather than a fabricated 0.
// The formatting helpers and table furniture are shared with the per-model
// drill-down (ModelStatsView), which lives off this page's model links.
//
// Charts and tables are paired deliberately, each chart above the table that spells it
// out: the chart carries the shape over time, the table carries the exact numbers, and
// no value on this page is reachable only by hovering something.

export const fmtInt = (n: number): string => n.toLocaleString()

export function fmtTokens(n: number): string {
  const trim = (s: string): string => s.replace(/\.0$/, '')
  if (n >= 1e9) return trim((n / 1e9).toFixed(1)) + 'B'
  if (n >= 1e6) return trim((n / 1e6).toFixed(1)) + 'M'
  if (n >= 1e3) return Math.round(n / 1e3) + 'k'
  return String(n)
}

// Totals read as plain dollars; sub-dollar per-session averages need more places
// to not collapse to $0.00, so `precise` widens them.
export function fmtUsd(n: number | null | undefined, precise = false): string {
  if (n == null) return '—'
  if (n === 0) return '$0'
  if (!precise) return '$' + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return '$' + n.toFixed(n < 1 ? 4 : 2)
}

function fmtMonth(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short' })
}

// A proportional fill bar (0..1 of the row's value against the column max).
export function Bar({ frac, color }: { frac: number; color?: ModelColor }) {
  const pct = Math.max(frac <= 0 ? 0 : 2, Math.min(100, frac * 100)) // floor a nonzero value so it's visible
  return (
    <span className="stat-bar">
      <span
        className={'stat-bar-fill' + (color ? ' model' : '')}
        style={{ width: pct + '%', ...(color ? colorStyle(color) : {}) }}
      />
    </span>
  )
}

export function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat-tile">
      <div className="n">{value}</div>
      <div className="l">{label}</div>
      {sub && <div className="s">{sub}</div>}
    </div>
  )
}

// A long table opens showing this many rows; the rest are behind the toggle. The
// archive's model list runs to dozens of entries and buried the charts under it.
const COLLAPSED_ROWS = 10

/** Cut a sorted list to its head, with the state and the label for putting the tail
 *  back. Everything a table derives from *all* its rows — bar scales, color assignment,
 *  which columns exist — must keep reading the full list: a value that shifts when the
 *  tail appears makes the two states disagree about the same data. */
function useCollapsed<T>(rows: T[], limit = COLLAPSED_ROWS) {
  const [open, setOpen] = useState(false)
  const hidden = Math.max(rows.length - limit, 0)
  return { shown: open || !hidden ? rows : rows.slice(0, limit), open, hidden, setOpen }
}

function ShowMore({
  open,
  hidden,
  noun,
  onToggle,
}: {
  open: boolean
  hidden: number
  noun: string
  onToggle: () => void
}) {
  if (!hidden) return null
  return (
    <button type="button" className="show-more" aria-expanded={open} onClick={onToggle}>
      {open ? 'Show fewer' : `Show ${fmtInt(hidden)} more ${hidden === 1 ? noun : noun + 's'}`}
    </button>
  )
}

function ProviderTable({ rows }: { rows: StatsSource[] }) {
  const maxTok = Math.max(1, ...rows.map((r) => r.tokens))
  const anyCost = rows.some((r) => r.cost != null && r.cost > 0)
  const { shown, open, hidden, setOpen } = useCollapsed(rows)
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>provider</th>
            <th className="num">sessions</th>
            <th className="bar-col">tokens</th>
            <th className="num">cached reads</th>
            <th className="num">avg / session</th>
            {anyCost && <th className="num">cost</th>}
            {anyCost && <th className="num">avg / session</th>}
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={r.source}>
              <td>{r.source}</td>
              <td className="num">{fmtInt(r.conversations)}</td>
              <td className="bar-col">
                <Bar frac={r.tokens / maxTok} />
                <span className="bar-num">{r.tokens ? fmtTokens(r.tokens) : '—'}</span>
              </td>
              <td className="num">{r.cache_read_tokens ? fmtTokens(r.cache_read_tokens) : '—'}</td>
              <td className="num">{r.avg_tokens ? fmtTokens(r.avg_tokens) : '—'}</td>
              {anyCost && <td className="num">{fmtUsd(r.cost)}</td>}
              {anyCost && <td className="num">{fmtUsd(r.avg_cost, true)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <ShowMore open={open} hidden={hidden} noun="provider" onToggle={() => setOpen(!open)} />
    </div>
  )
}

function ModelTable({ rows }: { rows: StatsModel[] }) {
  const maxReq = Math.max(1, ...rows.map((r) => r.requests))
  // Colored as a set: same-family models keep the family hue and separate by shade,
  // so a table of five opuses reads as five greens rather than five identical chips.
  // Assigned over every row, not the visible ones — collisions resolve in list order, so
  // colors drawn from the truncated list would shuffle the moment the tail appeared.
  const colors = assignColors(rows.map((r) => r.model))
  const anyCost = rows.some((r) => r.cost != null && r.cost > 0)
  const { shown, open, hidden, setOpen } = useCollapsed(rows)
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>model</th>
            <th className="bar-col">requests</th>
            <th className="num">tokens</th>
            <th className="num">cached reads</th>
            <th className="num">conversations</th>
            {anyCost && <th className="num">cost</th>}
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => (
            <tr key={r.model}>
              <td>
                <Link className="model-link" to={'/stats/model/' + encodeURIComponent(r.model)}>
                  <span className="model-tag" style={colorStyle(colors[r.model])}>
                    {r.model}
                  </span>
                </Link>
              </td>
              <td className="bar-col">
                <Bar frac={r.requests / maxReq} color={colors[r.model]} />
                <span className="bar-num">{fmtInt(r.requests)}</span>
              </td>
              <td className="num">{r.tokens ? fmtTokens(r.tokens) : '—'}</td>
              <td className="num">{r.cache_read_tokens ? fmtTokens(r.cache_read_tokens) : '—'}</td>
              <td className="num">{fmtInt(r.conversations)}</td>
              {anyCost && <td className="num">{fmtUsd(r.cost)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <ShowMore open={open} hidden={hidden} noun="model" onToggle={() => setOpen(!open)} />
    </div>
  )
}

// A histogram bucket's axis label. The open-ended top bucket reads '1M+'; the first
// starts at zero and reads as a ceiling.
function bucketLabel(b: StatsSessionSizes['buckets'][number], i: number): string {
  if (b.hi == null) return fmtTokens(b.lo) + '+'
  if (i === 0) return '<' + fmtTokens(b.hi)
  return fmtTokens(b.lo) + '–' + fmtTokens(b.hi)
}

// Where a value sits on the bucket axis, as a fractional bar index — the bucket it falls
// in, plus how far into it. Interpolated on a log scale because the buckets are: a p90 of
// 135k belongs at the near edge of 100k–500k, not at its midpoint.
function bucketPosition(buckets: StatsSessionSizes['buckets'], v: number): number {
  const i = buckets.findIndex((b) => b.hi == null || v <= b.hi)
  if (i < 0) return buckets.length - 1
  const { lo, hi } = buckets[i]
  if (hi == null || hi <= lo) return i
  const l = Math.log10(Math.max(lo, 1))
  return i + Math.min(1, Math.max(0, (Math.log10(Math.max(v, 1)) - l) / (Math.log10(hi) - l)))
}

function Activity({ timeline }: { timeline: StatsTimeline }) {
  const series: Series[] = timeline.conversations_by_source.map((s) => ({ key: s.key, values: s.values }))
  return (
    <div className="stat-section">
      <h2 className="stat-h">Activity</h2>
      <ChartLegend series={series} />
      <div className="chart-card">
        <h3 className="chart-title">Conversations per month</h3>
        <StackedMonths months={timeline.months} series={series} fmt={fmtInt} unit="conversations" />
      </div>
      <div className="chart-card">
        <h3 className="chart-title">Share of each month</h3>
        <StackedMonths months={timeline.months} series={series} share fmt={fmtInt} unit="conversations" />
      </div>
      <p className="stat-note">
        A conversation counts in the month it started. The run spans three orders of
        magnitude, so the same series are drawn twice: the first chart has the volume,
        which flattens the early years into a hairline, and the second has the mix, which
        is where those years are legible. Only the busiest few providers get their own
        band — the rest are “other” here and listed one by one in the table below.{' '}
        {timeline.undated > 0 &&
          `${fmtInt(timeline.undated)} conversations carry no events and so have no date; they are in the totals above but not on this axis.`}
      </p>
    </div>
  )
}

function ModelTimeline({ timeline }: { timeline: StatsTimeline }) {
  const colors = assignColors(timeline.tokens_by_model.map((s) => s.key))
  const series: Series[] = timeline.tokens_by_model.map((s) => ({
    key: s.key,
    values: s.values,
    color: colors[s.key],
  }))
  return (
    <div className="stat-section">
      <h2 className="stat-h">Model roster over time</h2>
      <SmallMultiples
        months={timeline.months}
        series={series}
        fmt={fmtTokens}
        unit="tokens"
        label={(s) =>
          s.key === 'other' ? (
            <span className="chart-facet-name">other models</span>
          ) : (
            <Link className="model-link" to={'/stats/model/' + encodeURIComponent(s.key)}>
              <span className="model-tag" style={colorStyle(colors[s.key])}>
                {s.key}
              </span>
            </Link>
          )
        }
      />
      <p className="stat-note">
        Tokens per month, one panel per model, on a shared scale — so the panels compare
        against each other and a model’s whole run reads at once: when it arrived, how
        long it carried the load, when it was retired. Each panel’s busiest month is
        labelled, since a quiet model traces along the axis at this scale.
      </p>
    </div>
  )
}

function SessionSizes({ sizes }: { sizes: StatsSessionSizes }) {
  const bars = sizes.buckets.map((b, i) => ({ label: bucketLabel(b, i), count: b.count }))
  const markers = [
    sizes.median != null && {
      at: bucketPosition(sizes.buckets, sizes.median),
      label: `median ${fmtTokens(sizes.median)}`,
    },
    sizes.p90 != null && { at: bucketPosition(sizes.buckets, sizes.p90), label: `p90 ${fmtTokens(sizes.p90)}` },
  ].filter(Boolean) as Array<{ at: number; label: string }>
  return (
    <div className="stat-section">
      <h2 className="stat-h">How big a session gets</h2>
      <div className="chart-card">
        <DistBars bars={bars} markers={markers} />
      </div>
      <p className="stat-note">
        {fmtInt(sizes.sessions)} sessions that recorded token usage. Buckets step by
        magnitude rather than by an even width, so the bars are counts per range, not a
        density.{' '}
        {sizes.without_tokens > 0 &&
          `${fmtInt(sizes.without_tokens)} more sessions logged no usage at all — a web export carries its messages but no token counts — and are left out rather than binned as zero.`}
      </p>
    </div>
  )
}

export function StatsView() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.stats().then(setStats).catch((e) => setErr(String(e.message ?? e)))
  }, [])

  if (err) return <div className="empty">stats unavailable: {err}</div>
  if (!stats) return <div className="empty">crunching the archive… (the first pass surveys every conversation)</div>

  const o = stats.overview
  const span = [fmtMonth(o.first_at), fmtMonth(o.last_at)].filter(Boolean)
  const spanLabel = span.length === 2 && span[0] !== span[1] ? `${span[0]} – ${span[1]}` : span[0] || ''

  return (
    <div className="wrap">
      <h1 className="title">Stats</h1>
      <div className="submeta">
        {[spanLabel, `${fmtInt(o.conversations)} conversations across ${o.sources} sources`]
          .filter(Boolean)
          .join(' · ')}
      </div>

      <div className="stat-tiles">
        <Tile label="conversations" value={fmtInt(o.conversations)} />
        <Tile
          label="tokens"
          value={fmtTokens(o.tokens)}
          sub={`${fmtTokens(o.input_tokens)} in · ${fmtTokens(o.cache_read_tokens)} cached · ${fmtTokens(o.output_tokens)} out`}
        />
        <Tile label="known cost" value={fmtUsd(o.cost)} sub={o.cost_conversations ? `over ${fmtInt(o.cost_conversations)} sessions` : 'none recorded'} />
        <Tile label="models" value={fmtInt(o.models)} />
      </div>

      {stats.timeline.months.length > 1 && <Activity timeline={stats.timeline} />}

      <div className="stat-section">
        <h2 className="stat-h">By provider</h2>
        <ProviderTable rows={stats.by_source} />
        <p className="stat-note">
          Token totals are uncached input plus output; cached reads are shown
          separately.{' '}
          Cost is only recorded by pay-per-token sources; subscription tools log
          tokens but no dollar figure, so their cost reads “—”.
        </p>
      </div>

      {stats.timeline.tokens_by_model.length > 0 && <ModelTimeline timeline={stats.timeline} />}

      {stats.by_model.length > 0 && (
        <div className="stat-section">
          <h2 className="stat-h">By model</h2>
          <ModelTable rows={stats.by_model} />
        </div>
      )}

      {stats.session_sizes.sessions > 0 && <SessionSizes sizes={stats.session_sizes} />}

      {stats.rhythm.total > 0 && (
        <div className="stat-section">
          <h2 className="stat-h">When the work happens</h2>
          <div className="chart-card">
            <RhythmGrid grid={stats.rhythm.grid} max={stats.rhythm.max} />
          </div>
          <p className="stat-note">
            Conversation starts by weekday and hour, in local time — {fmtInt(stats.rhythm.total)}{' '}
            sessions, busiest hour {fmtInt(stats.rhythm.max)}. Every cell carries its own
            count for a reader who can’t use the shading.
          </p>
        </div>
      )}
    </div>
  )
}
