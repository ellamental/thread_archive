// The status bar polls the archive survey. Its job here is resilience: a single
// transient failure (a watcher restart cycling the cohosted server) must not latch
// a permanent "archive unavailable" banner — the next poll clears it.
import { describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { StatusBar, RETRY_MS } from '../components/StatusBar'
import { mswJson, mswError, mswHandler, http, HttpResponse } from './msw'

const SURVEY = { threads: 5, events: 42, topics: 2, fts_indexed: 8, vectors_indexed: 3, home: '/h' }

// Flush the in-flight fetch (a real microtask) and any timers due by `ms`.
async function advance(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('StatusBar', () => {
  it('renders the counts once the survey lands', async () => {
    vi.useFakeTimers()
    try {
      mswJson('/api/status', SURVEY)
      render(<StatusBar />)
      await advance()
      expect(screen.getByText(/5 threads · 42 events · 8 indexed · 3 vectors/)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('recovers from a transient failure without a reload', async () => {
    vi.useFakeTimers()
    try {
      let calls = 0
      mswHandler(
        http.get('/api/status', () => {
          calls += 1
          return calls === 1
            ? new HttpResponse('down', { status: 503 })
            : HttpResponse.json(SURVEY)
        }),
      )
      render(<StatusBar />)

      // First poll fails → the banner shows, but it is not the final word.
      await advance()
      expect(screen.getByText(/archive unavailable/)).toBeInTheDocument()

      // The scheduled retry fires and succeeds; the banner clears on its own.
      await advance(RETRY_MS)
      expect(screen.queryByText(/archive unavailable/)).not.toBeInTheDocument()
      expect(screen.getByText(/5 threads/)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the last good counts through a later blip', async () => {
    vi.useFakeTimers()
    try {
      let calls = 0
      mswHandler(
        http.get('/api/status', () => {
          calls += 1
          return calls === 1
            ? HttpResponse.json(SURVEY)
            : new HttpResponse('down', { status: 503 })
        }),
      )
      render(<StatusBar />)

      await advance()
      expect(screen.getByText(/5 threads/)).toBeInTheDocument()

      // A refresh later fails — the counts stay on screen, no red banner flash.
      await advance(60_000)
      expect(screen.getByText(/5 threads/)).toBeInTheDocument()
      expect(screen.queryByText(/archive unavailable/)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('surfaces the error while no survey has ever landed', async () => {
    vi.useFakeTimers()
    try {
      mswError('/api/status', 500, 'boom')
      render(<StatusBar />)
      await advance()
      expect(screen.getByText(/archive unavailable: 500: boom/)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
