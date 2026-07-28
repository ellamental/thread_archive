// Categorical colors for the stats charts' *source* series, and the sequential ramp the
// rhythm heatmap grades by.
//
// Models already have a color system of their own (modelColor.ts — family hue, version
// shade) and the charts keep using it, so a model reads the same in a chart as it does
// in the table beside it. Sources had no colors at all; these are them.
//
// Three slots is a hard ceiling, not a starting point. The palette is held to *every*
// pair being separable — under protanopia, under deuteranopia, and under normal vision,
// in both color schemes, against the panel the chart sits on — rather than only the pairs
// that are neighbours in slot order. A stacked month chart usually earns the weaker
// neighbours-only bar, but not here: most months in an archive have only one or two live
// sources, so a band routinely sits directly against one several slots away, and which
// pairs touch isn't knowable in advance. Four hues cannot clear that bar; three plus a
// recessive gray can, and the tail folds into the gray rather than minting a fourth hue
// nobody can distinguish. Every source's own numbers are in the by-provider table below
// the chart, which is also the relief for the slots that fall under 3:1 on their panel.
//
// The actual hex lives in styles.css as `--cat-1…--cat-3` / `--cat-other` so both schemes
// swap in one place; this module names the slots and hands out the var references.

export const CAT_SLOTS = 3

/** The fill for series `i` of a categorical chart (0-based), in first-seen order.
 *  Anything past the palette — the folded tail — takes the recessive gray. */
export function catColor(i: number): string {
  return i < CAT_SLOTS ? `var(--cat-${i + 1})` : 'var(--cat-other)'
}

/** Where a value sits on the sequential ramp, as a step index for `--seq-N`.
 *  `frac` is 0…1 of the scale's max; 0 stays at the palest step so an empty cell
 *  recedes into the surface rather than reading as a low value. */
export const SEQ_STEPS = 6

export function seqColor(frac: number): string {
  if (!(frac > 0)) return 'var(--seq-0)'
  const step = Math.min(SEQ_STEPS, Math.max(1, Math.ceil(frac * SEQ_STEPS)))
  return `var(--seq-${step})`
}
