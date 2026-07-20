/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The viewer is served by the watcher's cohosted stdlib server (`archive watch
// --web`) from the package's static dir, so we build straight into it. `base: '/'`
// because the server mounts at root and the SPA owns client routes (/search,
// /archive/:id).
export default defineConfig({
  plugins: [react()],
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
