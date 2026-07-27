import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  api,
  type BenchRunRecord,
  type BenchRuns,
  type Percentiles,
  type QueryRow,
  type RunPerformance,
  type RunQueries,
} from '../api'
import {
  Pill,
  RunState,
  RunsTable,
  delta,
  elapsed,
  measure,
  ms,
  previousConfiguration,
  when,
} from './labRuns'

// One recorded benchmark run, and everything the ledger kept about it.
//
// The lab's benchmark table shows a row's headline metrics because that is what
// fits in a line, and drops the rest: the metrics the row does not lead with,
// the corpus fingerprint, the exact argv, the commit, how long it took. None of
// that is decoration during a tuning loop — "which corpus was that measured on"
// and "what did the invocation actually say" are the questions asked of a number
// that looks wrong, and a summary cannot answer either.
//
// Per-query detail comes off the run's own sidecar rather than the ledger — a
// bench pass scores thousands of queries, and inlining that would put a
// megabyte a pass into a file the runs list reads whole. So the ledger record
// carries `has_queries` and this page fetches the detail for the one run being
// read, which is the only run anybody wants it for.

/** A recorded fact about the run, one line. Absent facts render as `—` rather
 *  than vanishing: "the ledger has no corpus id for this run" is itself an
 *  answer, and a row that disappears turns it into a question about the page. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  )
}

/** Every number the run recorded, against the same numbers at the previous
 *  configuration.
 *
 *  All of them, not the row's headline set: a run's declared metrics are what it
 *  is *judged* on, and the ones beside them (recall@100 under an nDCG row, the
 *  query latency under either) are how a movement in the headline gets
 *  explained. The delta column only appears when there is an earlier
 *  configuration to read against — a first run has nothing to have moved from,
 *  and a zero would be a claim rather than a blank. */
function Measures({ run, prior }: { run: BenchRunRecord; prior: BenchRunRecord | null }) {
  // The row's declared metrics lead, in the row's own order; whatever else the
  // run recorded follows. `n` is the size of the query set, not a score — it
  // belongs with the run's conditions, and it is reported there.
  const declared = run.measure_keys.filter((k) => k in run.measures)
  const rest = Object.keys(run.measures).filter(
    (k) => k !== 'n' && !declared.includes(k),
  )
  const keys = [...declared, ...rest]
  if (!keys.length)
    return (
      <p className="muted">
        This run recorded no numbers
        {run.status === 'ok'
          ? ' — it exited cleanly without writing a report the runner could read.'
          : ', which is what a failure has to report.'}
      </p>
    )
  return (
    <div className="stat-table-wrap">
      <table className="stat-table">
        <thead>
          <tr>
            <th>measure</th>
            <th className="num">value</th>
            {prior && <th className="num">vs last config</th>}
            {prior && <th>then</th>}
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => {
            const now = run.measures[k]
            const before = prior?.measures[k]
            const moved =
              typeof now === 'number' && typeof before === 'number'
                ? now - before
                : null
            return (
              <tr key={k}>
                <td>
                  <code>{k}</code>
                  {!declared.includes(k) && (
                    <span className="lab-aside">not a headline metric for this row</span>
                  )}
                </td>
                <td className="num">{measure(now)}</td>
                {prior && (
                  // Signed, never coloured. Which direction is an improvement is
                  // the metric's business — a rise in nDCG and a rise in query
                  // latency are opposite news, and nothing on this page knows
                  // which a given key is. The sign says what moved; reading it
                  // as good or bad needs someone who knows the measure.
                  <td className={'num' + (moved ? '' : ' muted')}>
                    {moved == null ? '—' : delta(now as number, before as number)}
                  </td>
                )}
                {prior && <td className="muted num">{measure(before)}</td>}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** One headline cost, with what it was at the previous configuration. */
function Cost({
  label,
  value,
  before,
  note,
}: {
  label: string
  value: string
  before?: string
  note?: string
}) {
  return (
    <div className="stat-tile">
      <div className="n">{value}</div>
      <div className="l">{label}</div>
      {before && <div className="s">was {before}</div>}
      {note && <div className="s">{note}</div>}
    </div>
  )
}

/** A stage's share of the median query, as a bar.
 *
 *  Bars against the run's own p50 rather than against each other: the question a
 *  reader has is "what is this query spending its time on", and the answer is a
 *  fraction of the whole. The arms overlap in wall-clock, so the bars are
 *  deliberately not stacked and deliberately allowed to sum past 100% — a stack
 *  would assert a sequence that the concurrency makes false. */
function StageBar({ stage, of }: { stage: Percentiles; of: number }) {
  const share = of > 0 ? Math.min(1, stage.p50 / of) : 0
  return (
    <span className="lab-bar" title={`${Math.round(share * 100)}% of the median query`}>
      <span className="lab-bar-fill" style={{ width: `${share * 100}%` }} />
    </span>
  )
}

/** Where a run's time went, and what it cost per query.
 *
 *  This exists because a benchmark row already runs the exact workload a latency
 *  measurement would run again — so a ledger that recorded only the scores threw
 *  away a distribution and a stage profile it had already paid for. With both
 *  kept, a tuning pass that lifts nDCG by 0.004 and doubles the tail is legible
 *  as the trade it is rather than filed as a win.
 *
 *  Not a controlled benchmark: one sample per query, on a shared machine, under
 *  whatever else was running. What it answers is whether the *shape* of the cost
 *  moved between two configurations — which no careful measurement of only the
 *  newest one can answer. */
function Performance({
  run,
  prior,
}: {
  run: BenchRunRecord
  prior: BenchRunRecord | null
}) {
  const p: RunPerformance = run.performance ?? {}
  const was = prior?.performance
  const stages = Object.entries(p.stages ?? {})
  const p50 = p.total?.p50 ?? 0
  // Whatever the process spent outside the scored loop: building the corpus,
  // embedding it, loading a model. On a cold row it is most of the wall-clock
  // and none of the search cost, so reporting only the total would read as the
  // queries having been slow.
  const setup =
    typeof run.elapsed_s === 'number' && typeof p.scoring_s === 'number'
      ? Math.max(0, run.elapsed_s - p.scoring_s)
      : null

  if (!p.total && !p.queries)
    return (
      <p className="muted">
        This run recorded no cost profile. The harnesses report one per query and
        per stage; a run from before they did, or one that failed, has only the
        wall-clock above.
      </p>
    )

  return (
    <>
      <div className="stat-tiles">
        <Cost
          label="median query"
          value={ms(p.total?.p50)}
          before={was?.total ? ms(was.total.p50) : undefined}
        />
        <Cost
          label="p99 query"
          value={ms(p.total?.p99)}
          before={was?.total ? ms(was.total.p99) : undefined}
          note="the tail a ranking change moves first"
        />
        <Cost
          label="throughput"
          value={p.qps ? `${p.qps}/s` : '—'}
          before={was?.qps ? `${was.qps}/s` : undefined}
          note={p.queries ? `${measure(p.queries)} queries scored` : undefined}
        />
        <Cost
          label="scoring"
          value={elapsed(p.scoring_s)}
          before={was?.scoring_s ? elapsed(was.scoring_s) : undefined}
          note={setup != null ? `+ ${elapsed(setup)} getting ready` : undefined}
        />
      </div>

      <div className="stat-table-wrap">
        <table className="stat-table">
          <thead>
            <tr>
              <th>query latency</th>
              <th className="num">p50</th>
              <th className="num">p95</th>
              <th className="num">p99</th>
              <th className="num">mean</th>
              <th className="num">max</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>this run</td>
              <td className="num">{ms(p.total?.p50)}</td>
              <td className="num">{ms(p.total?.p95)}</td>
              <td className="num">{ms(p.total?.p99)}</td>
              <td className="num">{ms(p.mean_ms)}</td>
              <td className="num">{ms(p.max_ms)}</td>
            </tr>
            {was?.total && (
              <tr className="muted">
                <td>last config</td>
                <td className="num">{ms(was.total.p50)}</td>
                <td className="num">{ms(was.total.p95)}</td>
                <td className="num">{ms(was.total.p99)}</td>
                <td className="num">{ms(was.mean_ms)}</td>
                <td className="num">{ms(was.max_ms)}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {stages.length > 0 && (
        <>
          <h3 className="lab-h3">Where the median query went</h3>
          <p className="muted small">
            Durations, not shares. The two pool arms — <code>fts_ms</code> and{' '}
            <code>semantic_ms</code> — run concurrently, so they cover overlapping
            wall-clock and can sum past the total; only the shape stages after the
            pool is fused run in sequence. Bars are each stage's median against
            this run's median query, which is why they are not stacked.
          </p>
          <div className="stat-table-wrap">
            <table className="stat-table">
              <thead>
                <tr>
                  <th>stage</th>
                  <th className="bar-col">share of p50</th>
                  <th className="num">p50</th>
                  <th className="num">p95</th>
                  <th className="num">p99</th>
                  {was?.stages && <th className="num">was p50</th>}
                </tr>
              </thead>
              <tbody>
                {stages
                  .sort((a, b) => b[1].p50 - a[1].p50)
                  .map(([name, at]) => (
                    <tr key={name}>
                      <td>
                        <code>{name}</code>
                      </td>
                      <td className="bar-col">
                        <StageBar stage={at} of={p50} />
                      </td>
                      <td className="num">{ms(at.p50)}</td>
                      <td className="num">{ms(at.p95)}</td>
                      <td className="num">{ms(at.p99)}</td>
                      {was?.stages && (
                        <td className="num muted">{ms(was.stages[name]?.p50)}</td>
                      )}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <dl className="lab-facts lab-run-facts">
        <Fact label="arms">
          {p.arms?.length ? p.arms.join(' + ') : <span className="muted">—</span>}
          <span className="muted"> — which retrieval arms this row ran with</span>
        </Fact>
        <Fact label="corpus">
          {p.corpus_docs != null ? (
            <>
              {measure(p.corpus_docs)} docs
              <span className="muted"> — what each query was searched against</span>
            </>
          ) : (
            <span className="muted">—</span>
          )}
        </Fact>
        <Fact label="candidate pool">
          {p.pool_p50 != null ? (
            <>
              {measure(p.pool_p50)}
              <span className="muted">
                {' '}
                — median rows fused out of the arms and handed to the ranker
              </span>
            </>
          ) : (
            <span className="muted">—</span>
          )}
        </Fact>
        <Fact label="stage coverage">
          {p.staged != null && p.queries != null ? (
            <>
              {measure(p.staged)} of {measure(p.queries)}
              <span className="muted">
                {p.staged < p.queries
                  ? ' — the rest were served from the pool cache and did no retrieval, so they are excluded rather than averaged in as fast ones'
                  : ' searches carried a breakdown'}
              </span>
            </>
          ) : (
            <span className="muted">—</span>
          )}
        </Fact>
        <Fact label="cold searches">
          {p.cold != null ? (
            <>
              {measure(p.cold)}
              <span className="muted">
                {' '}
                — searches that paid a model load inside them, which lands in the
                max above rather than in the median
              </span>
            </>
          ) : (
            <span className="muted">—</span>
          )}
        </Fact>
      </dl>
    </>
  )
}

/** Where a query's gold document landed. The distinction this column exists for
 *  is `not found` against a deep rank: the first is a recall failure and the
 *  second a ranking one, they are fixed in different places, and the score
 *  reports them identically. */
function Rank({ row }: { row: QueryRow }) {
  if (row.rank == null) return <Pill tone="bad">not found</Pill>
  if (row.rank <= 10) return <span>{row.rank}</span>
  return <span className="lab-deep">{row.rank}</span>
}

/** Every query the run scored, worst first — or the ones that moved.
 *
 *  The comparison is why the detail is kept. Two configurations whose aggregate
 *  differs by 0.004 have usually not moved a little on every query; they moved a
 *  lot on a few, and which few is the whole content of the change. No aggregate
 *  says it, and no re-run recovers it — the earlier configuration is gone. */
function Queries({ run, prior }: { run: BenchRunRecord; prior: BenchRunRecord | null }) {
  const [detail, setDetail] = useState<RunQueries | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Comparable only when the earlier run's detail is also still on disk: the
  // store is capped, so the join has two ways to be unavailable.
  const comparable = Boolean(prior?.has_queries && run.has_queries)
  const [vs, setVs] = useState(false)

  useEffect(() => {
    let live = true
    setDetail(null)
    setError(null)
    api
      .searchLabRunQueries(run.id, vs && prior ? prior.id : undefined)
      .then((r) => live && setDetail(r))
      .catch((e) => live && setError(String(e)))
    return () => {
      live = false
    }
  }, [run.id, vs, prior])

  if (!run.has_queries)
    return (
      <p className="muted">
        No per-query detail for this run. The store is capped at the most recent
        runs, so an older one keeps its numbers and loses its detail — and a run
        from before the harnesses reported any never had it.
      </p>
    )
  if (error) return <p className="error">Could not load the per-query detail: {error}</p>
  if (!detail) return <p className="muted">Loading…</p>

  const lead = detail.lead
  const cols = detail.rows[0]
    ? Object.keys(detail.rows[0].measures).filter(
        (k) => typeof detail.rows[0].measures[k] === 'number',
      )
    : []

  return (
    <>
      <p className="muted">
        {measure(detail.total)} {detail.order === 'moved' ? 'queries moved' : 'queries'}
        {detail.order === 'worst' && detail.misses > 0 && (
          <>
            {' — '}
            <strong>{measure(detail.misses)}</strong> never retrieved their gold
            document at all, which is a recall failure rather than a ranking one
          </>
        )}
        .{' '}
        {detail.returned < detail.total &&
          `Showing the ${detail.order === 'moved' ? 'biggest movements' : 'worst'} ${measure(detail.returned)}.`}
      </p>

      {comparable && (
        <div className="lab-run-filter">
          <button
            className={'chip' + (!vs ? ' active' : '')}
            onClick={() => setVs(false)}
          >
            worst first
          </button>
          <button className={'chip' + (vs ? ' active' : '')} onClick={() => setVs(true)}>
            what moved vs last config
          </button>
        </div>
      )}

      <div className="stat-table-wrap">
        <table className="stat-table lab-query-table">
          <colgroup>
            <col className="c-query" />
          </colgroup>
          <thead>
            <tr>
              <th>query</th>
              <th className="num">gold rank</th>
              <th className="num">found</th>
              {detail.order === 'moved' && lead && (
                <th className="num">{lead} moved</th>
              )}
              {cols.map((k) => (
                <th key={k} className="num">
                  {k}
                </th>
              ))}
              <th className="num">latency</th>
            </tr>
          </thead>
          <tbody>
            {detail.rows.map((row) => (
              <tr key={row.qid}>
                <td className="lab-query-cell">
                  {row.query || <span className="muted">—</span>}
                  {row.group && <div className="lab-sub">{row.group}</div>}
                </td>
                <td className="num">
                  <Rank row={row} />
                  {row.before && row.before.rank !== row.rank && (
                    <div className="lab-sub">
                      was {row.before.rank ?? 'not found'}
                    </div>
                  )}
                </td>
                <td className="num">
                  {row.found == null || row.n_gold == null ? (
                    <span className="muted">—</span>
                  ) : (
                    `${row.found}/${row.n_gold}`
                  )}
                </td>
                {detail.order === 'moved' && lead && (
                  <td className="num">{row.moved != null ? delta(row.moved, 0) : '—'}</td>
                )}
                {cols.map((k) => (
                  <td key={k} className="num">
                    {measure(row.measures[k])}
                  </td>
                ))}
                <td className="num muted">{ms(row.latency_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

export function BenchRunView() {
  const { id = '' } = useParams()
  const [ledger, setLedger] = useState<BenchRuns | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .searchLabRuns()
      .then((r) => live && setLedger(r))
      .catch((e) => live && setError(String(e)))
    return () => {
      live = false
    }
  }, [])

  const run = useMemo(
    () => ledger?.runs.find((r) => r.id === id) ?? null,
    [ledger, id],
  )
  const prior = useMemo(
    () => (run && ledger ? previousConfiguration(run, ledger.runs) : null),
    [run, ledger],
  )
  const siblings = useMemo(
    () => (run && ledger ? ledger.runs.filter((r) => r.row === run.row) : []),
    [run, ledger],
  )

  if (error)
    return (
      <div className="lab-page">
        <h1>Benchmark run</h1>
        <p className="error">Could not load the benchmark ledger: {error}</p>
      </div>
    )
  if (!ledger)
    return (
      <div className="lab-page">
        <p className="muted">Loading…</p>
      </div>
    )
  if (!run)
    return (
      <div className="lab-page">
        <p className="lab-back">
          <Link to="/lab">← search lab</Link>
        </p>
        <h1>Benchmark run</h1>
        <p className="muted">
          No run <code>{id}</code> in the ledger.{' '}
          {ledger.returned < ledger.total
            ? `Only the newest ${ledger.returned} of ${ledger.total} records were read, so an older run is not reachable by link yet.`
            : 'A run is addressed by a hash of what it recorded, so a link only survives while the record does.'}
        </p>
      </div>
    )

  return (
    <div className="lab-page">
      <p className="lab-back">
        <Link to="/lab">← search lab</Link>
      </p>
      <header className="rv-head">
        <h1>
          <code>{run.row}</code>
        </h1>
        <span className="lab-run-marks">
          <RunState run={run} />
          {!run.on_bench && <Pill tone="warn">off the bench</Pill>}
          {run.code_current && <Pill tone="good">code in tree</Pill>}
        </span>
      </header>
      <p className="muted" title={run.at}>
        Ran {when(run.at)}, for {elapsed(run.elapsed_s)}.{' '}
        {run.current && run.on_bench
          ? 'These are the numbers the bench reports for this row.'
          : run.status === 'ok'
            ? 'A later pass has since measured this row, so the bench reports those numbers instead.'
            : 'It recorded a failure, which is why it reports no numbers.'}
        {run.code_current === false &&
          ' The ranking code has changed since, so a run now would not be expected to reproduce it.'}
      </p>

      <section>
        <h2>What it measured</h2>
        {prior ? (
          <p className="muted">
            Beside the same row at its last <em>different</em> configuration —{' '}
            <Link to={`/lab/run/${prior.id}`}>{when(prior.at)}</Link>, code{' '}
            <code>{prior.code_id?.slice(0, 8)}</code>. Not simply the previous
            run: a tuning loop measures one configuration more than once, and a
            delta against an identical one is zero, which reads as "the change did
            nothing" rather than "this is the same measurement twice".
          </p>
        ) : (
          <p className="muted">
            The first run of this row at any configuration on this box, so there
            is nothing behind it to read a movement against.
          </p>
        )}
        <Measures run={run} prior={prior} />
      </section>

      <section>
        <h2>Performance</h2>
        <p className="muted">
          What the run cost, beside what it scored. Both come off the same
          searches, and only together say whether a configuration worth the score
          is one worth shipping — a pass that lifts nDCG by 0.004 and doubles the
          tail is a trade, and a record of the score alone files it as a win.
          {prior && ' Each figure carries what it was at the last different configuration.'}
        </p>
        <p className="muted small">
          A lead, not a benchmark: one sample per query, on a shared machine,
          under whatever else was running — <code>speed.py</code> is what controls
          for those. What this answers is whether the <em>shape</em> of the cost
          moved between two configurations, which a careful measurement of only
          the newest one cannot.
        </p>
        <Performance run={run} prior={prior} />
      </section>

      <section>
        <h2>Conditions</h2>
        <p className="muted">
          What the numbers above are a measurement <em>of</em>. The pair that
          decides whether a recorded run may still be reported is{' '}
          <code>code</code> and <code>corpus</code>: the bench skips a row whose
          two still match, so a run's numbers are only current while both hold.
        </p>
        <dl className="lab-facts lab-run-facts">
          <Fact label="ranking code">
            {run.code_id ? (
              <>
                <code>{run.code_id}</code>{' '}
                <span className="muted">
                  {run.code_current === null
                    ? '— the row has left the manifest, so there is no current code id to compare it to'
                    : run.code_current
                      ? '— the ranking and scoring source as it sits in the working tree now'
                      : `— superseded; the tree now hashes to ${ledger.code_id.slice(0, 8)}`}
                </span>
              </>
            ) : (
              <span className="muted">—</span>
            )}
          </Fact>
          <Fact label="corpus">
            {run.corpus_id ? (
              <code>{run.corpus_id}</code>
            ) : (
              <span className="muted">
                no single corpus to fingerprint — the per-question harnesses build
                one small home per question, so the code hash alone decides
                freshness
              </span>
            )}
          </Fact>
          <Fact label="commit">
            {run.commit ? <code>{run.commit}</code> : <span className="muted">—</span>}
            <span className="muted">
              {' '}
              — the human label only. Uncommitted edits count toward the code
              hash, so several configurations can share one commit.
            </span>
          </Fact>
          <Fact label="queries">
            {typeof run.measures.n === 'number' ? (
              measure(run.measures.n)
            ) : (
              <span className="muted">not recorded</span>
            )}
          </Fact>
          {run.tier && <Fact label="tier">{run.tier}</Fact>}
          <Fact label="ledger id">
            <code>{run.id}</code>
          </Fact>
        </dl>
      </section>

      <section>
        <h2>Per query</h2>
        <p className="muted">
          Which queries this run actually failed. The aggregate above says the
          row scores what it scores; only this says <em>what it got wrong</em>,
          and the two failure modes it separates are fixed in different places —
          a gold document ranked 40th is a ranking problem, one that never came
          back is a recall problem, and a score of 0.0 reports them identically.
          {prior?.has_queries &&
            run.has_queries &&
            ' Comparing against the last different configuration lists the queries that moved, which is where a change of 0.004 in the mean actually happened.'}
        </p>
        <Queries run={run} prior={prior} />
      </section>

      <section>
        <h2>Invocation</h2>
        <p className="muted">
          The harness this run was, exactly as the runner spawned it — a row runs
          in its own process, so this is the whole of what produced the numbers.
        </p>
        <pre className="lab-argv">{run.argv.join(' ')}</pre>
      </section>

      <section>
        <h2>This row's history</h2>
        <p className="muted">
          Every run of <code>{run.row}</code> recorded on this box, newest first.
        </p>
        <RunsTable runs={siblings} scoped activeId={run.id} />
      </section>
    </div>
  )
}
