// The dev-panel switch: the server stamps the served shell, the viewer reads it.
import { afterEach, expect, it } from 'vitest'
import { devPanels } from '../dev'

function stamp(content: string): void {
  const meta = document.createElement('meta')
  meta.setAttribute('name', 'thread-archive-dev-panels')
  meta.setAttribute('content', content)
  document.head.appendChild(meta)
}

afterEach(() => {
  document.head.querySelectorAll('meta[name="thread-archive-dev-panels"]').forEach((m) => m.remove())
})

it('is off in an unstamped shell — what a shipped viewer serves', () => {
  expect(devPanels()).toBe(false)
})

it('is on once the shell carries the stamp', () => {
  stamp('1')
  expect(devPanels()).toBe(true)
})

it('reads a negative stamp as off', () => {
  // Nothing writes these — the server stamps only to turn the panels on. But a
  // marker whose content says "off" while its presence says "on" must not be
  // resolved in favor of showing the panels.
  stamp('0')
  expect(devPanels()).toBe(false)
  document.head.querySelector('meta[name="thread-archive-dev-panels"]')?.remove()
  stamp('false')
  expect(devPanels()).toBe(false)
})

it('reads a contentless stamp as off', () => {
  const meta = document.createElement('meta')
  meta.setAttribute('name', 'thread-archive-dev-panels')
  document.head.appendChild(meta)
  expect(devPanels()).toBe(false)
})
