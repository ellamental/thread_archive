import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type ArchiveEntry,
  type HealthRecord,
  type LoadPhase,
  type LoadRun,
  type Status,
  type WatchSourceRecord,
} from '../api'

// 'busy' is work in flight — distinct from 'warn', which means someone must act.
type Tone = 'good' | 'warn' | 'bad' | 'quiet' | 'busy'

interface Notice {
  key: string
  tone: Exclude<Tone, 'quiet'>
  title: string
  detail: string
  command?: string
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

function elapsed(iso?: string | null): number | null {
  if (!iso) return null
  const when = new Date(iso).getTime()
  return Number.isFinite(when) ? Math.max(0, Date.now() - when) : null
}

function age(iso?: string | null): string {
  const ms = elapsed(iso)
  if (ms == null) return 'never'
  if (ms < MINUTE) return 'just now'
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)}m ago`
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h ago`
  return `${Math.floor(ms / DAY)}d ago`
}

function dateTime(iso?: string | null): string {
  if (!iso) return 'Never recorded'
  const d = new Date(iso)
  return Number.isFinite(d.getTime())
    ? d.toLocaleString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      })
    : iso
}

function int(n?: number): string {
  return n == null ? '—' : n.toLocaleString()
}

function bytes(n?: number): string {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(1)} GB`
}

function duration(s?: number | null): string {
  if (s == null) return '—'
  if (s < 1) return `${Math.round(s * 1000)}ms`
  if (s < 90) return s < 10 ? `${s.toFixed(1)}s` : `${Math.round(s)}s`
  if (s < 5400) return `${(s / 60).toFixed(1)} min`
  return `${(s / 3600).toFixed(1)} h`
}

// The live record an in-flight load publishes, or null when this archive has no
// load state at all (never loaded through the tracked paths).
function liveLoad(archive: ArchiveEntry): LoadRun | null {
  const load = archive.load as LoadRun
  return load && load.status ? load : null
}

// The state of an archive's *derived* data — index, lexical search, vectors —
// and nothing else. This axis says whether a load built the archive's indexes, not
// whether anything can query it; reachability is a separate marker (see
// `servedTag`), because an archive can be perfectly indexed and unreachable.
// 'Untracked' is deliberately weaker than 'Indexed': an index exists but no load
// was ever recorded for it, so its completeness is unproven rather than proven.
function indexState(archive: ArchiveEntry): { label: string; tone: Tone } {
  if (!archive.exists) return { label: 'Missing', tone: 'bad' }
  const live = liveLoad(archive)
  if (live?.status === 'running') return { label: 'Loading', tone: 'busy' }
  if (live?.status === 'stalled') return { label: 'Stalled', tone: 'bad' }
  const last = archive.runs?.[0] ?? live
  if (last?.status === 'failed') return { label: 'Load failed', tone: 'bad' }
  if (last?.status === 'ok') return { label: 'Indexed', tone: 'good' }
  if ((archive.index_bytes ?? 0) > 0) return { label: 'Untracked', tone: 'quiet' }
  return { label: 'Empty', tone: 'quiet' }
}

function currentPhase(load: LoadRun): LoadPhase | null {
  const phases = load.phases || []
  return phases.length ? phases[phases.length - 1] : null
}

function PhaseProgress({ phase }: { phase: LoadPhase }) {
  const pct =
    phase.total && phase.total > 0
      ? Math.min(100, (phase.done / phase.total) * 100)
      : null
  return (
    <div className="load-progress">
      <div className="load-progress-top">
        <span className="load-phase-name">{phase.name}</span>
        <span className="load-phase-counts">
          {int(phase.done)}
          {phase.total != null ? ` / ${int(phase.total)}` : ''}
          {phase.rate_per_s != null ? ` · ${phase.rate_per_s.toFixed(1)}/s` : ''}
          {phase.eta_s != null ? ` · ETA ${duration(phase.eta_s)}` : ''}
        </span>
      </div>
      {pct != null && (
        <div
          className="load-bar"
          role="progressbar"
          aria-label={`${phase.name} progress`}
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="load-bar-fill" style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  )
}

// Each phase as a chip: what it was and what it cost. This is the thing that turns
// "the load felt slow" into "the embed phase was 20 minutes of the 21".
function PhaseChips({ phases }: { phases: LoadPhase[] }) {
  if (!phases.length) return <span className="load-chip-empty">no phases recorded</span>
  return (
    <span className="load-chips">
      {phases.map((phase) => (
        <span className="load-chip" key={phase.name} title={
          Object.entries(phase.detail_s || {})
            .map(([k, v]) => `${k} ${duration(v)}`)
            .join(' · ') || undefined
        }>
          <span className="load-chip-name">{phase.name}</span>
          <span className="load-chip-value">{duration(phase.elapsed_s)}</span>
        </span>
      ))}
    </span>
  )
}

function shellArg(value: string): string {
  if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value
  return `'${value.replaceAll("'", "'\"'\"'")}'`
}

function recordTone(record: HealthRecord | null, staleAfter: number): Tone {
  if (!record) return 'bad'
  if (record.ok === false) return 'bad'
  const ms = elapsed(record.at)
  if (ms == null || ms > staleAfter) return 'warn'
  return 'good'
}

function Pill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={`health-pill ${tone}`}>{children}</span>
}

function CheckCard({
  title,
  tone,
  state,
  at,
  detail,
  children,
}: {
  title: string
  tone: Tone
  state: string
  at?: string | null
  detail: string
  children?: React.ReactNode
}) {
  return (
    <article className={`health-check ${tone}`}>
      <div className="health-check-top">
        <h2>{title}</h2>
        <Pill tone={tone}>{state}</Pill>
      </div>
      <div className="health-check-age" title={dateTime(at)}>
        {age(at)}
      </div>
      <p>{detail}</p>
      {children}
    </article>
  )
}

function Command({ children }: { children: string }) {
  return <code className="health-command">{children}</code>
}

function supportTier(source: string): string {
  return source === 'claude-code' ? 'First-class' : 'Best effort'
}

function sourceTone(source: WatchSourceRecord): Tone {
  if (source.errors || source.parse_errors) return 'bad'
  return 'good'
}

function buildNotices(status: Status): Notice[] {
  const notices: Notice[] = []
  const dest = status.pipeline.dest || status.last_backup?.dest || status.last_nightly?.dest
  const nightlyCommand = dest
    ? `thread_archive nightly ${shellArg(dest)}`
    : 'thread_archive setup'

  if (!status.last_watch_pass) {
    notices.push({
      key: 'capture-missing',
      tone: 'bad',
      title: 'Capture has never reported a completed pass',
      detail: 'Run one pass now. If it succeeds, install or restart the watcher so new conversations keep arriving.',
      command: 'thread_archive watch --once',
    })
  } else if ((elapsed(status.last_watch_pass.at) ?? Infinity) > 15 * MINUTE) {
    notices.push({
      key: 'capture-stale',
      tone: 'bad',
      title: `Capture is stale — last check ${age(status.last_watch_pass.at)}`,
      detail: 'A stalled watcher can leave recent conversations outside the archive.',
      command: 'thread_archive watch --once',
    })
  }

  if (status.last_watch_errors) {
    notices.push({
      key: 'watch-errors',
      tone: 'bad',
      title: 'A provider failed during capture',
      detail: (status.last_watch_errors.errors || []).join(' · ') || 'The latest watcher run recorded provider errors.',
      command: 'thread_archive status',
    })
  }

  for (const [source, data] of Object.entries(status.last_watch_pass?.sources || {})) {
    if (!data.errors && !data.parse_errors) continue
    notices.push({
      key: `source-${source}`,
      tone: 'bad',
      title: `${source} is not importing cleanly`,
      detail: `${int(data.parse_errors)} parse errors and ${int(data.errors)} watcher errors since this capture process started.`,
      command: `thread_archive fix-import ${shellArg(source)}`,
    })
  }

  if (!status.pipeline.ran) {
    notices.push({
      key: 'nightly-missing',
      tone: 'bad',
      title: 'The protection pipeline has never completed',
      detail: 'Backup, integrity verification, and a restore drill have not yet been proven together.',
      command: nightlyCommand,
    })
  } else if (!status.pipeline.ok) {
    notices.push({
      key: 'nightly-failed',
      tone: 'bad',
      title: `Protection failed at ${status.pipeline.failed_stages.join(', ') || 'an unknown stage'}`,
      detail: 'The pipeline verdict accounts for later successful reruns, so these failures are still unresolved.',
      command: nightlyCommand,
    })
  } else if ((elapsed(status.pipeline.nightly_at) ?? Infinity) > 36 * HOUR) {
    notices.push({
      key: 'nightly-stale',
      tone: 'bad',
      title: `Protection is stale — last pipeline ${age(status.pipeline.nightly_at)}`,
      detail: 'The scheduled backup and recovery proof may have stopped running.',
      command: nightlyCommand,
    })
  }

  if (status.backup_same_device === true) {
    notices.push({
      key: 'same-disk',
      tone: 'warn',
      title: 'Backup is on the same filesystem as the archive',
      detail: 'This protects against index corruption and accidental deletion, but not loss of the disk. Move the scheduled destination to another disk.',
      command: 'thread_archive daemon install --backup --dest /Volumes/<backup-disk>/thread-archive',
    })
  }

  if (status.last_coverage && !status.last_coverage.ok) {
    notices.push({
      key: 'coverage-failed',
      tone: 'bad',
      title: 'Capture coverage has gaps',
      detail: (status.last_coverage.failed || []).join(' · ') || 'The coverage audit found missing or degraded source data.',
      command: 'thread_archive coverage',
    })
  }
  for (const [index, warning] of (status.last_coverage?.warnings || []).entries()) {
    notices.push({
      key: `coverage-warning-${index}`,
      tone: 'warn',
      title: 'Coverage warning',
      detail: warning,
      command: 'thread_archive coverage',
    })
  }

  const update = status.last_self_update
  if (update?.action === 'update') {
    notices.push({
      key: 'update',
      tone: 'good',
      title: `${update.tag || 'A new release'} is available`,
      detail: update.reason || 'Applying updates is explicit.',
      command: 'thread_archive self-update',
    })
  } else if (update && !update.ok) {
    notices.push({
      key: 'update-blocked',
      tone: 'warn',
      title: `Updates are ${update.action || 'blocked'}`,
      detail: update.reason || 'The update check did not complete successfully.',
      command: 'thread_archive self-update --check',
    })
  }

  return notices
}

// How often the archive list re-reads while a load is in flight. A load is the one
// thing on this page that changes by the second, so it polls to stay a live view;
// when nothing is loading there is nothing to animate and it backs off.
const LOADING_POLL_MS = 2_000
const IDLE_POLL_MS = 30_000

export function HealthView() {
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [archives, setArchives] = useState<ArchiveEntry[] | null>(null)

  useEffect(() => {
    api.status().then(setStatus).catch((e) => setError(String(e.message ?? e)))
  }, [])

  const loading = (archives || []).some((a) => liveLoad(a)?.status === 'running')

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      api
        .archives()
        .then((rows) => {
          if (!cancelled) setArchives(rows)
        })
        .catch(() => {
          // An archive listing failure must never blank the trust page — the
          // section degrades to whatever it last showed.
        })
        .finally(() => {
          if (!cancelled) timer = setTimeout(tick, loading ? LOADING_POLL_MS : IDLE_POLL_MS)
        })
    }
    tick()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [loading])

  // Every archive's runs on one timeline — the cross-archive load history.
  const history = useMemo(() => {
    const rows: { archive: string; run: LoadRun }[] = []
    for (const archive of archives || [])
      for (const run of archive.runs || []) rows.push({ archive: archive.label, run })
    rows.sort((a, b) => String(b.run.at || '').localeCompare(String(a.run.at || '')))
    return rows.slice(0, 12)
  }, [archives])

  const notices = useMemo(() => (status ? buildNotices(status) : []), [status])

  if (error) return <div className="empty">health unavailable: {error}</div>
  if (!status) return <div className="empty">checking capture and recovery evidence…</div>

  const critical = notices.filter((notice) => notice.tone === 'bad')
  const warnings = notices.filter((notice) => notice.tone === 'warn')
  const overallTone: Tone = critical.length ? 'bad' : warnings.length ? 'warn' : 'good'
  const overallTitle =
    overallTone === 'good'
      ? 'Your archive is protected'
      : overallTone === 'warn'
        ? 'Your archive needs attention'
        : 'Protection is incomplete'
  const providerRows = Object.entries(status.last_watch_pass?.sources || {})
  const backupTone = recordTone(status.last_backup, 36 * HOUR)
  const verifyTone = recordTone(status.last_verify, 36 * HOUR)
  const drillTone = recordTone(status.last_restore_drill, 8 * DAY)
  const captureTone: Tone =
    !status.last_watch_pass || status.last_watch_errors
      ? 'bad'
      : (elapsed(status.last_watch_pass.at) ?? Infinity) > 15 * MINUTE
        ? 'bad'
        : providerRows.some(([, source]) => sourceTone(source) === 'bad')
          ? 'bad'
          : 'good'

  return (
    <div className="health-page">
      <header className={`health-hero ${overallTone}`}>
        <div>
          <p className="eyebrow">Trust center</p>
          <h1>{overallTitle}</h1>
          <p className="health-lede">
            Live evidence that conversations are arriving, truth is intact, and a backup can actually restore.
          </p>
        </div>
        <div className={`health-orb ${overallTone}`} aria-hidden="true">
          {overallTone === 'good' ? '✓' : overallTone === 'warn' ? '!' : '×'}
        </div>
      </header>

      {notices.length > 0 && (
        <section className="health-actions" aria-labelledby="health-actions-heading">
          <div className="health-section-heading">
            <div>
              <p className="eyebrow">Action queue</p>
              <h2 id="health-actions-heading">
                {critical.length
                  ? `${critical.length} protection ${critical.length === 1 ? 'gap' : 'gaps'}`
                  : warnings.length
                    ? `${warnings.length} ${warnings.length === 1 ? 'warning' : 'warnings'}`
                    : 'Maintenance available'}
              </h2>
            </div>
          </div>
          <div className="health-notices">
            {notices.map((notice) => (
              <article className={`health-notice ${notice.tone}`} key={notice.key}>
                <span className="health-notice-mark" aria-hidden="true">
                  {notice.tone === 'bad' ? '×' : notice.tone === 'warn' ? '!' : '↑'}
                </span>
                <div>
                  <h3>{notice.title}</h3>
                  <p>{notice.detail}</p>
                  {notice.command && <Command>{notice.command}</Command>}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      <section aria-labelledby="protection-heading">
        <div className="health-section-heading">
          <div>
            <p className="eyebrow">Protection chain</p>
            <h2 id="protection-heading">Four checks that make the archive trustworthy</h2>
          </div>
        </div>
        <div className="health-check-grid">
          <CheckCard
            title="Capture"
            tone={captureTone}
            state={status.watch_process_alive ? 'Active' : captureTone === 'good' ? 'Current' : 'Stale'}
            at={status.last_watch_pass?.at}
            detail={
              status.watch_process_alive
                ? `Always-on capture is active with ${int(status.last_watch_pass?.passes)} completed passes.`
                : 'No persistent capture process is observed; the timestamp shows the most recent completed catch-up.'
            }
          />
          <CheckCard
            title="Integrity"
            tone={verifyTone}
            state={status.last_verify?.ok ? 'Verified' : status.last_verify ? 'Failed' : 'Unproven'}
            at={status.last_verify?.at}
            detail={
              status.last_verify
                ? `${status.last_verify.deep ? 'Deep' : 'Standard'} verification${status.last_verify.hashes ? ' with payload hashes' : ''}; ${int(status.last_verify.parse_errors || 0)} parse errors.`
                : 'No truth-to-index verification has been recorded.'
            }
          />
          <CheckCard
            title="Backup"
            tone={backupTone}
            state={status.last_backup?.ok ? 'Mirrored' : status.last_backup ? 'Failed' : 'Missing'}
            at={status.last_backup?.at}
            detail={
              status.last_backup
                ? `${int(status.last_backup.files_copied)} files copied in the latest run.`
                : 'No durable truth mirror has been recorded.'
            }
          >
            {status.last_backup?.dest && (
              <code className="health-path" title={status.last_backup.dest}>
                {status.last_backup.dest}
              </code>
            )}
          </CheckCard>
          <CheckCard
            title="Recovery"
            tone={drillTone}
            state={status.last_restore_drill?.ok ? 'Restored' : status.last_restore_drill ? 'Failed' : 'Unproven'}
            at={status.last_restore_drill?.at}
            detail={
              status.last_restore_drill
                ? `A throwaway restore rebuilt ${int(status.last_restore_drill.events)} events${status.last_restore_drill.seconds != null ? ` in ${Math.round(status.last_restore_drill.seconds)}s` : ''}.`
                : 'No backup has been rebuilt and smoke-tested in isolation.'
            }
          />
        </div>
      </section>

      <section className="health-section" aria-labelledby="archives-heading">
        <div className="health-section-heading">
          <div>
            <p className="eyebrow">Archives</p>
            <h2 id="archives-heading">Archives on this machine</h2>
          </div>
          {loading && <span className="health-section-meta load-live">loading now</span>}
        </div>
        {archives === null ? (
          <div className="health-empty-card">reading the archive registry…</div>
        ) : archives.length === 0 ? (
          <div className="health-empty-card">
            No archives registered yet. An archive is registered the first time it is opened.
          </div>
        ) : (
          <div className="load-archive-list">
            {archives.map((archive) => {
              const state = indexState(archive)
              const live = liveLoad(archive)
              const running = live?.status === 'running'
              const phase = live ? currentPhase(live) : null
              const last = archive.runs?.[0]
              return (
                <article className={`load-archive ${state.tone}`} key={archive.id}>
                  <div className="load-archive-top">
                    <h3>
                      {archive.label}
                      {archive.role && (
                        <span
                          className="load-role-tag"
                          title="What this archive is for — a descriptive tag, set via `thread_archive archives --set-role`."
                        >
                          {archive.role}
                        </span>
                      )}
                      <span
                        className={`load-served-tag${archive.active ? '' : ' off'}`}
                        title={
                          archive.active
                            ? 'Retrieval answers from this archive — it is the home this process was started against.'
                            : 'Indexed but not reachable: retrieval answers only from the served archive.'
                        }
                      >
                        {archive.active ? 'served' : 'not served'}
                      </span>
                    </h3>
                    <Pill tone={state.tone}>{state.label}</Pill>
                  </div>
                  <code className="health-path" title={archive.home}>{archive.home}</code>
                  <div className="load-archive-meta">
                    <span>{bytes(archive.index_bytes)} index</span>
                    <span title={dateTime(archive.last_opened)}>opened {age(archive.last_opened)}</span>
                  </div>
                  {running && phase && <PhaseProgress phase={phase} />}
                  {!running && last && (
                    <div className="load-archive-last">
                      <span className="load-last-label">
                        last load · {last.kind} · {age(last.at)}
                      </span>
                      <span className="load-last-total">{duration(last.duration_s)}</span>
                      <PhaseChips phases={last.phases || []} />
                    </div>
                  )}
                  {!running && !last && (
                    <p className="load-archive-none">No tracked load has been recorded for this archive.</p>
                  )}
                </article>
              )
            })}
          </div>
        )}
        <p className="health-footnote">
          A load builds one archive's derived data — index, lexical search, vectors — from its truth
          log. Lexical search is usable as soon as the index is built; the embed phase backfills
          semantic search behind it. Being indexed does not make an archive reachable:{' '}
          <code>thread_search</code> and <code>thread_read</code> answer from the single archive their
          process was started against, marked <em>served</em> here.
        </p>
      </section>

      <section className="health-section" aria-labelledby="load-history-heading">
        <div className="health-section-heading">
          <div>
            <p className="eyebrow">Load history</p>
            <h2 id="load-history-heading">What past loads cost</h2>
          </div>
        </div>
        {history.length ? (
          <div className="health-table-wrap">
            <table className="health-table">
              <thead>
                <tr>
                  <th>Archive</th>
                  <th>Load</th>
                  <th>Finished</th>
                  <th className="num">Duration</th>
                  <th>Phases</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {history.map(({ archive, run }, index) => (
                  <tr key={`${archive}-${run.at}-${index}`}>
                    <td className="health-provider">{archive}</td>
                    <td>{run.kind}</td>
                    <td title={dateTime(run.at)}>{age(run.at)}</td>
                    <td className="num">{duration(run.duration_s)}</td>
                    <td><PhaseChips phases={run.phases || []} /></td>
                    <td>
                      <Pill tone={run.status === 'ok' ? 'good' : 'bad'}>
                        {run.status === 'ok' ? 'Complete' : run.status}
                      </Pill>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="health-empty-card">
            No loads recorded yet. Import, reindex, and embed runs are logged here as they happen.
          </div>
        )}
      </section>

      <section className="health-section" aria-labelledby="providers-heading">
        <div className="health-section-heading">
          <div>
            <p className="eyebrow">Capture coverage</p>
            <h2 id="providers-heading">Providers observed by the watcher</h2>
          </div>
          <span className="health-section-meta" title={dateTime(status.last_watch_pass?.at)}>
            checked {age(status.last_watch_pass?.at)}
          </span>
        </div>
        {providerRows.length ? (
          <div className="health-table-wrap">
            <table className="health-table">
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Support</th>
                  <th className="num">Sources checked</th>
                  <th className="num">Items imported</th>
                  <th className="num">Events captured</th>
                  <th className="num">Parse / watch errors</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {providerRows.map(([source, data]) => {
                  const tone = sourceTone(data)
                  return (
                    <tr key={source}>
                      <td className="health-provider">{source}</td>
                      <td>{supportTier(source)}</td>
                      <td className="num">{int(data.checked)}</td>
                      <td className="num">{int(data.items)}</td>
                      <td className="num">{int(data.events)}</td>
                      <td className="num">{int(data.parse_errors)} / {int(data.errors)}</td>
                      <td><Pill tone={tone}>{tone === 'good' ? 'Clean' : 'Degraded'}</Pill></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="health-empty-card">No available provider stores were observed in the latest pass.</div>
        )}
        <p className="health-footnote">
          Counters are cumulative since the current capture process started. Claude Code is the supported first-class source; other harnesses are best-effort and retain raw evidence for repair.
        </p>
      </section>

      <div className="health-detail-grid">
        <section className="health-detail" aria-labelledby="coverage-detail-heading">
          <div className="health-detail-title">
            <h2 id="coverage-detail-heading">Coverage audit</h2>
            <Pill tone={recordTone(status.last_coverage, 36 * HOUR)}>
              {status.last_coverage?.ok ? 'Passed' : status.last_coverage ? 'Failed' : 'Unproven'}
            </Pill>
          </div>
          <dl>
            <div><dt>Last run</dt><dd title={dateTime(status.last_coverage?.at)}>{age(status.last_coverage?.at)}</dd></div>
            <div><dt>Sources checked</dt><dd>{int(status.last_coverage?.sources_checked)}</dd></div>
            <div><dt>Recent skips</dt><dd>{int(status.last_coverage?.skips_recent)}</dd></div>
            <div><dt>Recent drift</dt><dd>{int(status.last_coverage?.drift_recent)}</dd></div>
          </dl>
          <Command>thread_archive coverage</Command>
        </section>

        <section className="health-detail" aria-labelledby="mirror-heading">
          <div className="health-detail-title">
            <h2 id="mirror-heading">Raw source mirror</h2>
            <Pill tone={recordTone(status.last_source_mirror, 36 * HOUR)}>
              {status.last_source_mirror?.ok ? 'Current' : status.last_source_mirror ? 'Failed' : 'Unproven'}
            </Pill>
          </div>
          <dl>
            <div><dt>Last run</dt><dd title={dateTime(status.last_source_mirror?.at)}>{age(status.last_source_mirror?.at)}</dd></div>
            <div><dt>Files seen</dt><dd>{int(status.last_source_mirror?.files)}</dd></div>
            <div><dt>Files copied</dt><dd>{int(status.last_source_mirror?.copied)}</dd></div>
            <div><dt>Bytes written</dt><dd>{bytes(status.last_source_mirror?.bytes_out)}</dd></div>
          </dl>
        </section>

        <section className="health-detail" aria-labelledby="storage-heading">
          <div className="health-detail-title">
            <h2 id="storage-heading">Local archive</h2>
            <Pill tone="quiet">On this machine</Pill>
          </div>
          <dl>
            <div><dt>Conversations</dt><dd>{int(status.threads)}</dd></div>
            <div><dt>Events</dt><dd>{int(status.events)}</dd></div>
            <div><dt>Lexically indexed</dt><dd>{int(status.fts_indexed)}</dd></div>
            <div><dt>Vector indexed</dt><dd>{int(status.vectors_indexed)}</dd></div>
          </dl>
          <code className="health-path" title={status.home}>{status.home}</code>
        </section>

        <section className="health-detail" aria-labelledby="update-heading">
          <div className="health-detail-title">
            <h2 id="update-heading">Software update</h2>
            <Pill tone={
              status.last_self_update?.action === 'update'
                ? 'warn'
                : status.last_self_update?.ok === false
                  ? 'bad'
                  : 'quiet'
            }>
              {status.last_self_update?.action || 'Not checked'}
            </Pill>
          </div>
          <dl>
            <div><dt>Installed</dt><dd>{status.last_self_update?.current ? `v${status.last_self_update.current.replace(/^v/, '')}` : '—'}</dd></div>
            <div><dt>Available</dt><dd>{status.last_self_update?.tag || '—'}</dd></div>
            <div><dt>Last check</dt><dd title={dateTime(status.last_self_update?.at)}>{age(status.last_self_update?.at)}</dd></div>
          </dl>
          <Command>thread_archive self-update --check</Command>
        </section>
      </div>
    </div>
  )
}
