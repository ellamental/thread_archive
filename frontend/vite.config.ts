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
      // Floor, not target: sits just under the measured 75.58% lines, so a
      // regression reds the suite without making the number a thing to chase.
      // Only fires under --coverage, which only the CI row passes — a plain
      // local `npx vitest run` stays fast and ungated. Lines only, the same one
      // ratchet lab/web holds; the shell components (Sidebar, StatusBar,
      // Landing, App) are the untested surface the number is waiting on.
      thresholds: {
        lines: 75,
      },
    },
  },
})
