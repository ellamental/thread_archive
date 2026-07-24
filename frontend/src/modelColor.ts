import type { CSSProperties } from 'react'

// A per-model accent color, so the header lists which models answered a thread and
// each assistant message is tinted to match the model that produced it.
//
// The color is semantic, not arbitrary: the hue comes from the model's *family* —
// every opus is a green, every gpt/codex a blue, fable a red — and its *version*
// picks a shade inside that family's band, so a thread's colors read as "two opuses
// and a gpt" at a glance. Models from families we don't know fall back to a hashed
// hue, which at least stays stable per name.
//
// Only the hue and a saturation offset are fixed here; the final lightness/alpha are
// resolved in styles.css against the `--mc-h` / `--mc-s` custom properties, separately
// per color-scheme — a dark hue like blue is lightened on the near-black dark panel, a
// bright hue like amber darkened on the light panel, so every model reads in both modes.

export interface ModelColor {
  /** hue in degrees */
  h: number
  /** saturation offset in percentage points, added to each rule's base in styles.css */
  s: number
}

// Family → hue, matched in order against the lowercased model name: the specific
// Claude lines win before the bare-`claude` catch-all at the end. Hues sit far enough
// apart that neighboring families stay distinct even at the edges of their shade bands.
const FAMILIES: Array<[RegExp, number]> = [
  [/grok/, 25], // orange
  [/haiku/, 48], // amber
  [/gemma/, 92], // yellow-green
  [/opus/, 145], // green
  [/gemini/, 175], // teal
  [/glm|z-ai/, 197], // cyan
  [/gpt|codex|chatgpt|davinci|\bo[134]\b/, 219], // blue
  [/deepseek/, 242], // indigo
  [/sonnet/, 272], // violet
  [/kimi|moonshot/, 298], // purple
  [/qwen/, 320], // magenta
  [/fable/, 356], // red
  [/claude/, 118], // any other Claude line
]

// Hues for models outside the table (local finetunes, one-off vendors), sitting in the
// gaps between the family hues above.
const OTHER_HUES = [70, 338, 257, 10, 160, 285, 105, 131, 208, 230, 309, 186]

// Shades per family: how many steps of hue/saturation a family's band is cut into
// before versions start reusing a shade (a span of 0.6 of a version — far more than
// any one thread mixes).
const SHADES = 6

// A shade step (0…SHADES-1) as offsets from the family's base color. Small hue swing so
// the family stays readable, wider saturation swing so adjacent versions still separate.
function shadeOffsets(step: number): { dh: number; ds: number } {
  const off = step - (SHADES - 1) / 2 // -2.5 … +2.5
  return { dh: off * 4, ds: off * 8 }
}

// FNV-1a over a name → an integer, for the unfamiliar-model fallback.
function hash(name: string): number {
  let h = 2166136261
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

// The version that follows the family token, as major*10 + minor: "claude-opus-4-6" →
// 46, "gpt-5.2" → 52, "claude-opus-5" → 50, an unversioned name → 0. Only the ordering
// matters (which shade a version lands on), so a trailing size or variant read as a
// minor ("gemma-4-26B") is harmless — it just gives that variant its own shade.
function versionRank(rest: string): number {
  const m = /(\d+)(?:[.\-_](\d+))?/.exec(rest)
  if (!m) return 0
  return (parseInt(m[1], 10) % 100) * 10 + (m[2] ? parseInt(m[2], 10) % 10 : 0)
}

// `bump` walks a model along its family's shade ladder — used only to break ties
// between two models that would otherwise land on the same color in one view.
function colorAt(model: string, bump: number): ModelColor {
  // Release-date suffixes (…-20251101) would read as a version, so drop them first.
  const name = model.toLowerCase().replace(/-?20\d{6}\b/g, '')
  for (const [re, hue] of FAMILIES) {
    const m = re.exec(name)
    if (!m) continue
    const step = (versionRank(name.slice(m.index + m[0].length)) + bump) % SHADES
    const { dh, ds } = shadeOffsets(step)
    return { h: (hue + dh + 360) % 360, s: ds }
  }
  const h = hash(name)
  const { dh, ds } = shadeOffsets(((h >>> 8) + bump) % SHADES)
  // Muted, so a hashed hue that lands near a family's band still can't pass for it.
  return { h: (OTHER_HUES[h % OTHER_HUES.length] + dh + 360) % 360, s: ds - 22 }
}

// A model's color on its own — for callers without a set of sibling models to hand.
export function modelColor(model: string): ModelColor {
  return colorAt(model, 0)
}

// Assign colors across a set of models shown together (a thread's models, a stats
// table's rows). Each takes its own family+version color; two that would land on the
// same shade walk their family's ladder until they separate — so same-family models
// stay the same hue while reading as distinct versions, and colors only repeat once a
// family has more members on screen than the ladder has shades. Order-dependent only
// on collision, so a first-seen order gives a deterministic, reload-stable mapping.
export function assignColors(models: string[]): Record<string, ModelColor> {
  const used = new Set<string>()
  const out: Record<string, ModelColor> = {}
  const key = (c: ModelColor) => `${Math.round(c.h)}:${Math.round(c.s)}`
  for (const m of models) {
    let c = colorAt(m, 0)
    for (let k = 1; k < SHADES && used.has(key(c)); k++) c = colorAt(m, k)
    used.add(key(c))
    out[m] = c
  }
  return out
}

// Inline style carrying a color as the `--mc-h` / `--mc-s` custom properties for
// styles.css to turn into scheme-aware colors. (CSSProperties has no slot for custom
// props, so the cast is unavoidable.)
export function colorStyle(c: ModelColor): CSSProperties {
  return { '--mc-h': c.h, '--mc-s': c.s } as unknown as CSSProperties
}
