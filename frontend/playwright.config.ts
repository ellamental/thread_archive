import { defineConfig, devices } from '@playwright/test'

/**
 * Browser tests run against a production Vite build, with the JSON API mocked
 * at the browser network boundary. This exercises the real router, browser
 * fetches, CSS, and event handling without reading a developer's live archive.
 * The dedicated output directory also leaves the committed package bundle
 * untouched.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 4 : 3,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4174',
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
      'npx tsc --noEmit && npx vite build --outDir .e2e-dist && npx vite preview --outDir .e2e-dist --host 127.0.0.1 --port 4174 --strictPort',
    // The dev panels are mounted only for a viewer whose operator asked for
    // them, and in a served viewer that answer comes from the archive's config
    // — which a static preview has no server to read. This build stamps the
    // shell itself (see vite.config.ts), so the browser suite can drive
    // `/retrieval` and `/lab` the way an operator running with them on does.
    env: { ARCHIVE_DEV_PANELS: '1' },
    url: 'http://127.0.0.1:4174',
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
