import { expect, test } from '@playwright/test'

import { MODEL, THREAD_ID, mockApi, monitorPage } from './helpers'
import { ROUTES } from './routes'

for (const route of ROUTES) {
  test(`${route.path} renders without browser errors`, async ({ page }) => {
    const errors = monitorPage(page)
    const unhandled = await mockApi(page)

    await page.goto(route.path)
    await expect(route.landmark(page)).toBeVisible()
    await expect(page.locator('.statusbar')).toContainText('1 threads · 3 events')

    expect(unhandled).toEqual([])
    expect(errors).toEqual([])
  })
}

test('search opens and highlights the matching archived message', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)

  await page.goto('/')
  await page.getByPlaceholder('search conversations…').fill('needle')
  await page.getByPlaceholder('search conversations…').press('Enter')

  await expect(page).toHaveURL(/\/search\?q=needle$/)
  await page.getByText('The needle lives here.').click()

  await expect(page).toHaveURL(new RegExp(`/archive/${THREAD_ID}\\?e=12$`))
  await expect(page.locator('.msg.hit-target')).toContainText('The needle lives here.')

  await page.getByLabel('thinking').check()
  await page.locator('details.thinking summary').click()
  await expect(page.getByText('Private browser-test reasoning.')).toBeVisible()

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})

test('stats drills into a model and back through real browser navigation', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)

  await page.goto('/stats')
  await page.getByRole('link', { name: MODEL }).click()
  await expect(page).toHaveURL(new RegExp(`/stats/model/${MODEL}$`))
  await expect(page.getByRole('heading', { name: MODEL })).toBeVisible()

  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Stats' })).toBeVisible()

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})
