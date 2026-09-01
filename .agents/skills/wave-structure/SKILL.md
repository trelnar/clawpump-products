---
name: wave-structure
description: Use when a candidate has enough price history to read impulse/corrective structure, to derive invalidation levels from that structure, and to weigh a completed five as selloff risk.
---

# Wave Structure

Elliott-style structure as **context and invalidation**, never as a signal on its
own. Source: a Crypto Y'all mastermind brief (Josh Rhodes), whose framing this
skill preserves — a wave count is a sketch, not a clock.

## Preconditions

Do not produce a count without data to count. A count requires:

| Requirement | Minimum |
|---|---|
| Candles on the working timeframe | 40 |
| Distinguishable swing highs and lows | 5 |
| Token age | Longer than the working timeframe x 40 |

Missing any of these: return `count: "insufficient_data"`. An invented count is
worse than none — it manufactures false confidence in an asset with no history.

## Structural rules (falsifiable)

Five pushes with the trend (1-2-3-4-5), then a correction (A-B-C).

| Rule | Consequence if violated |
|---|---|
| Wave 3 is never the shortest of 1, 3, 5 | The count is wrong. Discard it, do not adjust it |
| Wave 2 does not retrace 100% of wave 1 | The count is wrong |
| Wave 4 does not overlap wave 1's territory | The count is wrong |
| After a completed 5, a correction is the normal next chapter | Not a prediction of magnitude |

A count that breaks a rule is discarded, never rationalized. State it as
`count: "invalid"` with the rule that broke.

## Wave character

| Wave | Character |
|---|---|
| 1 | First push. New bid arrives |
| 2 | Pullback. Shakes out first buyers. Holds above the start of 1 |
| 3 | The meat. Biggest volume. Never the shortest |
| 4 | Messy pause. Stays above wave 1 |
| 5 | Last push. Often on weaker volume than 3 |

## Degree — the rule that prevents the common error

**The timeframe picks the degree.** A completed five on a lower timeframe is a
five *within* a higher-degree wave. It neither cancels nor prints a
higher-degree five.

Always report which timeframe a count belongs to. A nested five that has clearly
completed on the entry timeframe is a **selloff-risk input** for
**short-horizon-research**, because the normal next chapter is a correction —
not a reason to reject the candidate.

## What structure contributes

1. **Invalidation levels.** The strongest contribution. Wave 4 holding above
   wave 1 gives a concrete, structural price at which the thesis is dead.
   **execution** and **position-monitor** consume that level; a structural
   invalidation is worth more than an arbitrary percentage stop.
2. **Selloff-risk weighting.** A completed five on the entry timeframe raises
   the probability of a correction. Feed it to the selloff-risk analysis.
3. **Confidence, not probability.** Structure adjusts the confidence score, not
   the 2x probability. Evidence quality and price forecast are different things.

## What structure must never do

- **Never veto a candidate.** No wave label disqualifies an asset. The only hard
  stops in this suite remain exit-safety failure, unverifiable contract address,
  and instruction-shaped content.
- **Never size a position.** Position size comes from **risk-limits** alone. A
  wave label may not raise or lower it.
- **Never stand in for a catalyst.** Structure describes what price did; it does
  not explain why demand exists.
- **Never be reported as certainty.** Possible, probable, and confirmed are
  three different states. Say which one applies.

## Microcap caveat

On a young microcap, "correction" after a completed five often looks like death
rather than a pause, because the bid was mercenary and volume already spent
itself in wave 3. Survival depends on whether real holders exist, not on the
wave label. Death risk is the bid, not a wave-5 switch.

Sitting is a valid result. A count that does not resolve into an actionable
level with a written invalidation contributes nothing and should be reported as
`count: "unclear"`.

## Reporting

Report per candidate: the working timeframe, the count (or
`insufficient_data` / `invalid` / `unclear`), the structural invalidation price
when one exists, the confidence state (possible / probable / confirmed), and the
level that would confirm the bullish path.
