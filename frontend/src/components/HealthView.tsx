import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type DiskEntry,
  type DiskUsage,
  type HealthRecord,
  type LibraryEntry,
  type LoadPhase,
  type LoadRun,
  type LoadStatus,
  type Notice,
  type NoticeBoard,
  type Status,
  type WatchSourceRecord,
} from '../api'

// 'busy' is work in flight — distinct from 'warn', which means someone must act.
type Tone = 'good' | 'warn' | 'bad' | 'quiet' | 'busy'

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

function recordTone(record: HealthRecord | null, staleAfter: number): Tone {
  if (!record) return 'bad'
  if (record.ok === false) return 'bad'
  const ms = elapsed(record.at)
  if (ms == null || ms > staleAfter) return 'warn'
  return 'good'
}

// 'quiet' for an uninstalled extra and 'warn' for a missing base library: the first is
// a choice about what this install does, the second is an install that didn't finish.
function libraryTone(library: LibraryEntry): Tone {
  if (library.state === 'degraded') return 'warn'
  return library.state === 'ok' ? 'good' : 'quiet'
}

function libraryState(library: LibraryEntry): string {
  if (library.state === 'ok') return 'Active'
  return library.state === 'degraded' ? 'Missing' : 'Not installed'
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

/** One notice, with the control that puts it aside (or brings it back).
 *
 *  The action is a button on the card rather than a separate mode: deciding a
 *  warning is understood is the same glance as reading it.
 */
function NoticeCard({
  notice,
  silenced,
  busy,
  onToggle,
}: {
  notice: Notice
  silenced?: boolean
  busy: boolean
  onToggle: (key: string, silence: boolean) => void
}) {
  return (
    <article className={`health-notice ${notice.tone}${silenced ? ' silenced' : ''}`}>
      <span className="health-notice-mark" aria-hidden="true">
        {notice.tone === 'bad' ? '×' : notice.tone === 'warn' ? '!' : '↑'}
      </span>
      <div className="health-notice-body">
        <h3>{notice.title}</h3>
        <p>{notice.detail}</p>
        {notice.command && <Command>{notice.command}</Command>}
        {silenced && notice.silenced_at && (
          <p className="health-notice-since" title={dateTime(notice.silenced_at)}>
            silenced {age(notice.silenced_at)}
          </p>
        )}
      </div>
      <button
        type="button"
        className="health-silence"
        disabled={busy}
        title={
          silenced
            ? 'Show this in the action queue again'
            : 'Hide this until the condition changes or clears'
        }
        onClick={() => onToggle(notice.key, !silenced)}
      >
        {silenced ? 'Unsilence' : 'Silence'}
      </button>
    </article>
  )
}

// How often this page re-reads. A load in flight is the one thing here that
// changes by the second, so the archive list tracks it closely; everything else
// (and the archive list when nothing is loading) refreshes on the idle cadence,
// which is what keeps the ages and staleness verdicts honest while the page sits
// open.
const LOADING_POLL_MS = 2_000
const IDLE_POLL_MS = 30_000
// Storage is the one figure here that costs a directory walk to produce, and the
// one that moves in hours rather than seconds. Its own slow cadence keeps the
// page's 30s heartbeat from re-walking the home forty times an hour.
const DISK_POLL_MS = 5 * 60_000

// The kinds a byte can be, in the order the meter stacks them: least disposable
// first, so the bar reads left to right as what must be kept → what could be
// reclaimed. The note is the reason the kind exists, which is what makes the
// number actionable — a share of the disk means nothing without knowing whether
// deleting it loses anything.
const DISK_KINDS: { key: DiskEntry['kind']; label: string; note: string }[] = [
  { key: 'truth', label: 'Truth', note: 'the conversations themselves — irreplaceable' },
  { key: 'index', label: 'Index', note: 'derived; rebuilds from truth' },
  { key: 'sources', label: 'Raw sources', note: 'provider files and drift quarantine, never auto-pruned' },
  { key: 'other', label: 'Other', note: 'logs, telemetry, migration payloads, caches' },
]

/** Storage as one proportional bar plus the entries behind it.
 *
 *  Segments are labeled in the legend and listed in the table below rather than
 *  written into the bar: two of the four fills sit under 3:1 against the light
 *  panel, and at these widths a segment can be a few pixels wide anyway.
 */
function StorageSection({ disk }: { disk: DiskUsage | null }) {
  const total = disk?.total_bytes || 0
  const share = (n: number) => (total > 0 ? (n / total) * 100 : 0)
  return (
    <section className="health-section" aria-labelledby="storage-usage-heading">
      <div className="health-section-heading">
        <div>
          <p className="eyebrow">Storage</p>
          <h2 id="storage-usage-heading">What this archive costs on disk</h2>
        </div>
        {disk && (
          <span className="health-section-meta">
            {bytes(disk.total_bytes)} · {int(disk.files)} files
          </span>
        )}
      </div>
      {!disk ? (
        <div className="health-empty-card">measuring the archive home…</div>
      ) : (
        <>
          <div
            className="disk-meter"
            role="img"
            aria-label={DISK_KINDS.map(
              (k) => `${k.label} ${bytes(disk.kinds[k.key])}`,
            ).join(', ')}
          >
            {DISK_KINDS.map((kind) => (
              <div
                key={kind.key}
                className={`disk-seg ${kind.key}`}
                style={{ width: `${share(disk.kinds[kind.key])}%` }}
                title={`${kind.label}: ${bytes(disk.kinds[kind.key])} — ${kind.note}`}
              />
            ))}
          </div>
          <div className="disk-legend">
            {DISK_KINDS.map((kind) => (
              <div className="disk-legend-item" key={kind.key}>
                <span className={`disk-swatch ${kind.key}`} aria-hidden="true" />
                <span className="disk-legend-name">{kind.label}</span>
                <span className="disk-legend-size">
                  {bytes(disk.kinds[kind.key])} · {share(disk.kinds[kind.key]).toFixed(0)}%
                </span>
                <span className="disk-legend-note">{kind.note}</span>
              </div>
            ))}
          </div>
          <div className="health-table-wrap disk-table-wrap">
            <table className="health-table">
              <thead>
                <tr>
                  <th>Entry</th>
                  <th>Kind</th>
                  <th className="num">Size</th>
                  <th className="num">Share</th>
                </tr>
              </thead>
              <tbody>
                {disk.entries.map((entry) => (
                  <tr key={entry.name}>
                    <td className="health-provider">
                      <span className={`disk-swatch ${entry.kind}`} aria-hidden="true" />
                      {entry.name}
                    </td>
                    <td>{DISK_KINDS.find((k) => k.key === entry.kind)?.label ?? entry.kind}</td>
                    <td className="num">{bytes(entry.bytes)}</td>
                    <td className="num">{share(entry.bytes).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {disk.external.length > 0 && (
            <p className="health-footnote">
              Counted from outside the home: {disk.external.join(' · ')}
            </p>
          )}
          <p className="health-footnote">
            {bytes(disk.rebuildable_bytes)} of this is index, which <code>thread_archive reindex</code>{' '}
            and <code>thread_archive embed</code> rebuild from truth. Raw sources and the drift
            quarantine are kept on purpose — they outlive what the harnesses delete — so pruning
            them is a decision the archive leaves to you.
          </p>
        </>
      )}
    </section>
  )
}

export function HealthView() {
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loads, setLoads] = useState<LoadStatus | null>(null)
  const [disk, setDisk] = useState<DiskUsage | null>(null)
  const [board, setBoard] = useState<NoticeBoard | null>(null)
  const [showSilenced, setShowSilenced] = useState(false)
  // The notice a silence/unsilence is in flight for, and the reason the last one
  // failed. A write that didn't reach disk must say so: the poll below would
  // otherwise quietly restore the card and read as a button that does nothing.
  const [silencing, setSilencing] = useState<string | null>(null)
  const [silenceError, setSilenceError] = useState<string | null>(null)

  // The status records are all read as ages ("last check 3m ago", stale past a
  // threshold), so a one-shot fetch would leave the page asserting a freshness
  // that decays the whole time it sits open. It re-reads on the idle cadence;
  // after the first success a failed poll keeps the last good evidence rather
  // than blanking the page.
  useEffect(() => {
    let cancelled = false
    let loaded = false
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      api
        .status()
        .then((s) => {
          if (cancelled) return
          loaded = true
          setStatus(s)
          setError(null)
        })
        .catch((e) => {
          if (!cancelled && !loaded) setError(String(e.message ?? e))
        })
        .finally(() => {
          if (!cancelled) timer = setTimeout(tick, IDLE_POLL_MS)
        })
    }
    tick()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [])

  // The action queue on the same idle cadence as the records behind it: a notice
  // is a judgment about ages, so a queue that never re-read would keep asserting
  // a verdict the page has already outgrown. Its own fetch because silencing
  // rewrites it out of band, and because it costs no index survey.
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      api
        .notices()
        .then((b) => {
          if (!cancelled) setBoard(b)
        })
        .catch(() => {
          // Keep the last good queue rather than blanking it — the checks below
          // are unaffected, and an empty action queue reads as "all clear".
        })
        .finally(() => {
          if (!cancelled) timer = setTimeout(tick, IDLE_POLL_MS)
        })
    }
    tick()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [])

  const loading = loads?.current?.status === 'running'

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      api
        .loads()
        .then((rows) => {
          if (!cancelled) setLoads(rows)
        })
        .catch(() => {
          // A load-ledger read failure must never blank the trust page — the
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

  // Disk usage on its own slow timer: a walk of the home, and a figure that
  // moves over hours. A failure leaves the section on its last good reading
  // rather than blanking it — the rest of the page's evidence is unaffected.
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      api
        .disk()
        .then((d) => {
          if (!cancelled) setDisk(d)
        })
        .catch(() => {})
        .finally(() => {
          if (!cancelled) timer = setTimeout(tick, DISK_POLL_MS)
        })
    }
    tick()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [])

  // The finished loads, newest first — what building this archive's derived data
  // has cost. Truncated: the page is evidence at a glance, not a full ledger.
  const history = useMemo(() => {
    const rows = [...(loads?.recent || [])]
    rows.sort((a, b) => String(b.at || '').localeCompare(String(a.at || '')))
    return rows.slice(0, 12)
  }, [loads])

  const live = loads?.current ?? null
  const livePhase = live ? currentPhase(live) : null

  // Both writes answer with the board the server committed, so the queue never
  // shows a state the store doesn't hold.
  const toggleSilence = (key: string, silence: boolean) => {
    setSilencing(key)
    setSilenceError(null)
    const call = silence ? api.silenceNotice(key) : api.unsilenceNotice(key)
    call
      .then(setBoard)
      .catch((e) => setSilenceError(String(e.message ?? e)))
      .finally(() => setSilencing(null))
  }

  const notices = board?.active || []
  const silenced = board?.silenced || []

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
  // The lede has to agree with the headline: claiming a backup can restore reads
  // as a contradiction on a page whose own queue says otherwise.
  const overallLede =
    overallTone === 'good'
      ? 'Live evidence that conversations are arriving, truth is intact, and a backup can actually restore.'
      : overallTone === 'warn'
        ? 'The evidence below still holds, but the warnings weaken it — clear them to keep a restore trustworthy.'
        : 'The chain from capture to restore has a gap, so the evidence below cannot be relied on yet. Start with the queue.'
  const providerRows = Object.entries(status.last_watch_pass?.sources || {})
  const libraries = status.libraries || []
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
          <p className="health-lede">{overallLede}</p>
        </div>
        <div className={`health-orb ${overallTone}`} aria-hidden="true">
          {overallTone === 'good' ? '✓' : overallTone === 'warn' ? '!' : '×'}
        </div>
      </header>

      {(notices.length > 0 || silenced.length > 0) && (
        <section className="health-actions" aria-labelledby="health-actions-heading">
          <div className="health-section-heading">
            <div>
              <p className="eyebrow">Action queue</p>
              <h2 id="health-actions-heading">
                {critical.length
                  ? `${critical.length} protection ${critical.length === 1 ? 'gap' : 'gaps'}`
                  : warnings.length
                    ? `${warnings.length} ${warnings.length === 1 ? 'warning' : 'warnings'}`
                    : notices.length
                      ? 'Maintenance available'
                      : 'Nothing needs attention'}
              </h2>
            </div>
            {/* The count is the honesty of the silencing: whatever is hidden is
                still on the page, one click from being read and restored. */}
            {silenced.length > 0 && (
              <button
                type="button"
                className="health-silenced-toggle"
                aria-expanded={showSilenced}
                onClick={() => setShowSilenced((open) => !open)}
              >
                {silenced.length} silenced
              </button>
            )}
          </div>
          {silenceError && <p className="health-silence-error">{silenceError}</p>}
          <div className="health-notices">
            {notices.map((notice) => (
              <NoticeCard
                key={notice.key}
                notice={notice}
                busy={silencing === notice.key}
                onToggle={toggleSilence}
              />
            ))}
          </div>
          {showSilenced && silenced.length > 0 && (
            <div className="health-silenced">
              <p className="health-silenced-note">
                Held aside by you. Each returns on its own if the condition changes or
                clears and comes back.
              </p>
              <div className="health-notices">
                {silenced.map((notice) => (
                  <NoticeCard
                    key={notice.key}
                    notice={notice}
                    silenced
                    busy={silencing === notice.key}
                    onToggle={toggleSilence}
                  />
                ))}
              </div>
            </div>
          )}
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

      <StorageSection disk={disk} />

      <section className="health-section" aria-labelledby="load-history-heading">
        <div className="health-section-heading">
          <div>
            <p className="eyebrow">Load history</p>
            <h2 id="load-history-heading">What past loads cost</h2>
          </div>
          {loading && <span className="health-section-meta load-live">loading now</span>}
        </div>
        {live && livePhase && (
          <div className="load-archive busy">
            <div className="load-archive-top">
              <h3>{live.kind} in flight</h3>
              <Pill tone={live.status === 'stalled' ? 'bad' : 'busy'}>
                {live.status === 'stalled' ? 'Stalled' : 'Loading'}
              </Pill>
            </div>
            <PhaseProgress phase={livePhase} />
            {live.status === 'stalled' && (
              <p className="load-archive-none">
                The process writing this state is gone — the load died mid-phase.
              </p>
            )}
          </div>
        )}
        {history.length ? (
          <div className="health-table-wrap">
            <table className="health-table">
              <thead>
                <tr>
                  <th>Load</th>
                  <th>Finished</th>
                  <th className="num">Duration</th>
                  <th>Phases</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {history.map((run, index) => (
                  <tr key={`${run.at}-${index}`}>
                    <td className="health-provider">{run.kind}</td>
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

        <section className="health-detail" aria-labelledby="libraries-heading">
          <div className="health-detail-title">
            <h2 id="libraries-heading">Search libraries</h2>
            <Pill tone={libraries.some((l) => l.state === 'degraded') ? 'warn' : 'good'}>
              {libraries.some((l) => l.state === 'degraded') ? 'Degraded' : 'Complete'}
            </Pill>
          </div>
          <dl>
            {libraries.map((library) => (
              <div key={library.name}>
                <dt title={library.capability}>{library.name}</dt>
                <dd title={library.detail}>
                  <Pill tone={libraryTone(library)}>{libraryState(library)}</Pill>
                </dd>
              </div>
            ))}
          </dl>
          <p className="health-footnote">
            Base libraries ship with the archive; an extra is installed on purpose. Search
            answers either way — a missing base library costs ranking quality silently, which
            is why it is listed here.
          </p>
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
