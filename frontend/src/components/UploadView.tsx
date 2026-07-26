import { useCallback, useEffect, useRef, useState } from 'react'
import { api, uploadExport, type DropEntry, type DropZone } from '../api'

// The live CLI harnesses are tailed continuously; a provider's *web* history is
// only ever a download you ask for. These are the exact paths to that download,
// kept in the same words the importer's own module uses.
const PROVIDERS = [
  {
    name: 'Claude',
    where: 'claude.ai',
    path: 'Settings → Account → Export Data',
    detail: 'Anthropic emails a download link. The file arrives as a ZIP.',
  },
  {
    name: 'ChatGPT',
    where: 'chatgpt.com',
    path: 'Settings → Data Controls → Export Data',
    detail: 'OpenAI emails a link that expires after 24 hours. Download the ZIP before it does.',
  },
  {
    name: 'Grok',
    where: 'grok.com / x.com',
    path: 'Settings → Data Controls → Download Your Data',
    detail: 'xAI prepares the archive and emails it. The file arrives as a ZIP.',
  },
]

// How often the drop zone is re-read while something is in flight. The import
// runs in the watcher, so this listing is the only thing that can report it —
// but it is worth asking only while there is an answer coming.
const POLL_MS = 3000

type Phase = 'uploading' | 'queued' | 'rejected'

interface Upload {
  id: number
  filename: string
  bytes: number
  phase: Phase
  progress: number
  // Set once the server takes it: the name it landed under (which may be
  // numbered away from a collision) and the export it was recognized as.
  droppedAs?: string
  label?: string
  message?: string
}

function fmtBytes(n: number | null): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

function fmtWhen(iso: string | null): string {
  if (!iso) return ''
  const when = new Date(iso)
  return isNaN(when.getTime())
    ? ''
    : when.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function has(entries: DropEntry[], name?: string): boolean {
  return !!name && entries.some((entry) => entry.name === name)
}

// What an accepted upload has become, read off the drop zone rather than
// assumed: the watcher moves the file, so its location is the status.
//
// A drop in none of the three lists is mid-import — the watcher has taken it out
// of the zone and not yet finished — but only if we ever saw it waiting. Before
// that, the listing is simply older than the upload, and calling that "importing"
// would claim progress from a stale read.
function settledState(drops: DropZone | null, upload: Upload, seenWaiting: Set<string>) {
  const name = upload.droppedAs
  if (!drops || !name) return null
  if (has(drops.failed, name)) return { tone: 'bad', text: 'needs a look — quarantined in failed/' }
  if (has(drops.imported, name)) return { tone: 'good', text: 'imported' }
  if (has(drops.waiting, name) || !seenWaiting.has(name))
    return { tone: 'busy', text: 'waiting for the importer' }
  return { tone: 'busy', text: 'importing…' }
}

function DropList({ title, entries, empty }: { title: string; entries: DropEntry[]; empty: string }) {
  return (
    <div className="drop-list">
      <h3>{title}</h3>
      {entries.length === 0 && <p className="drop-empty">{empty}</p>}
      {entries.map((entry) => (
        <div className="drop-row" key={entry.name}>
          <span className="drop-name">{entry.name}</span>
          <span className="drop-meta">
            {[entry.kind, fmtBytes(entry.bytes), fmtWhen(entry.at)].filter(Boolean).join(' · ')}
          </span>
        </div>
      ))}
    </div>
  )
}

export function UploadView() {
  const [drops, setDrops] = useState<DropZone | null>(null)
  const [uploads, setUploads] = useState<Upload[]>([])
  const [dragging, setDragging] = useState(false)
  const nextId = useRef(1)
  // Uploads run one at a time: a browser sending three multi-gigabyte files at
  // once just makes all three slower, and the progress bars less honest.
  const queue = useRef<Promise<unknown>>(Promise.resolve())
  // Drops this page has watched sit in the zone — what tells a not-yet-listed
  // upload apart from one the importer has picked up. See settledState.
  const seenWaiting = useRef<Set<string>>(new Set())

  const refresh = useCallback(
    () =>
      api
        .drops()
        .then((zone) => {
          for (const entry of zone.waiting) seenWaiting.current.add(entry.name)
          setDrops(zone)
        })
        .catch(() => undefined),
    [],
  )

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Poll only while something is still moving. An upload that was refused,
  // imported, or quarantined has arrived somewhere final — polling past that
  // would keep asking a question with a settled answer for as long as the page
  // is open.
  const busy =
    uploads.some((upload) => upload.phase === 'uploading') ||
    uploads.some(
      (upload) => settledState(drops, upload, seenWaiting.current)?.tone === 'busy',
    ) ||
    (drops?.waiting.length ?? 0) > 0

  useEffect(() => {
    if (!busy) return
    const timer = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [busy, refresh])

  const update = useCallback((id: number, patch: Partial<Upload>) => {
    setUploads((current) =>
      current.map((upload) => (upload.id === id ? { ...upload, ...patch } : upload)),
    )
  }, [])

  const enqueue = useCallback(
    (files: File[]) => {
      for (const file of files) {
        const id = nextId.current++
        setUploads((current) => [
          ...current,
          { id, filename: file.name, bytes: file.size, phase: 'uploading', progress: 0 },
        ])
        queue.current = queue.current.then(() =>
          uploadExport(file, (fraction) => update(id, { progress: fraction })).then(
            (accepted) => {
              update(id, {
                phase: 'queued',
                progress: 1,
                droppedAs: accepted.name,
                label: accepted.label,
              })
              return refresh()
            },
            (error: Error) => update(id, { phase: 'rejected', message: error.message }),
          ),
        )
      }
    },
    [refresh, update],
  )

  const onDrop = (event: React.DragEvent) => {
    event.preventDefault()
    setDragging(false)
    enqueue([...event.dataTransfer.files])
  }

  return (
    <div className="wrap upload-page">
      <h1>Import an account export</h1>
      <p className="upload-lede">
        Your CLI and editor sessions are captured as they happen. Conversations you had on the{' '}
        <em>web</em> — claude.ai, ChatGPT, Grok — exist only in your account until you download
        them. Drop that download here and every conversation in it becomes a searchable thread.
      </p>

      <label
        className={'dropzone' + (dragging ? ' dragging' : '')}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input
          type="file"
          className="dropzone-input"
          accept=".zip,application/zip"
          multiple
          onChange={(event) => {
            enqueue([...(event.target.files ?? [])])
            event.target.value = ''
          }}
        />
        <span className="dropzone-title">Drop your export ZIP here</span>
        <span className="dropzone-sub">or click to choose a file</span>
      </label>

      {uploads.length > 0 && (
        <div className="upload-queue">
          {uploads.map((upload) => {
            const settled = settledState(drops, upload, seenWaiting.current)
            return (
              <div className={'upload-row ' + (settled?.tone ?? upload.phase)} key={upload.id}>
                <div className="upload-row-top">
                  <span className="upload-name">{upload.droppedAs || upload.filename}</span>
                  <span className="upload-state">
                    {upload.phase === 'uploading' &&
                      `uploading… ${Math.round(upload.progress * 100)}%`}
                    {upload.phase === 'rejected' && 'not accepted'}
                    {upload.phase === 'queued' && (settled?.text ?? 'waiting for the importer')}
                  </span>
                </div>
                {upload.phase === 'uploading' && (
                  <div
                    className="upload-bar"
                    role="progressbar"
                    aria-label={`uploading ${upload.filename}`}
                    aria-valuenow={Math.round(upload.progress * 100)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <div className="upload-bar-fill" style={{ width: `${upload.progress * 100}%` }} />
                  </div>
                )}
                <div className="upload-detail">
                  {upload.phase === 'rejected'
                    ? upload.message
                    : [upload.label, fmtBytes(upload.bytes)].filter(Boolean).join(' · ')}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <section className="upload-section">
        <h2>What happens next</h2>
        <p>
          An upload lands in the archive's drop folder, where the importer picks it up within
          a few seconds. Every conversation in the export becomes its own thread, and re-uploading
          a later export merges into what is already there — conversations that grew gain their new
          messages, unchanged ones import nothing. Importing a large export takes a while; it runs
          in the background, so you can leave this page.
        </p>
        <p>
          The download itself is kept as the recovery copy of the most recent export per provider,
          because normalizing is lossy in ways the importer can't always see. An export that fails,
          or that had any conversation the importer choked on, is quarantined for review instead —
          never deleted.
        </p>
      </section>

      <section className="upload-section">
        <h2>Getting your export</h2>
        <div className="provider-cards">
          {PROVIDERS.map((provider) => (
            <article className="provider-card" key={provider.name}>
              <h3>{provider.name}</h3>
              <p className="provider-where">{provider.where}</p>
              <p className="provider-path">{provider.path}</p>
              <p className="provider-detail">{provider.detail}</p>
            </article>
          ))}
        </div>
        <p className="upload-note">
          Upload the ZIP exactly as it downloaded — unpacking or repacking it changes the shape
          the importer recognizes.
        </p>
      </section>

      <section className="upload-section">
        <h2>The drop folder</h2>
        {drops === null && <p className="drop-empty">Reading the drop folder…</p>}
        {drops && (
          <>
            <p>
              Uploads go here, and so can anything you copy in yourself — useful for a file this
              page won't take, or an export you already unpacked into a folder.
            </p>
            <p className="drop-path">{drops.dumps_dir}</p>
            <DropList
              title="Waiting to import"
              entries={drops.waiting}
              empty="Nothing waiting."
            />
            <DropList
              title="Needs a look"
              entries={drops.failed}
              empty="Nothing quarantined."
            />
            <DropList
              title="Kept as recovery copies"
              entries={drops.imported}
              empty="No exports imported yet."
            />
          </>
        )}
      </section>
    </div>
  )
}
