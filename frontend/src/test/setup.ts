// Vitest setup (wired via vite.config.ts `test.setupFiles`): registers the
// jest-dom matchers (and their TS augmentations — this file is inside tsconfig's
// include, so `tsc --noEmit` sees the matcher types too), and unmounts rendered
// trees between tests (RTL only auto-registers its cleanup when the runner
// exposes globals, which we keep off).
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => cleanup())
