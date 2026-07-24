import { describe, expect, it } from 'vitest'
import { assignColors, colorStyle, modelColor, type ModelColor } from '../modelColor'

// The archive spans a dozen model families across a decade of naming conventions, so
// what's asserted here is the mapping's *meaning*: family → hue band, version → shade
// inside it, and no two models shown together wearing the same color.

// Hue distance on the wheel (a family band never wraps far, but red does sit at ~356).
const arc = (a: number, b: number): number => {
  const d = Math.abs(a - b) % 360
  return d > 180 ? 360 - d : d
}

const BANDS: Array<[string, number, string[]]> = [
  ['opus (green)', 145, ['claude-opus-4-5-20251101', 'claude-opus-4-6', 'claude-opus-4-8', 'claude-opus-5', 'opus']],
  ['fable (red)', 356, ['claude-fable-5']],
  ['sonnet (violet)', 272, ['claude-sonnet-4-6', 'Claude Sonnet 4.6 (Thinking)', 'claude-sonnet-5']],
  ['haiku (amber)', 48, ['claude-haiku-4-5-20251001']],
  ['gpt/codex (blue)', 219, ['gpt-5.6-sol', 'gpt-5-2-thinking', 'gpt-4o', 'text-davinci-002-render-sha', 'GPT-OSS 120B (Medium)']],
  ['grok (orange)', 25, ['grok-4.5', 'x-ai/grok-build-0.1', 'grok-composer-2.5-fast']],
  ['gemini (teal)', 175, ['gemini', 'Gemini 3.5 Flash (Medium)']],
  ['deepseek (indigo)', 242, ['deepseek/deepseek-v4-pro', 'openrouter/deepseek/deepseek-v4-flash']],
  ['kimi (purple)', 298, ['moonshotai/kimi-k2.7-code', 'openrouter/moonshotai/kimi-k3']],
  ['qwen (magenta)', 320, ['local/Qwen_Qwen3.5-9B-Q4_K_M']],
  ['glm (cyan)', 197, ['z-ai/glm-5.2', 'local/GLM-4.7-Flash-REAP-23B-A3B-Q4_K_M']],
  ['gemma (yellow-green)', 92, ['local/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL']],
]

describe('model families', () => {
  for (const [label, hue, models] of BANDS) {
    it(`puts every ${label} model in its family's hue band`, () => {
      for (const m of models) expect(arc(modelColor(m).h, hue), m).toBeLessThanOrEqual(12)
    })
  }

  it('keeps families apart — no two family hues share a band', () => {
    const hues = BANDS.map(([, h]) => h)
    for (let i = 0; i < hues.length; i++)
      for (let j = i + 1; j < hues.length; j++) expect(arc(hues[i], hues[j])).toBeGreaterThan(20)
  })

  it('gives an unfamiliar model a stable color of its own', () => {
    const locals = [
      'local/Cydonia-24B-v4zg-Q4_K_M',
      'local/Olmo-3.1-32B-Instruct-UD-Q4_XL',
      'openrouter/microsoft/wizardlm-2-8x22b',
      'cursor',
    ]
    for (const m of locals) expect(modelColor(m), m).toEqual(modelColor(m))
    expect(new Set(locals.map((m) => `${modelColor(m).h}:${modelColor(m).s}`)).size).toBe(locals.length)
  })
})

describe('versions', () => {
  const key = (c: ModelColor): string => `${c.h}:${c.s}`

  it('shades the opus line so each version reads distinctly', () => {
    const versions = ['claude-opus-4-5', 'claude-opus-4-6', 'claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5']
    expect(new Set(versions.map((m) => key(modelColor(m)))).size).toBe(versions.length)
  })

  it('ignores a release-date suffix — same model, same color', () => {
    expect(modelColor('claude-opus-4-5-20251101')).toEqual(modelColor('claude-opus-4-5'))
  })

  it('reads a version however the provider punctuates it', () => {
    expect(modelColor('gpt-5-2')).toEqual(modelColor('gpt-5.2'))
    expect(modelColor('claude-opus-4-6')).toEqual(modelColor('claude-opus-4.6'))
  })
})

describe('assignColors', () => {
  it('gives every model in a thread its own color', () => {
    const models = ['claude-opus-4-6', 'claude-opus-5', 'claude-haiku-4-5-20251001', 'gpt-5.2', 'claude-fable-5']
    const c = assignColors(models)
    expect(new Set(models.map((m) => `${c[m].h}:${c[m].s}`)).size).toBe(models.length)
  })

  it('separates colliding models by shade, not by family', () => {
    // Same family, same shade step (rank 45 vs 105 — a full ladder apart) — the second
    // walks the ladder rather than leaving the family's hue band.
    const c = assignColors(['claude-opus-4-5', 'claude-opus-10-5'])
    expect(c['claude-opus-4-5']).not.toEqual(c['claude-opus-10-5'])
    for (const m of Object.keys(c)) expect(arc(c[m].h, 145)).toBeLessThanOrEqual(12)
  })

  it('leaves a model alone when nothing collides with it', () => {
    expect(assignColors(['claude-opus-5', 'gpt-5.2'])['claude-opus-5']).toEqual(modelColor('claude-opus-5'))
  })

  it('carries the color to CSS as custom properties', () => {
    expect(colorStyle({ h: 145, s: -10 })).toEqual({ '--mc-h': 145, '--mc-s': -10 })
  })
})
