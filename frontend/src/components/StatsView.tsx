import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Stats, type StatsModel, type StatsSource } from '../api'
import { hueStyle, modelHue } from '../modelColor'

// The stats page: token & cost analytics over the whole archive, read from an
// incrementally-maintained rollup (server-side). Cost is only present for the
// pay-per-token sources that record it; subscription tools log tokens but no
// dollar figure, so their cost shows as '—' rather than a fabricated 0.
// The formatting helpers and table furniture are shared with the per-model
// drill-down (ModelStatsView), which lives off this page's model links.

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
export function Bar({ frac, hue }: { frac: number; hue?: number }) {
  const pct = Math.max(frac <= 0 ? 0 : 2, Math.min(100, frac * 100)) // floor a nonzero value so it's visible
  return (
    <span className="stat-bar">
      <span
        className={'stat-bar-fill' + (hue != null ? ' model' : '')}
        style={{ width: pct + '%', ...(hue != null ? hueStyle(hue) : {}) }}
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

function ProviderTable({ rows }: { rows: StatsSource[] }) {
  const maxTok = Math.max(1, ...rows.map((r) => r.tokens))
  const anyCost = rows.some((r) => r.cost != null && r.cost > 0)
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
          {rows.map((r) => (
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
    </div>
  )
}

function ModelTable({ rows }: { rows: StatsModel[] }) {
  const maxReq = Math.max(1, ...rows.map((r) => r.requests))
  const anyCost = rows.some((r) => r.cost != null && r.cost > 0)
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
          {rows.map((r) => (
            <tr key={r.model}>
              <td>
                <Link className="model-link" to={'/stats/model/' + encodeURIComponent(r.model)}>
                  <span className="model-tag" style={hueStyle(modelHue(r.model))}>
                    {r.model}
                  </span>
                </Link>
              </td>
              <td className="bar-col">
                <Bar frac={r.requests / maxReq} hue={modelHue(r.model)} />
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

      {stats.by_model.length > 0 && (
        <div className="stat-section">
          <h2 className="stat-h">By model</h2>
          <ModelTable rows={stats.by_model} />
        </div>
      )}
    </div>
  )
}
