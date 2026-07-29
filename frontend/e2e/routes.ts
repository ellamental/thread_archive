import type { Locator, Page } from '@playwright/test'

import { MODEL, RUN_ID, THREAD_ID } from './helpers'

export interface SmokeRoute {
  path: string
  landmark: (page: Page) => Locator
}

/**
 * The page routes archive commits to (docs/web-viewer.md). Editor buttons,
 * sibling consoles' navbars and bookmarks link these from outside this repo, so
 * a rename strands a URL living in someone else's source. Adding a route is
 * free; dropping one of these is a deliberate act that edits this list.
 */
export const PUBLIC_ROUTES: readonly string[] = [
  '/',
  '/search',
  '/threads',
  '/stats',
  '/stats/model/:model',
  '/health',
  '/upload',
  '/archive/:id',
]

/** One concrete browser case for every route declared by App. */
export const ROUTES: SmokeRoute[] = [
  {
    path: '/',
    landmark: (page) => page.getByRole('heading', { name: 'Find the conversation you remember.' }),
  },
  {
    path: '/search?q=needle',
    landmark: (page) => page.getByText('results for “needle”', { exact: false }),
  },
  { path: '/threads', landmark: (page) => page.getByPlaceholder('filter by title…') },
  { path: '/stats', landmark: (page) => page.getByRole('heading', { name: 'Stats' }) },
  {
    path: '/health',
    landmark: (page) => page.getByRole('heading', { name: 'Your archive is protected' }),
  },
  {
    // A dev panel: it exists only for a viewer whose operator asked for the
    // panels, which is what this suite's build stamps into the shell (see
    // playwright.config.ts). Off, the route does not resolve at all — that case
    // is App's, in the vitest suite, since there is nothing here to navigate to.
    path: '/retrieval',
    landmark: (page) => page.getByRole('heading', { name: 'Retrieval', level: 1 }),
  },
  {
    // Operational histories that are useful only while developing the archive —
    // same dev-panel gate, same shell stamp.
    path: '/telemetry',
    landmark: (page) => page.getByRole('heading', { name: 'Telemetry', level: 1 }),
  },
  {
    // The benchmark inventory — the third dev panel, under the same gate.
    path: '/lab',
    landmark: (page) => page.getByRole('heading', { name: 'Search lab', level: 1 }),
  },
  {
    // One recorded run off the lab's ledger — reached by clicking a row there,
    // and addressable on its own so a configuration worth arguing about can be
    // linked to rather than described.
    path: `/lab/run/${RUN_ID}`,
    landmark: (page) => page.getByRole('heading', { name: 'What it measured' }),
  },
  {
    path: '/upload',
    landmark: (page) => page.getByRole('heading', { name: 'Import an account export' }),
  },
  {
    path: `/stats/model/${MODEL}`,
    landmark: (page) => page.getByRole('heading', { name: MODEL }),
  },
  {
    path: `/archive/${THREAD_ID}`,
    landmark: (page) => page.getByRole('heading', { name: 'Browser Test Thread' }),
  },
]
