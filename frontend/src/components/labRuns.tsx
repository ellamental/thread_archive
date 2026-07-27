import { Link } from 'react-router-dom'
import type { BenchRunRecord } from '../api'

// The benchmark ledger, as both lab pages read it.
//
// The inventory's benchmark table answers "what does the bench say *now*", and
// to answer it keeps one run per row — the newest successful one. This is the
// other half: every run that ever happened on this box, including the three
// kinds a newest-per-row summary has to drop. The passes before the newest one
// (a tuning number means nothing without the pass it moved from), the failures
// (a row that stopped being runnable reads as a silent gap otherwise), and the
// rows that have since left the manifest, which were still measured here and
// have nowhere else to be read.

/** The lab's status chip. Tones are the four the inventory uses, so a state on
 *  the run history reads the same as the equivalent state on the summary.
 *  `title` carries the hover gloss for a chip whose one word needs one. */
export function Pill({
  tone,
  title,
  children,
}: {
  tone: string
  title?: string
  children: React.ReactNode
}) {
  return (
    <span className={`lab-pill ${tone}`} title={title}>
      {children}
    </span>
  )
}

/** A latency in milliseconds, at the precision the number carries: sub-10ms
 *  stages separate on a tenth, and a second-long query does not. */
export function ms(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`
}

/** A recorded measure. A count is an integer, and printing it as `5.000` claims
 *  a precision it does not have; a rate in [0,1] needs three decimals for two
 *  tuning passes to read apart, and a millisecond figure does not. */
export function measure(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  if (Number.isInteger(v)) return v.toLocaleString()
  return Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3)
}

/** A movement between two runs, always signed — an unsigned `0.004` sitting
 *  beside a score reads as another measurement rather than a change. */
export function delta(now: number, before: number): string {
  const d = now - before
  const digits = Math.abs(now) >= 100 || Number.isInteger(now) ? 1 : 3
  return (d > 0 ? '+' : '') + d.toFixed(digits)
}

/** How long a run took. Seconds below a minute and a half — the lexical rows
 *  finish inside one, and `<1 min` would be the whole number — minutes above it,
 *  where the tenth is what varies between passes. */
export function elapsed(s: number | null | undefined): string {
  if (typeof s !== 'number' || !Number.isFinite(s)) return '—'
  if (s < 90) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`
  if (s < 5400) return `${(s / 60).toFixed(1)} min`
  return `${(s / 3600).toFixed(1)} hr`
}

/** A run's instant in the reader's own zone. The ledger writes UTC, but a run is
 *  read against the afternoon it happened in — the recorded instant stays
 *  exactly available as the cell's title. */
export function when(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

/** The run a delta should be read against: the newest *earlier* success of the
 *  same row under **different** code.
 *
 *  Not simply the previous run. A tuning loop measures the same configuration
 *  more than once — a forced pass, a re-run after an unrelated edit — and a delta
 *  against an identical configuration is zero, which reads as "the change did
 *  nothing" rather than "this is the same measurement twice". Mirrors the runner's
 *  own `previous_configuration`, so the page and the terminal agree.
 *
 *  `runs` must be the newest-first order the ledger returns; the first match
 *  after this run's position is therefore the nearest earlier one. */
export function previousConfiguration(
  run: BenchRunRecord,
  runs: BenchRunRecord[],
): BenchRunRecord | null {
  const at = runs.findIndex((r) => r.id === run.id)
  if (at < 0) return null
  return (
    runs
      .slice(at + 1)
      .find(
        (r) => r.row === run.row && r.status === 'ok' && r.code_id !== run.code_id,
      ) ?? null
  )
}

/** What one run *is*, which is not the same question as whether it succeeded.
 *
 *  `reported` is the run whose numbers the benchmark table above is showing —
 *  the newest success of a row still on the bench. Everything else that
 *  succeeded is history: real measurements, superseded by a later pass. A
 *  failure stays visible as a failure, because "this row stopped being runnable
 *  at this configuration" is the fact a summary of successes cannot state. */
export function RunState({ run }: { run: BenchRunRecord }) {
  if (run.status !== 'ok') return <Pill tone="bad">{run.status}</Pill>
  if (run.current && run.on_bench) return <Pill tone="good">reported</Pill>
  return <Pill tone="idle">superseded</Pill>
}

/** The configuration a run measured, short enough for a cell.
 *
 *  The hash is the whole point of the ledger's freshness test, so it is what
 *  identifies a configuration here too — the commit is only the human label, and
 *  during a tuning loop several configurations share one. `in tree` marks the
 *  run measured under the ranking code as it sits right now: the only runs whose
 *  numbers describe what a run today would produce. */
function Config({ run }: { run: BenchRunRecord }) {
  if (!run.code_id) return <span className="muted">—</span>
  return (
    <span className="lab-config">
      <code>{run.code_id.slice(0, 8)}</code>
      {run.code_current && <span className="lab-intree">in tree</span>}
    </span>
  )
}

/** A run's headline numbers — the metrics its row declares, in the row's own
 *  order, so a run reads with the same figures the summary shows it with.
 *  Capped: the full set is on the run's own page, and a cell wide enough for
 *  eight of them makes the column it shares unreadable. */
function Headline({ run, max = 3 }: { run: BenchRunRecord; max?: number }) {
  const keys = run.measure_keys.filter((k) => typeof run.measures[k] === 'number')
  if (!keys.length) return <span className="muted">—</span>
  return (
    <span className="lab-measures">
      {keys.slice(0, max).map((k) => (
        <span key={k}>
          <span className="muted">{k}</span> {measure(run.measures[k])}
        </span>
      ))}
      {keys.length > max && <span className="muted">+{keys.length - max}</span>}
    </span>
  )
}

/** The ledger as a table, newest first. Every row links to the run itself, which
 *  is where the measures the headline dropped, the invocation and the corpus it
 *  ran against live. `scoped` drops the row column for a table already showing
 *  one row's history — repeating the name down every line of it says nothing. */
export function RunsTable({
  runs,
  scoped = false,
  activeId,
}: {
  runs: BenchRunRecord[]
  scoped?: boolean
  activeId?: string
}) {
  if (!runs.length)
    return <p className="muted">No runs recorded on this box yet.</p>
  return (
    <div className="stat-table-wrap">
      <table className="stat-table lab-runs-table">
        <thead>
          <tr>
            <th>when</th>
            {!scoped && <th>row</th>}
            <th>state</th>
            <th className="num">cost</th>
            <th className="num">p50</th>
            <th className="num">p99</th>
            <th>measured</th>
            <th>config</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className={run.id === activeId ? 'active' : undefined}>
              <td>
                <Link className="lab-run-link" to={`/lab/run/${run.id}`} title={run.at}>
                  {when(run.at)}
                </Link>
              </td>
              {!scoped && (
                <td>
                  <code>{run.row}</code>
                  {!run.on_bench && (
                    <div className="lab-sub">no longer a row on the bench</div>
                  )}
                </td>
              )}
              <td>
                <RunState run={run} />
              </td>
              <td className="num">{elapsed(run.elapsed_s)}</td>
              {/* Query latency beside the score, in the ledger's own list. A
                  configuration is a pair of results — what it found and what it
                  cost — and a history showing only the first hides every trade
                  made to get there. p99 rather than p95: the tail is where a
                  ranking change shows up first. */}
              <td className="num">{ms(run.performance?.total?.p50)}</td>
              <td className="num">{ms(run.performance?.total?.p99)}</td>
              <td>
                <Headline run={run} />
              </td>
              <td>
                <Config run={run} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
