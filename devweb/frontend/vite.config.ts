/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built into ../static, which `python -m devweb` serves. No dev-panel stamp
// plugin here and none coming: every page in this app is a dev panel, so there
// is nothing to gate. `base: '/'` because the server mounts at root and the SPA
// owns its client routes (/retrieval, /telemetry, /lab, /lab/run/:id).
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies the JSON API to a locally-running `python -m devweb`.
    proxy: {
      '/api': 'http://127.0.0.1:8789',
    },
  },
  test: {
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/test/**', 'src/main.tsx'],
    },
  },
})
