import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type ModelStats } from '../api'
import { hueStyle, modelHue } from '../modelColor'
import { Bar, Tile, fmtInt, fmtTokens, fmtUsd } from './StatsView'

// One model's drill-down, reached from the stats page's model list: how much it
// was used over time (a monthly series), what a session with it looks like
// (min/median/avg/max tokens), how often its sessions compacted, and the heaviest
// sessions themselves — each a link into the reader. Sessions are conversations
// where this model answered at least one request; token/cost figures are the
// model's own share of each, so a mixed-model session doesn't inflate it.

// 'YYYY-MM' → 'Feb 2026', straight off the string: parsing it as a Date would
// shift the month-start instant across timezones.
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function fmtMonth(month: string): string {
  const m = MONTH_NAMES[Number(month.slice(5, 7)) - 1]
  return m ? `${m} ${month.slice(0, 4)}` : month
}

// Session timestamps arrive as SQLite's 'YYYY-MM-DD HH:MM:SS[.ffffff]'; the 'T'
// makes them Date-parseable everywhere.
function fmtDay(at: string | null): string {
  if (!at) return ''
  const d = new Date(at.replace(' ', 'T'))
  return isNaN(d.getTime())
    ? at.slice(0, 10)
    : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function MonthTable({ rows, hue, anyCost }: { rows: ModelStats['by_month']; hue: number; anyCost: boolean }) {
  const maxTok = Math.max(1, ...rows.map((r) => r.tokens))
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>month</th>
            <th className="num">sessions</th>
            <th className="bar-col">tokens</th>
            <th className="num">avg / session</th>
            <th className="num">requests</th>
            <th className="num">compactions</th>
            {anyCost && <th className="num">cost</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.month}>
              <td>{fmtMonth(r.month)}</td>
              <td className="num">{r.sessions ? fmtInt(r.sessions) : '—'}</td>
              <td className="bar-col">
                <Bar frac={r.tokens / maxTok} hue={hue} />
                <span className="bar-num">{r.tokens ? fmtTokens(r.tokens) : '—'}</span>
              </td>
              <td className="num">{r.avg_tokens ? fmtTokens(r.avg_tokens) : '—'}</td>
              <td className="num">{r.requests ? fmtInt(r.requests) : '—'}</td>
              <td className="num">{r.compactions ? fmtInt(r.compactions) : '—'}</td>
              {anyCost && <td className="num">{fmtUsd(r.cost)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function SessionTable({ rows, hue }: { rows: ModelStats['top_sessions']; hue: number }) {
  const maxTok = Math.max(1, ...rows.map((r) => r.tokens))
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>session</th>
            <th>when</th>
            <th>source</th>
            <th className="bar-col">tokens</th>
            <th className="num">requests</th>
            <th className="num">compactions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.thread_id}>
              <td className="session-cell">
                <Link className="session-link" to={'/archive/' + r.thread_id}>
                  {r.title || 'thread ' + r.thread_id}
                </Link>
              </td>
              <td>{fmtDay(r.at)}</td>
              <td>{r.source}</td>
              <td className="bar-col">
                <Bar frac={r.tokens / maxTok} hue={hue} />
                <span className="bar-num">{r.tokens ? fmtTokens(r.tokens) : '—'}</span>
              </td>
              <td className="num">{fmtInt(r.requests)}</td>
              <td className="num">{r.compactions ? fmtInt(r.compactions) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ModelStatsView() {
  const { model = '' } = useParams()
  const [data, setData] = useState<ModelStats | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setData(null)
    setErr(null)
    api.modelStats(model).then(setData).catch((e) => setErr(String(e.message ?? e)))
  }, [model])

  if (err) return <div className="empty">model stats unavailable: {err}</div>
  if (!data) return <div className="empty">surveying {model}…</div>

  const o = data.overview
  const p = data.per_session
  const hue = modelHue(data.model)
  const span = [fmtDay(o.first_at), fmtDay(o.last_at)].filter(Boolean)
  const spanLabel = span.length === 2 && span[0] !== span[1] ? `${span[0]} – ${span[1]}` : span[0] || ''

  return (
    <div className="wrap">
      <div className="submeta">
        <Link className="session-link" to="/stats">
          ← stats
        </Link>
      </div>
      <h1 className="title">
        <span className="model-tag title-tag" style={hueStyle(hue)}>
          {data.model}
        </span>
      </h1>
      <div className="submeta">{[spanLabel, `${fmtInt(o.conversations)} sessions`].filter(Boolean).join(' · ')}</div>

      <div className="stat-tiles">
        <Tile label="sessions" value={fmtInt(o.conversations)} />
        <Tile
          label="tokens"
          value={fmtTokens(o.tokens)}
          sub={`${fmtTokens(o.input_tokens)} in · ${fmtTokens(o.output_tokens)} out`}
        />
        <Tile
          label="requests"
          value={fmtInt(o.requests)}
          sub={p.avg_requests != null ? `~${Math.round(p.avg_requests)} / session` : undefined}
        />
        <Tile
          label="tokens / session"
          value={p.avg_tokens != null ? fmtTokens(p.avg_tokens) + ' avg' : '—'}
          sub={`min ${fmtTokens(p.min_tokens)} · median ${
            p.median_tokens != null ? fmtTokens(p.median_tokens) : '—'
          } · max ${fmtTokens(p.max_tokens)}`}
        />
        <Tile
          label="compactions"
          value={fmtInt(o.compactions)}
          sub={
            o.conversations && o.compactions
              ? `~${(o.compactions / o.conversations).toFixed(o.compactions / o.conversations < 10 ? 1 : 0)} / session`
              : 'none recorded'
          }
        />
        {o.cost != null && (
          <Tile label="known cost" value={fmtUsd(o.cost)} sub={`over ${fmtInt(o.cost_conversations)} sessions`} />
        )}
      </div>

      <div className="stat-section">
        <h2 className="stat-h">By month</h2>
        <MonthTable rows={data.by_month} hue={hue} anyCost={data.by_month.some((r) => r.cost != null && r.cost > 0)} />
        <p className="stat-note">
          Sessions land in the month they started; compactions in the month they happened. In a
          session that mixed models, tokens and requests are this model’s share, while compactions
          count for every model that took part.
        </p>
      </div>

      {data.top_sessions.length > 0 && (
        <div className="stat-section">
          <h2 className="stat-h">Heaviest sessions</h2>
          <SessionTable rows={data.top_sessions} hue={hue} />
        </div>
      )}
    </div>
  )
}
