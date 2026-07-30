import { expect, test } from '@playwright/test'

import { RUN_ID, mockApi, monitorPage } from './helpers'

test('every panel renders with no console errors and no unmocked calls', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)

  for (const [path, heading] of [
    ['/', 'Overview'],
    ['/retrieval', 'Retrieval'],
    ['/telemetry', 'Telemetry'],
    ['/lab', 'Search lab'],
  ] as const) {
    await page.goto(path)
    await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible()
  }

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})

test('the lab drills into a recorded run and back through real browser navigation', async ({
  page,
}) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)

  await page.goto('/lab')
  // The ledger shows the superseded pass and the failure the table above it
  // cannot — that is the whole reason the section exists.
  const runs = page.locator('.lab-runs-table')
  await expect(runs.getByText('superseded')).toBeVisible()
  await expect(runs.getByText('failed')).toBeVisible()

  await runs.getByRole('link').first().click()
  await expect(page).toHaveURL(new RegExp(`/lab/run/${RUN_ID}$`))
  // What the summary had no room for: the metrics the row does not lead with,
  // the movement since the last different configuration, and the invocation.
  await expect(page.getByRole('heading', { name: 'What it measured' })).toBeVisible()
  await expect(page.getByText('recall100')).toBeVisible()
  await expect(page.getByText('+0.028')).toBeVisible()
  await expect(page.locator('.lab-argv')).toContainText('--dataset scifact --vectors')
  // And what it cost, at the depth the ledger now keeps: the tail, the
  // throughput, and which stage the median query actually went into.
  const perf = page.getByRole('heading', { name: 'Performance' }).locator('..')
  const costs = perf.locator('.stat-tiles')
  await expect(costs.getByText('377 ms')).toBeVisible()
  await expect(costs.getByText('5.45/s')).toBeVisible()
  await expect(costs.getByText('was 210 ms')).toBeVisible()
  // The stage the median query actually went into — invisible before the ledger
  // kept a breakdown, and the first thing a ranking change moves.
  await expect(perf.getByRole('cell', { name: 'rank_ms' })).toBeVisible()

  // And which queries it actually failed — the two failure modes separated,
  // since a gold document ranked 40th and one never retrieved both score 0.0.
  const perQuery = page.getByRole('heading', { name: 'Per query' }).locator('..')
  await expect(perQuery.getByText('not found')).toBeVisible()
  await expect(perQuery.getByRole('cell', { name: '40' })).toBeVisible()
  await expect(perQuery.getByText(/never retrieved their gold document/)).toBeVisible()

  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Search lab' })).toBeVisible()

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})
