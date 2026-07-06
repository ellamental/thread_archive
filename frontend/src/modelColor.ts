import type { CSSProperties } from 'react'

// A per-model accent color, so the header lists which models answered a thread and
// each assistant message is tinted to match the model that produced it.
//
// Only the *hue* is fixed here; the actual color (lightness/alpha) is resolved in
// styles.css against the `--mc-h` custom property, separately per color-scheme — a
// dark hue like blue is lightened on the near-black dark panel, a bright hue like
// amber darkened on the light panel, so every model reads in both modes.

// Hues spread around the wheel so adjacent picks stay distinct.
const HUES = [210, 265, 330, 12, 40, 150, 185, 95, 300, 235, 355, 118]

// Each model's preferred hue: a stable hash of its name, so a model tends to keep
// its color across threads (learnable at a glance). Within a single thread that
// isn't enough — two models can hash to the same slot — so `assignHues` resolves
// collisions; callers should key colors off that map, not this directly.
function baseIndex(model: string): number {
  // FNV-1a over the name → a palette slot.
  let h = 2166136261
  for (let i = 0; i < model.length; i++) {
    h ^= model.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) % HUES.length
}

// Assign a distinct hue to each model in a thread. Each starts from its hashed slot
// (cross-thread stability) and, on collision, walks to the next free slot — so the
// models in one thread stay visually distinct until there are more than the palette
// holds (then colors repeat, unavoidably). Order-dependent only on collision, so a
// thread's first-seen model order gives a deterministic, reload-stable mapping.
export function assignHues(models: string[]): Record<string, number> {
  const used = new Set<number>()
  const out: Record<string, number> = {}
  for (const m of models) {
    const base = baseIndex(m)
    let slot = base
    for (let k = 0; k < HUES.length; k++) {
      const cand = (base + k) % HUES.length
      if (!used.has(cand)) {
        slot = cand
        break
      }
    }
    used.add(slot)
    out[m] = HUES[slot]
  }
  return out
}

// A model's standalone hue, ignoring within-thread collisions — for callers without
// the thread's assignment to hand.
export function modelHue(model: string): number {
  return HUES[baseIndex(model)]
}

// Inline style carrying a hue as the `--mc-h` custom property for styles.css to turn
// into scheme-aware colors. (CSSProperties has no slot for custom props, so the cast
// is unavoidable.)
export function hueStyle(hue: number): CSSProperties {
  return { '--mc-h': hue } as unknown as CSSProperties
}
