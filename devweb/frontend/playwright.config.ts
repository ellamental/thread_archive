import { defineConfig, devices } from '@playwright/test'

/**
 * Browser tests for the dev panels, against a production Vite build with the
 * JSON API mocked at the browser network boundary — the real router, real
 * fetches, real CSS, and never the operator's live archive or the bench's real
 * run ledger.
 *
 * Port 4175, not the viewer's 4174: the two suites are separate lanes and may
 * run at once.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 4 : 3,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4175',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command:
      'npx tsc --noEmit && npx vite build --outDir .e2e-dist && npx vite preview --outDir .e2e-dist --host 127.0.0.1 --port 4175 --strictPort',
    url: 'http://127.0.0.1:4175',
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
