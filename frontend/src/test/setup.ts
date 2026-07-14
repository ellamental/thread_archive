// Vitest setup (wired via vite.config.ts `test.setupFiles`): registers the
// jest-dom matchers (and their TS augmentations — this file is inside tsconfig's
// include, so `tsc --noEmit` sees the matcher types too), unmounts rendered
// trees between tests (RTL only auto-registers its cleanup when the runner
// exposes globals, which we keep off), and runs the MSW lifecycle.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach } from 'vitest'
import { server } from './mswServer'

// Every request a test makes must be stubbed. Two layers hold that, because the
// viewer's components catch their own fetch errors and render an error state
// (Sidebar swallows outright) — a rejected request alone can therefore leave a
// test green:
//
//   1. `onUnhandledRequest: 'error'` — MSW logs the unmatched request and
//      rejects the fetch, so anything awaiting a response blows up.
//   2. the `request:unhandled` recorder below — reds the test even when the
//      component swallowed the rejection, which is the silent-pass case.
let unhandled: string[] = []

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' })
})

beforeEach(() => {
  unhandled = []
  server.events.on('request:unhandled', ({ request }) => {
    unhandled.push(`${request.method} ${new URL(request.url).pathname}`)
  })
})

afterEach(() => {
  cleanup()
  server.resetHandlers()
  const stray = unhandled
  // Also drops the `request:start` listeners `recordRequests()` attaches —
  // handler resets don't touch listeners, so a recorder would outlive its test.
  server.events.removeAllListeners()
  if (stray.length > 0) {
    throw new Error(
      `unmocked request(s): ${stray.join(', ')}\n` +
        'Every request the code under test makes must have an MSW handler — see src/test/msw.ts.',
    )
  }
})

afterAll(() => {
  server.close()
})
