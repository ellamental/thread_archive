import { expect, test } from '@playwright/test'

import { MODEL, THREAD_ID, mockApi, monitorPage } from './helpers'
import { ROUTES } from './routes'

for (const route of ROUTES) {
  test(`${route.path} renders without browser errors`, async ({ page }) => {
    const errors = monitorPage(page)
    const unhandled = await mockApi(page)

    await page.goto(route.path)
    await expect(route.landmark(page)).toBeVisible()
    await expect(page.locator('.appbar')).toBeVisible()

    expect(unhandled).toEqual([])
    expect(errors).toEqual([])
  })
}

test('search opens and highlights the matching archived message', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)

  await page.goto('/')
  const search = page.locator('[data-global-search][data-primary="true"]')
  await search.fill('needle')
  await search.press('Enter')

  await expect(page).toHaveURL(/\/search\?q=needle$/)
  await page.getByText('The needle lives here.').click()

  await expect(page).toHaveURL(new RegExp(`/archive/${THREAD_ID}\\?e=12&q=needle$`))
  await expect(page.locator('.msg.hit-target')).toContainText('The needle lives here.')

  await page.getByLabel('thinking').check()
  await page.locator('details.thinking summary').click()
  await expect(page.getByText('Private browser-test reasoning.')).toBeVisible()

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})

test('home exposes recent conversations and the global search shortcut', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)

  await page.goto('/')
  await expect(page.getByRole('link', { name: /Browser Test Thread/ }).last()).toBeVisible()
  await expect(page.getByText('Open the browser test thread and verify its recent-card preview.')).toBeVisible()
  await page.keyboard.press('/')
  await expect(page.locator('[data-global-search][data-primary="true"]')).toBeFocused()

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})

test('thread reader find and detail controls work in the browser', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)
  await page.goto(`/archive/${THREAD_ID}`)

  await page.getByRole('searchbox', { name: 'find in thread' }).fill('needle')
  await expect(page.getByText('1 of 2')).toBeVisible()
  await expect(page.locator('.msg.find-target')).toContainText('needle')

  const details = page.locator('details.tool')
  await expect(details).toHaveCount(2)
  await page.getByRole('button', { name: 'expand details' }).click()
  await expect(details.first()).toHaveAttribute('open', '')
  await page.getByRole('button', { name: 'collapse details' }).click()
  await expect(details.first()).not.toHaveAttribute('open', '')

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})

for (const viewport of [
  { name: 'phone', width: 390, height: 844, minMain: 380 },
  { name: 'tablet', width: 768, height: 900, minMain: 500 },
  { name: 'desktop', width: 1200, height: 800, minMain: 850 },
]) {
  test(`thread layout remains usable at ${viewport.name} width`, async ({ page }) => {
    const errors = monitorPage(page)
    const unhandled = await mockApi(page)
    await page.setViewportSize(viewport)
    await page.goto(`/archive/${THREAD_ID}`)
    await expect(page.getByRole('heading', { name: 'Browser Test Thread' })).toBeVisible()

    const mainBox = await page.locator('main').boundingBox()
    expect(mainBox?.width ?? 0).toBeGreaterThanOrEqual(viewport.minMain)

    const menu = page.getByRole('button', { name: 'open navigation' })
    if (viewport.width < 760) {
      await expect(menu).toBeVisible()
      const closedBox = await page.locator('#archive-navigation').boundingBox()
      expect((closedBox?.x ?? 0) + (closedBox?.width ?? 0)).toBeLessThanOrEqual(1)
      await menu.click()
      await expect(page.locator('#archive-navigation')).toHaveClass(/open/)
      await expect(page.getByRole('searchbox', { name: 'search conversations' })).toBeVisible()
      await page.getByRole('button', { name: 'close navigation' }).first().click()
      await expect(page.locator('#archive-navigation')).not.toHaveClass(/open/)
    } else {
      await expect(menu).toBeHidden()
    }

    expect(unhandled).toEqual([])
    expect(errors).toEqual([])
  })
}

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
