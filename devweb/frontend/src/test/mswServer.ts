/**
 * Shared MSW server instance for the vitest suite.
 *
 * Lifecycle is wired in `src/test/setup.ts`:
 *   - beforeAll: server.listen({ onUnhandledRequest: 'error' })
 *   - afterEach: server.resetHandlers()
 *   - afterAll:  server.close()
 *
 * Tests register per-test handlers with `server.use(http.get('/api/…', …))`.
 * Nothing is registered here: with no default handlers, every request a test
 * makes must be stubbed by that test, and anything unmatched is an error.
 */
import { setupServer } from 'msw/node'

export const server = setupServer()
