import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import { PUBLIC_ROUTES, ROUTES } from './routes'

const here = dirname(fileURLToPath(import.meta.url))
const appSource = readFileSync(resolve(here, '..', 'src', 'App.tsx'), 'utf8')

function routePattern(path: string): RegExp {
  const escaped = path.replace(/[.*+?()|^$\[\]{}\\]/g, '\\$&')
  return new RegExp(
    '^' + escaped.replace(/:[A-Za-z_][A-Za-z0-9_]*/g, '[^/?]+') + '(?:\\?.*)?$',
  )
}

const declared = [...appSource.matchAll(/<Route\s+path="([^"]+)"/g)].map((match) => match[1])

test('every application route has exactly one browser smoke case', () => {
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

test('every committed page route still exists', () => {
  // The coverage test above is a bijection between App and the smoke cases, so
  // deleting a route *and* its case leaves it green. This is the other half: the
  // URLs archive promises to outside callers cannot quietly disappear.
  const dropped = PUBLIC_ROUTES.filter((route) => !declared.includes(route))
  expect(dropped, `Committed routes missing from App: ${dropped.join(', ')}`).toEqual([])
})
