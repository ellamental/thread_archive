import { expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import type { DropZone } from '../api'
import { UploadView } from '../components/UploadView'
import { http, HttpResponse, mswHandler, mswJson } from './msw'

const DUMPS = '/Users/test/.thread/archive/dumps'

function emptyZone(): DropZone {
  return { dumps_dir: DUMPS, waiting: [], imported: [], failed: [] }
}

function view() {
  return render(
    <MemoryRouter>
      <UploadView />
    </MemoryRouter>,
  )
}

function exportZip(name = 'claude-export.zip') {
  return new File(['PK pretend zip'], name, { type: 'application/zip' })
}

/** Serve /api/drops from a value the test can change between polls. */
function servingDrops(initial: DropZone) {
  const state = { zone: initial }
  mswHandler(http.get('/api/drops', () => HttpResponse.json(state.zone)))
  return state
}

function dropInput() {
  return screen.getByLabelText(/drop your export zip here/i)
}

it('tells the reader where each provider hides its export', async () => {
  mswJson('/api/drops', emptyZone())
  view()

  expect(screen.getByRole('heading', { name: 'Import an account export' })).toBeInTheDocument()
  expect(screen.getByText('Settings → Account → Export Data')).toBeInTheDocument()
  expect(screen.getByText('Settings → Data Controls → Export Data')).toBeInTheDocument()
  expect(screen.getByText('Settings → Data Controls → Download Your Data')).toBeInTheDocument()
  // The hand-drop escape hatch is only useful if the folder is named.
  expect(await screen.findByText(DUMPS)).toBeInTheDocument()
})

it('lists what the drop folder already holds', async () => {
  mswJson('/api/drops', {
    dumps_dir: DUMPS,
    waiting: [{ name: 'chatgpt.zip', bytes: 3_500_000_000, at: '2026-07-26T10:00:00Z' }],
    imported: [{ name: 'claude.zip', bytes: 4096, at: '2026-07-25T10:00:00Z', kind: 'claude' }],
    failed: [
      { name: 'mystery.zip', bytes: 12, at: null },
      // An export someone unpacked into a folder: no size, and a directory is
      // exactly the shape this page cannot upload but the drop folder accepts.
      { name: 'data-2026-batch-0001', bytes: null, at: '2026-07-24T10:00:00Z' },
    ],
  })
  view()

  expect(await screen.findByText('chatgpt.zip')).toBeInTheDocument()
  expect(screen.getByText('mystery.zip')).toBeInTheDocument()
  expect(screen.getByText('data-2026-batch-0001')).toBeInTheDocument()
  expect(screen.getByText('claude.zip')).toBeInTheDocument()
  expect(screen.getByText(/claude · 4 KB/)).toBeInTheDocument()
  expect(screen.getByText(/3\.26 GB/)).toBeInTheDocument()
  expect(screen.getByText('12 B')).toBeInTheDocument()
})

it('uploads a chosen file under its own name, with the write guard header', async () => {
  const seen: { name: string | null; guard: string | null } = { name: null, guard: null }
  servingDrops(emptyZone())
  mswHandler(
    http.post('/api/upload', ({ request }) => {
      seen.name = new URL(request.url).searchParams.get('name')
      seen.guard = request.headers.get('X-Archive-Upload')
      return HttpResponse.json({
        name: 'claude-export.zip', kind: 'claude', label: 'claude.ai',
        bytes: 20, dumps_dir: DUMPS,
      })
    }),
  )
  view()

  await userEvent.upload(dropInput(), exportZip())

  await waitFor(() => expect(seen.name).toBe('claude-export.zip'))
  expect(seen.guard).toBe('1')
  expect(await screen.findByText('waiting for the importer')).toBeInTheDocument()
})

it('follows an accepted upload through to imported', async () => {
  const state = servingDrops(emptyZone())
  mswHandler(
    http.post('/api/upload', () => {
      // The watcher has not looked yet: the drop is sitting in the zone.
      state.zone = {
        ...emptyZone(),
        waiting: [{ name: 'claude-export.zip', bytes: 20, at: '2026-07-26T12:00:00Z' }],
      }
      return HttpResponse.json({
        name: 'claude-export.zip', kind: 'claude', label: 'claude.ai',
        bytes: 20, dumps_dir: DUMPS,
      })
    }),
  )
  view()

  await userEvent.upload(dropInput(), exportZip())
  expect(await screen.findByText('waiting for the importer')).toBeInTheDocument()

  // Taken out of the zone and not yet anywhere else: the import is running.
  state.zone = emptyZone()
  expect(await screen.findByText('importing…', {}, { timeout: 9000 })).toBeInTheDocument()

  // Imported, and the download retained as the recovery copy — which the page
  // learns by polling the folder, not by being told.
  state.zone = {
    ...emptyZone(),
    imported: [{ name: 'claude-export.zip', bytes: 20, at: '2026-07-26T12:00:05Z', kind: 'claude' }],
  }
  expect(await screen.findByText('imported', {}, { timeout: 9000 })).toBeInTheDocument()
}, 25_000)

it('takes a file dragged onto the zone, and lights up while it is over', async () => {
  const seen: string[] = []
  servingDrops(emptyZone())
  mswHandler(
    http.post('/api/upload', ({ request }) => {
      seen.push(new URL(request.url).searchParams.get('name') ?? '')
      return HttpResponse.json({
        name: 'grok-export.zip', kind: 'grok', label: 'xAI (Grok)',
        bytes: 14, dumps_dir: DUMPS,
      })
    }),
  )
  const { container } = view()
  const zone = container.querySelector('.dropzone') as HTMLElement

  fireEvent.dragOver(zone)
  expect(zone).toHaveClass('dragging')
  fireEvent.dragLeave(zone)
  expect(zone).not.toHaveClass('dragging')

  fireEvent.drop(zone, { dataTransfer: { files: [exportZip('grok-export.zip')] } })

  await waitFor(() => expect(seen).toEqual(['grok-export.zip']))
  // The drag highlight clears once the file is taken, not on the next hover.
  expect(zone).not.toHaveClass('dragging')
  expect(await screen.findByText(/xAI \(Grok\)/)).toBeInTheDocument()
})

it('reports a quarantined drop as needing a look', async () => {
  const state = servingDrops(emptyZone())
  mswHandler(
    http.post('/api/upload', () => {
      state.zone = {
        ...emptyZone(),
        failed: [{ name: 'claude-export.zip', bytes: 20, at: '2026-07-26T12:00:05Z' }],
      }
      return HttpResponse.json({
        name: 'claude-export.zip', kind: 'claude', label: 'claude.ai',
        bytes: 20, dumps_dir: DUMPS,
      })
    }),
  )
  view()

  await userEvent.upload(dropInput(), exportZip())

  expect(await screen.findByText(/needs a look/)).toBeInTheDocument()
})

it('shows the server’s own reason when an upload is refused', async () => {
  mswJson('/api/drops', emptyZone())
  mswHandler(
    http.post(
      '/api/upload',
      () => new HttpResponse('not a recognized account export (claude.ai, ChatGPT, xAI)', {
        status: 415,
      }),
    ),
  )
  view()

  await userEvent.upload(dropInput(), exportZip('holiday-photos.zip'))

  expect(await screen.findByText('not accepted')).toBeInTheDocument()
  expect(screen.getByText(/not a recognized account export/)).toBeInTheDocument()
})
