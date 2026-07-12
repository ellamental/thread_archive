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
  },
})
