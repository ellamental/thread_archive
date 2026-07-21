import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import { ROUTES } from './routes'

const here = dirname(fileURLToPath(import.meta.url))
const appSource = readFileSync(resolve(here, '..', 'src', 'App.tsx'), 'utf8')

function routePattern(path: string): RegExp {
  const escaped = path.replace(/[.+?()|^$\[\]{}]/g, '\\$&')
  return new RegExp(
    '^' + escaped.replace(/:[A-Za-z_][A-Za-z0-9_]*/g, '[^/?]+') + '(?:\\?.*)?$',
  )
}

test('every application route has exactly one browser smoke case', () => {
  const declared = [...appSource.matchAll(/<Route\s+path="([^"]+)"/g)].map((match) => match[1])

  const missing = declared.filter(
    (route) => !ROUTES.some((smoke) => routePattern(route).test(smoke.path)),
  )
  const orphaned = ROUTES.filter(
    (smoke) => !declared.some((route) => routePattern(route).test(smoke.path)),
  ).map((smoke) => smoke.path)

  expect(missing, `Routes without browser coverage: ${missing.join(', ')}`).toEqual([])
  expect(orphaned, `Browser cases without an application route: ${orphaned.join(', ')}`).toEqual([])
  expect(new Set(ROUTES.map((route) => route.path)).size).toBe(ROUTES.length)
})
