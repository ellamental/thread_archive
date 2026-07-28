/// <reference types="vitest/config" />
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Stamps the shell with the dev-panel marker `src/dev.ts` reads.
 *
 * In the shipped viewer that stamp comes from the server, off the operator's
 * config line, so a production build must not carry it — the whole point is
 * that an install has no dev panels. Two places want it anyway: `npm run dev`,
 * where working on those pages is the reason the dev server is up, and the
 * browser suite's build (`ARCHIVE_DEV_PANELS=1`, set by playwright.config.ts),
 * which drives them through a real production bundle.
 */
function devPanelsMeta(): Plugin {
  return {
    name: 'thread-archive:dev-panels-meta',
    transformIndexHtml(_html, ctx) {
      const on = ctx.server != null || process.env.ARCHIVE_DEV_PANELS === '1'
      return on
        ? [{
            tag: 'meta',
            attrs: { name: 'thread-archive-dev-panels', content: '1' },
            injectTo: 'head' as const,
          }]
        : []
    },
  }
}

// The viewer is served by the watcher's cohosted stdlib server (`archive watch
// --web`) from the package's static dir, so we build straight into it. `base: '/'`
// because the server mounts at root and the SPA owns client routes (/search,
// /archive/:id).
export default defineConfig({
  plugins: [react(), devPanelsMeta()],
  base: '/',
  build: {
    outDir: '../src/thread_archive/_web/static',
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies the JSON API to a locally-running `archive watch --web`.
    proxy: {
      '/api': 'http://127.0.0.1:8787',
    },
  },
  test: {
    // Browser specs have their own Playwright runner. Keeping Vitest scoped to
    // component tests prevents either framework from collecting the other's
    // `test()` calls.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary'],
      include: ['src/**/*.{ts,tsx}'],
      // Test scaffolding and the Vite entry shim carry no behavior worth covering.
      exclude: ['src/test/**', 'src/main.tsx'],
      // Regression floors sit just under the measured suite. They fire only
      // under --coverage, which the CI row passes; a plain local `vitest run`
      // stays fast and ungated. Keep every dimension gated so deleting branch-
      // heavy interaction tests cannot hide behind unchanged line coverage.
      thresholds: {
        statements: 88,
        branches: 74,
        functions: 86,
        lines: 91,
      },
    },
  },
})
