import type { Locator, Page } from '@playwright/test'

import { MODEL, THREAD_ID } from './helpers'

export interface SmokeRoute {
  path: string
  landmark: (page: Page) => Locator
}

/** One concrete browser case for every route declared by App. */
export const ROUTES: SmokeRoute[] = [
  { path: '/', landmark: (page) => page.getByText(/Search above/) },
  {
    path: '/search?q=needle',
    landmark: (page) => page.getByText('results for “needle”', { exact: false }),
  },
  { path: '/threads', landmark: (page) => page.getByPlaceholder('filter by title…') },
  { path: '/stats', landmark: (page) => page.getByRole('heading', { name: 'Stats' }) },
  { path: '/experiments', landmark: (page) => page.getByRole('heading', { name: 'Experiments' }) },
  { path: '/experiments/patterns', landmark: (page) => page.getByRole('heading', { name: 'Patterns' }) },
  {
    path: '/experiments/patterns/browser-pattern',
    landmark: (page) => page.getByRole('heading', { name: 'Pattern matches' }),
  },
  { path: '/patterns', landmark: (page) => page.getByRole('heading', { name: 'Patterns' }) },
  {
    path: `/stats/model/${MODEL}`,
    landmark: (page) => page.getByRole('heading', { name: MODEL }),
  },
  {
    path: `/archive/${THREAD_ID}`,
    landmark: (page) => page.getByRole('heading', { name: 'Browser Test Thread' }),
  },
]
