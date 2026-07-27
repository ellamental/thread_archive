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

test('browse reaches every thread through URL-backed pagination', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)
  const threads = Array.from({ length: 101 }, (_, i) => ({
    id: `${THREAD_ID.slice(0, -3)}${String(i + 1).padStart(3, '0')}`,
    title: `Paginated Thread ${i + 1}`,
    source: 'claude-code',
    first_user_message: null,
    thread_type: 'conversation',
    updated_at: '2026-07-20T12:00:00Z',
  }))
  await page.route('**/api/threads?*', async (route) => {
    const url = new URL(route.request().url())
    const current = Number(url.searchParams.get('page') ?? 1)
    const pageSize = Number(url.searchParams.get('limit') ?? 100)
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        threads: threads.slice((current - 1) * pageSize, current * pageSize),
        total: threads.length,
        page: current,
        page_size: pageSize,
        pages: Math.ceil(threads.length / pageSize),
      }),
    })
  })

  await page.goto('/threads')
  const topPager = page.getByRole('navigation', { name: 'thread pages top' })
  const browse = page.locator('main .wrap')
  await expect(topPager).toContainText('Page 1 of 2')
  await expect(browse.getByText('Paginated Thread 1', { exact: true })).toBeVisible()
  await expect(browse.getByText('Paginated Thread 101', { exact: true })).toHaveCount(0)

  await topPager.getByRole('button', { name: 'Next' }).click()
  await expect(page).toHaveURL(/\/threads\?page=2$/)
  await expect(browse.getByText('Paginated Thread 101', { exact: true })).toBeVisible()
  await expect(browse.getByText('Paginated Thread 1', { exact: true })).toHaveCount(0)

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

test('an export dragged onto the import page uploads and is tracked to imported', async ({
  page,
}) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)
  // Registered after mockApi, so it wins: the drop zone as the watcher works
  // through it, flipped between polls.
  const zone = {
    dumps_dir: '/tmp/browser-archive/dumps',
    waiting: [] as unknown[],
    imported: [] as unknown[],
    failed: [] as unknown[],
  }
  await page.route('**/api/drops', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(zone) })
  })

  await page.goto('/upload')
  await expect(page.getByText('/tmp/browser-archive/dumps')).toBeVisible()

  const dataTransfer = await page.evaluateHandle(() => {
    const transfer = new DataTransfer()
    transfer.items.add(new File(['PK'], 'grok-export.zip', { type: 'application/zip' }))
    return transfer
  })
  await page.locator('.dropzone').dispatchEvent('drop', { dataTransfer })

  await expect(page.locator('.upload-row')).toContainText('grok-export.zip')
  await expect(page.locator('.upload-row')).toContainText('waiting for the importer')

  // The watcher imports it and retains the download as the recovery copy; the
  // page finds that out by polling the folder.
  zone.imported = [
    { name: 'grok-export.zip', bytes: 4, at: '2026-07-20T12:00:00Z', kind: 'grok' },
  ]
  await expect(page.locator('.upload-row')).toContainText('imported', { timeout: 15_000 })

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})

test('a silenced warning stays one click from being read and restored', async ({ page }) => {
  const errors = monitorPage(page)
  const unhandled = await mockApi(page)
  // Registered after mockApi, so it wins: the silence store as the server keeps
  // it, moved between the two lists by the writes the page issues.
  const held = {
    key: 'same-disk',
    tone: 'warn',
    title: 'Backup is on the same filesystem as the archive',
    detail: 'Move the scheduled destination to another disk.',
    command: 'thread_archive daemon install --backup --dest /Volumes/disk',
    fingerprint: 'e2e',
    silenced_at: new Date().toISOString(),
  }
  const board = { active: [] as unknown[], silenced: [held] }
  await page.route(/\/api\/notices/, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/unsilence')) {
      board.active = [held]
      board.silenced = []
    }
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(board) })
  })

  await page.goto('/health')
  // Nothing is shouting, and the page still says what it is holding back.
  await expect(page.getByRole('heading', { name: 'Nothing needs attention' })).toBeVisible()
  const indicator = page.getByRole('button', { name: '1 silenced' })
  await expect(indicator).toBeVisible()

  await indicator.click()
  await expect(page.getByText('Backup is on the same filesystem as the archive')).toBeVisible()
  await page.getByRole('button', { name: 'Unsilence' }).click()

  await expect(page.getByRole('heading', { name: '1 warning' })).toBeVisible()
  await expect(page.getByRole('button', { name: '1 silenced' })).toHaveCount(0)

  expect(unhandled).toEqual([])
  expect(errors).toEqual([])
})
