// The dev-pages switch: a URL turns it on or off, storage remembers the answer.
import { afterEach, expect, it } from 'vitest'
import { devMode } from '../dev'

afterEach(() => window.localStorage.clear())

it('is off until something asks for it', () => {
  expect(devMode('')).toBe(false)
})

it('is turned on by the URL and then remembered without one', () => {
  expect(devMode('?dev=1')).toBe(true)
  // The point of persisting: navigating off the `?dev=1` address keeps it on,
  // so the flag is a switch rather than a URL to keep retyping.
  expect(devMode('')).toBe(true)
})

it('is turned back off by the URL, and that is remembered too', () => {
  devMode('?dev=1')
  expect(devMode('?dev=0')).toBe(false)
  expect(devMode('')).toBe(false)
})

it('survives storage it cannot use', () => {
  const original = window.localStorage.getItem
  Object.defineProperty(window.localStorage, 'getItem', {
    configurable: true,
    value: () => {
      throw new Error('storage disabled')
    },
  })
  try {
    // Unreadable storage means "off", never a thrown error out of the sidebar.
    expect(devMode('')).toBe(false)
  } finally {
    Object.defineProperty(window.localStorage, 'getItem', { configurable: true, value: original })
  }
})
