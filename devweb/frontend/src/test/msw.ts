/**
 * MSW handler helpers. Per-test handler registration on the shared `server`
 * from `mswServer.ts`; `setup.ts` resets them (and the recorders below) between
 * tests, so nothing leaks.
 *
 * The viewer only ever GETs, so the helpers are GET-only. `path` is an MSW path
 * pattern matched against the request path — `'/api/search'`, or `'/api/thread/:id'`
 * to match any id. Query strings are ignored when matching; assert on them with
 * `recordRequests()`.
 *
 * Usage:
 *   mswJson('/api/search', { query: 'x', hits: [] })
 *   mswError('/api/thread/:id', 404, 'nope')
 *   mswPending('/api/search')                 // stays in flight: the loading state
 *
 * Escape hatch (arbitrary handler):
 *   mswHandler(http.get('/api/status', () => HttpResponse.json({ threads: 1 })))
 *
 * `onUnhandledRequest: 'error'` is on — a request no handler matches fails the
 * test loudly. Stub every request the code under test makes.
 */
import { delay, http, HttpResponse, type HttpHandler } from 'msw'
import { server } from './mswServer'

/** Answer a GET with JSON. */
export function mswJson(path: string, body: unknown, status = 200): void {
  server.use(
    http.get(path, () =>
      HttpResponse.json(body as Parameters<typeof HttpResponse.json>[0], { status }),
    ),
  )
}

/**
 * Answer a GET with an error status and a plain-text body — the shape `api.ts`
 * reads back via `response.text()` on a non-OK response.
 */
export function mswError(path: string, status: number, message = 'Error'): void {
  server.use(http.get(path, () => new HttpResponse(message, { status })))
}

/** Answer a GET with a request that never settles: the code under test stays loading. */
export function mswPending(path: string): void {
  server.use(
    http.get(path, async () => {
      await delay('infinite')
    }),
  )
}

/** Escape hatch: register arbitrary handlers (dynamic responses, non-GET, …). */
export function mswHandler(...handlers: HttpHandler[]): void {
  server.use(...handlers)
}

/**
 * Record every request path (pathname + query) the code under test issues, in
 * order, into the returned array — the MSW-side replacement for asserting on a
 * fetch spy's `mock.calls`. Listeners are removed in `setup.ts`'s afterEach.
 */
export function recordRequests(): string[] {
  const seen: string[] = []
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url)
    seen.push(url.pathname + url.search)
  })
  return seen
}

// Re-export the MSW primitives so a test composing an inline handler needs one import.
export { http, HttpResponse }
