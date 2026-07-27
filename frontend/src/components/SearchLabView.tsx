import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type Benchmark,
  type BenchRuns,
  type Dataset,
  type DirSize,
  type FunnelRow,
  type LabInventory,
} from '../api'
import { Link } from 'react-router-dom'
import { Pill, RunsTable } from './labRuns'

// What the bench has to measure with. Every other view here is about the corpus
// or the pipeline reading it; this one is about the instruments — which benchmark
// rows can run on this box right now, which corpora are on disk, which miners
// exist.
//
// Tables rather than charts, because nothing on this page is a series: it is an
// inventory, and the questions it answers ("can this row run?", "is that corpus
// built?", "what fixed those labels?") are categorical.

/** Bytes, or `—`. A truncated walk prints as a floor: the number is real but the
 *  total is larger by however much the walk did not reach, and `≥` is the only
 *  honest way to show a size that stopped early. */
function bytes(size: DirSize | undefined): string {
  const n = size?.bytes
  if (!n) return '—'
  const units: [string, number][] = [
    ['GB', 1e9],
    ['MB', 1e6],
    ['KB', 1e3],
  ]
  const [unit, scale] = units.find(([, s]) => n >= s) ?? ['B', 1]
  const value = scale === 1 ? String(n) : (n / scale).toFixed(n / scale < 10 ? 1 : 0)
  return `${size?.truncated ? '≥' : ''}${value} ${unit}`
}

function count(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

function minutes(m: number): string {
  if (m < 1) return '<1 min'
  if (m < 90) return `${Math.round(m)} min`
  return `${(m / 60).toFixed(1)} hr`
}

function day(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : '—'
}

/** How each benchmark state reads, and the tone it carries. `missing` is the only
 *  one that is not about numbers being current — the row cannot run at all. */
const BENCH_STATE: Record<string, { label: string; tone: string; note: string }> = {
  fresh: {
    label: 'fresh',
    tone: 'good',
    note: 'measured at this configuration — a run would skip it',
  },
  stale: {
    label: 'stale',
    tone: 'warn',
    note: 'ranking or scoring code has changed since it last ran',
  },
  'never-run': { label: 'never run', tone: 'busy', note: 'corpus is built, no run recorded' },
  missing: { label: 'no corpus', tone: 'bad', note: 'the corpus is not on this box' },
}

/** A mining run's funnel, drawn.
 *
 *  The one chart on this page, and it earns the exception: a funnel is the shape
 *  of a *loss*, and a table of in/out pairs makes the reader do the subtraction
 *  that is the entire point. Each bar is a stage's survivors as a share of what
 *  entered the funnel, so the taper is the yield and the gaps are where units
 *  went.
 *
 *  Drop reasons are listed rather than colour-coded because they are not ordinal
 *  and there are usually two or three: a unit dropped as `misattributed` is a
 *  wrong label leaving the benchmark, and one dropped as `untargetable-commit` is
 *  a hard case leaving it. Reading those as one "dropped" number is the mistake
 *  this whole view exists to prevent. */
function Funnel({ rows }: { rows: FunnelRow[] }) {
  const start = rows[0]?.in ?? 0
  if (!start) return null
  return (
    <ol className="mine-funnel" aria-label="mining funnel">
      {rows.map((row) => {
        const drops = Object.entries(row.reasons).filter(([r]) => r !== 'ok')
        return (
          <li key={row.stage}>
            <div className="mine-funnel-head">
              <code>{row.stage}</code>
              {row.kind === 'agent' && (
                <Pill tone="warn" title="this stage spends tokens">
                  agent
                </Pill>
              )}
              <span className="muted small">
                {count(row.in)} → {count(row.out)}
                {row.in > row.out && ` (−${row.in - row.out})`}
                {row.cost_usd ? ` · $${row.cost_usd.toFixed(2)}` : ''}
              </span>
            </div>
            <div className="mine-funnel-bar" aria-hidden="true">
              <span
                className={`mine-funnel-fill${row.kind === 'agent' ? ' is-agent' : ''}`}
                style={{ width: `${Math.max((row.out / start) * 100, 0.5)}%` }}
              />
            </div>
            {drops.length > 0 && (
              <p className="muted small mine-funnel-why">
                {drops.map(([reason, n]) => `${reason} ${n}`).join(' · ')}
              </p>
            )}
          </li>
        )
      })}
    </ol>
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

/** A row's headline metrics beside its published reference, when it has one.
 *  A benchmark number in isolation says nothing — the whole point of the external
 *  rows is landing beside a leaderboard, so the reference travels with the score. */
function Measures({ row }: { row: Benchmark }) {
  const measures = row.last?.measures ?? {}
  const shown = row.measure_keys.filter((k) => typeof measures[k] === 'number')
  if (!shown.length) return <span className="muted">—</span>
  return (
    <span className="lab-measures">
      {shown.map((k) => (
        <span key={k}>
          <span className="muted">{k}</span> {(measures[k] as number).toFixed(3)}
        </span>
      ))}
    </span>
  )
}

function BenchTable({ rows, runIds }: { rows: Benchmark[]; runIds: Map<string, string> }) {
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>row</th>
            <th>state</th>
            <th className="num">cost</th>
            <th>last measured</th>
            <th>when</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const state = BENCH_STATE[row.state] ?? BENCH_STATE.missing
            // The ledger record these numbers came off, when the history has
            // loaded — so the summary is a way into the run behind it rather
            // than a dead end that has to be found again in the table below.
            const runId = runIds.get(row.name)
            return (
              <tr key={row.name}>
                <td>
                  <code>{row.name}</code>
                  {row.state === 'missing' && row.build_hint && (
                    <div className="lab-sub">{row.build_hint}</div>
                  )}
                </td>
                <td>
                  <Pill tone={state.tone}>{state.label}</Pill>
                </td>
                <td className="num">{minutes(row.est_min)}</td>
                <td>
                  <Measures row={row} />
                </td>
                <td className="muted">
                  {runId ? (
                    <Link className="lab-run-link" to={`/lab/run/${runId}`}>
                      {day(row.last?.at)}
                    </Link>
                  ) : (
                    day(row.last?.at)
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** What a dataset is on this box: built into a corpus, merely downloaded, or
 *  neither — which is the whole "installed vs available" question. */
function datasetState(d: Dataset): 'built' | 'downloaded' | 'available' {
  if (d.homes.some((h) => h.built)) return 'built'
  return d.download.present ? 'downloaded' : 'available'
}

/** The published baseline a family reports, flattened to one line. The shapes
 *  differ by family (BEIR carries two reference retrievers, CDR one ceiling, the
 *  haystacks a recall table), so this reads whatever the harness recorded rather
 *  than assuming one schema. */
function referenceLine(reference: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [key, value] of Object.entries(reference)) {
    if (key === 'metric' || value == null) continue
    if (typeof value === 'number') parts.push(`${key} ${value.toFixed(3)}`)
    else if (typeof value === 'string') parts.push(`${key} ${value}`)
    else if (typeof value === 'object')
      parts.push(
        Object.entries(value as Record<string, unknown>)
          .map(([at, v]) => `@${at} ${v}`)
          .join(' · '),
      )
  }
  const metric = typeof reference.metric === 'string' ? reference.metric : ''
  return [metric, parts.join('  ')].filter(Boolean).join(' — ')
}

/** What a built corpus holds. A stamped home reports its own counts; the haystack
 *  corpora are built but never stamped, so their build marker's doc count is what
 *  there is to say. */
function contents(d: Dataset): string {
  const built = d.homes.filter((h) => h.built)
  const stamped = built.find((h) => h.counts?.threads)
  if (stamped)
    return `${count(stamped.counts.threads)} threads · ${count(stamped.counts.vectors)} vectors`
  const marker = built.find((h) => h.build?.docs)
  if (marker?.build)
    return `${count(marker.build.docs)} docs${marker.build.embedded ? ' · embedded' : ''}`
  const workspace = built.find((h) => h.homes)
  if (workspace) return `${count(workspace.homes)} per-question homes`
  return ''
}

function DatasetTable({ rows }: { rows: Dataset[] }) {
  return (
    <div className="stat-table-wrap">
      <table className="stat-table lab-dataset-table">
        <colgroup>
          <col className="c-name" />
          <col className="c-state" />
          <col className="c-disk" />
          <col className="c-holds" />
          <col />
        </colgroup>
        <thead>
          <tr>
            <th>dataset</th>
            <th>on this box</th>
            <th className="num">disk</th>
            <th>holds</th>
            <th>published reference</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => {
            const state = datasetState(d)
            const size = d.homes.reduce((n, h) => n + (h.bytes ?? 0), 0) || d.download.bytes
            const truncated =
              d.homes.some((h) => h.truncated) || Boolean(d.download.truncated)
            return (
              <tr key={d.name}>
                <td>
                  <code>{d.name}</code>
                  {d.on_bench.length > 0 && (
                    <div className="lab-sub">on the bench as {d.on_bench.join(', ')}</div>
                  )}
                </td>
                <td>
                  <Pill
                    tone={state === 'built' ? 'good' : state === 'downloaded' ? 'busy' : 'idle'}
                  >
                    {state}
                  </Pill>
                </td>
                <td className="num">{bytes({ bytes: size, truncated })}</td>
                <td className="muted">{contents(d) || '—'}</td>
                <td className="muted">{referenceLine(d.reference) || '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function SearchLabView() {
  const [inv, setInv] = useState<LabInventory | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ledger, setLedger] = useState<BenchRuns | null>(null)
  const [only, setOnly] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .searchLab()
      .then((r) => live && setInv(r))
      .catch((e) => live && setError(String(e)))
    // Its own request, and its own failure: the ledger is a different route off
    // a different cost (a file read, not the cached filesystem walk), and a
    // history that will not load must not take the inventory down with it.
    api
      .searchLabRuns()
      .then((r) => live && setLedger(r))
      .catch(() => live && setLedger(null))
    return () => {
      live = false
    }
  }, [])

  /** Benchmark row → the ledger record its reported numbers came off. */
  const runIds = useMemo(() => {
    const index = new Map<string, string>()
    for (const run of ledger?.runs ?? [])
      if (run.current && !index.has(run.row)) index.set(run.row, run.id)
    return index
  }, [ledger])

  const runs = ledger?.runs ?? []
  const shown = only ? runs.filter((r) => r.row === only) : runs
  /** Every row the ledger has ever recorded, in the order it last ran them —
   *  which includes rows the manifest no longer names. */
  const ledgerRows = useMemo(() => [...new Set(runs.map((r) => r.row))], [runs])

  const byFamily = useMemo(() => {
    const groups = new Map<string, Dataset[]>()
    for (const d of inv?.datasets ?? []) {
      const list = groups.get(d.family) ?? []
      list.push(d)
      groups.set(d.family, list)
    }
    return [...groups]
  }, [inv])

  if (error)
    return (
      <div className="lab-page">
        <h1>Search lab</h1>
        <p className="error">Could not load the bench inventory: {error}</p>
      </div>
    )
  if (!inv)
    return (
      <div className="lab-page">
        <p className="muted">Loading…</p>
      </div>
    )

  const runnable = inv.benchmarks.filter((b) => b.corpus_built).length
  const built = inv.datasets.filter((d) => datasetState(d) === 'built').length

  return (
    <div className="lab-page">
      <header className="rv-head">
        <h1>Search lab</h1>
        <span className="muted small">
          ranking code <code>{inv.code_id}</code>
        </span>
      </header>
      <p className="muted">
        What the bench has to measure search with. Rows come off the lab's own
        registries and the corpora on disk, so this is the state of this box rather
        than a list somebody maintained.
      </p>

      <div className="stat-tiles">
        <Tile
          value={`${runnable}/${inv.benchmarks.length}`}
          label="benchmark rows"
          sub="runnable here · the rest await a corpus"
        />
        <Tile
          value={`${built}/${inv.datasets.length}`}
          label="corpora built"
          sub={`${inv.datasets.filter((d) => datasetState(d) === 'downloaded').length} downloaded, not built`}
        />
        <Tile value={bytes(inv.cache)} label="on disk" sub={inv.cache_root} />
        <Tile
          value={String(inv.miners.length)}
          label="label miners"
          sub="registered · nothing scores against their output"
        />
        <Tile
          value={ledger ? String(ledger.total) : '—'}
          label="runs recorded"
          sub={
            ledger
              ? `${ledgerRows.length} distinct rows have run here`
              : 'reading the ledger'
          }
        />
      </div>

      <section>
        <h2>Benchmarks</h2>
        <p className="muted">
          Every row <code>python -m search_lab benchmark</code> knows — all of them
          public benchmarks, scored beside the baseline their own leaderboard
          publishes. A <em>fresh</em> row has already been measured against the
          ranking code as it sits in the working tree, so a run skips it and reports
          from the ledger; anything else would have to run. Cost is what the row
          actually took last time, not an estimate.
        </p>
        <p className="muted small">
          What they answer is "are the retrieval components competitive in general?"
          — never whether search got better <em>on this archive</em>. These corpora
          look nothing like an agent's own session log, and no row here is a
          substitute for one that would: a relevance label about this archive would
          have to be made by searching this archive, which is the thing that cannot
          be trusted to grade itself.
        </p>
        <BenchTable rows={inv.benchmarks} runIds={runIds} />
      </section>

      <section>
        <h2>Runs</h2>
        <p className="muted">
          Every benchmark run recorded on this box, newest first — the ledger
          itself rather than the table above, which keeps one run per row to say
          what the bench reports <em>now</em>. Three things only this can show:
          the passes before the newest one, which is what a delta is read
          against; the failures, which a summary of successes renders as a silent
          gap; and rows the manifest no longer names, whose numbers were still
          measured here.
        </p>
        <p className="muted small">
          A run is a measurement of one <em>configuration</em> — a content hash of
          the ranking code, and the corpus it ran over. That is what the{' '}
          <code>config</code> column identifies, not the commit: a tuning pass
          edits a default and does not commit, so several configurations share
          one commit and the hash is the only thing that separates them. Open a
          run for the numbers its row does not lead with, what it ran against,
          and how it was invoked.
        </p>
        {ledger === null ? (
          <p className="muted">The benchmark ledger could not be read.</p>
        ) : (
          <>
            {ledgerRows.length > 1 && (
              <div className="lab-run-filter">
                <button
                  className={'chip' + (only === null ? ' active' : '')}
                  onClick={() => setOnly(null)}
                >
                  all rows
                </button>
                {ledgerRows.map((name) => (
                  <button
                    key={name}
                    className={'chip' + (only === name ? ' active' : '')}
                    onClick={() => setOnly(only === name ? null : name)}
                  >
                    {name}
                  </button>
                ))}
              </div>
            )}
            {ledger.returned < ledger.total && (
              <p className="muted small">
                Showing the newest {ledger.returned} of {ledger.total} records —
                the ledger is append-only and never pruned, so a read of it is
                bounded.
              </p>
            )}
            <RunsTable runs={shown} scoped={only !== null} />
          </>
        )}
      </section>

      <section>
        <h2>Datasets</h2>
        <p className="muted">
          Every corpus a harness here can build, whether or not it is on this box.
          <em> built</em> means an ingested (and usually embedded) home is ready to
          score; <em>downloaded</em> means the raw data is here and the build has not
          run; <em>available</em> means a flag and CPU time away. Embedding runs at
          roughly 350 docs/min on this box — that is what an un-built corpus costs.
        </p>
        {byFamily.map(([family, rows]) => (
          <div key={family} className="lab-family">
            <h3 className="lab-h3">{family}</h3>
            <p className="muted small">{inv.families[family]}</p>
            <DatasetTable rows={rows} />
          </div>
        ))}
      </section>

      <section>
        <h2>Miners</h2>
        <p className="muted">
          The only tokens-spending instrument: a miner drives headless agents against
          a frozen snapshot and mints graded relevance labels the cheaper protocols
          cannot produce. What each one's labels are <em>fixed by</em> is the field to
          read first — a label established by searching the corpus with the engine
          under test can only describe what that engine already reaches, so a
          systematic blind spot can never score as a miss.
        </p>
        <p className="muted small">
          Nothing gates on what these produce, and nothing should. Even the
          retrieval-free miners author their <em>query</em> from an artifact rather
          than observing one someone asked, so a case file is material for a
          deliberate, hand-read experiment — never a number a change can be credited
          against.
        </p>
        <div className="lab-miners">
          {inv.miners.map((m) => (
            <article key={m.name} className="lab-miner">
              <header>
                <code>{m.name}</code>
                <Pill tone={m.retrieval_free ? 'good' : 'warn'}>
                  {m.retrieval_free ? 'retrieval-free labels' : 'pooled labels'}
                </Pill>
                <span className="muted small">{m.measures}</span>
              </header>
              <p>{m.summary}</p>
              <dl className="lab-facts">
                <dt>labels fixed by</dt>
                <dd>{m.gold_source}</dd>
                <dt>cost</dt>
                <dd>{m.cost}</dd>
                <dt>invoked</dt>
                <dd>
                  <code>
                    python -m search_lab.mine {m.name}
                    {m.target_kind === 'per-case' ? ` --target ${m.default_target}` : ''}
                  </code>
                  {!m.runnable_in_all && ' — needs an argument, so `mine all` skips it'}
                </dd>
                <dt>runs</dt>
                <dd>
                  {m.runs_total === 0 ? (
                    <span className="muted">never run here</span>
                  ) : (
                    `${m.runs_total} recorded, last ${day(m.runs[0]?.at)} — ${count(
                      m.runs[0]?.written,
                    )} case(s) from ${count(m.runs[0]?.attempted)} ${m.unit}(s)`
                  )}
                </dd>
                <dt>pipeline</dt>
                <dd>
                  {m.stages.length === 0 ? (
                    <span className="muted">
                      one opaque step — this miner declares no stages, so a run
                      records where units ended and never where they were lost
                    </span>
                  ) : (
                    <ul className="mine-stages">
                      {m.stages.map((s) => (
                        <li key={s.name}>
                          <code>{s.name}</code>
                          {s.kind === 'agent' && <Pill tone="warn">agent</Pill>}
                          <span className="muted small">{s.summary}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </dd>
              </dl>
              {m.runs[0]?.funnel?.length ? (
                <>
                  <h4 className="mine-funnel-title">
                    last run’s funnel <span className="muted small">{day(m.runs[0].at)}</span>
                  </h4>
                  <Funnel rows={m.runs[0].funnel} />
                </>
              ) : m.stages.length > 0 && m.runs_total > 0 ? (
                <p className="muted small">
                  The recorded runs predate this miner’s stages, so no funnel was
                  captured — the next run records one.
                </p>
              ) : null}
            </article>
          ))}
        </div>
      </section>

    </div>
  )
}
