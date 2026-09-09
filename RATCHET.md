# The ratchet — context-aware profit exit

**Status: SHADOW.** It computes everything and journals what it would have sold; it
places no orders. `RATCHET` in Telegram reports whether it is beating what actually
happened. It goes live only by setting `RATCHET_MODE=live` after the report passes.

## Why

The bot exited only on a stop, a standing plan leg, or the model's SELL_NOW. A token
that went +20% and faded gave it all back; the first live night saw −40% stops on
tokens that had been up first. The operator's ask: sell on a +20%-in-an-hour move when
the asset's own behaviour says the move is done, keep the 1–3 day window as the maximum.

Designed by a review panel (three designs, two judges, synthesis, three refutations) on
2026-09-09; the refutations changed it materially (dwell to arm, capped share, breakeven
stop on the rest, ADD refused once armed, noise-aware lock).

## The rule

1. **Arm** when three consecutive 1-minute closes sit at or above entry × 1.20. A single
   sniper print can't do it; the move has to hold.
2. **On arm:** the stop rises to breakeven (entry × 1.03). 75% of the position is the
   ratchet's share; 25% rides under the breakeven stop, the standing plan and the model.
   ADDs are refused for the rest of the trade (live mode).
3. **Floor** on the ratchet share, monotone: the highest of breakeven, `peak × (1 − gb)`,
   and `min(entry + ½ peak gain, peak × (1 − 1.5 σ15))`. `gb = clamp(k · σ15, 4%, 35%)`,
   `k = clamp(2 / (speed × stale), 1, 3)`: tighter the faster the run and the longer since
   the peak. σ15 is an EWMA of |1-minute log return| × √15, updated only on bars where the
   price moved.
4. **Sells of the share**, in order each tick: **TAKE** (blow-off: +30% over 15 min with the
   last 5 min turning down; or arming inside 2h with sellers outnumbering buyers),
   **FLOOR** (two fresh prints below the floor), **STALL** (no new peak for
   `clamp(10 × hours held, 20, 120)` minutes, sellers > buyers, and no HOLD from the model
   in the last 35 min with p2x ≥ 0.25 on rising signal).
5. **Win or loss** is the whole position's realised P&L at full close. A loss takes the
   existing path (approval withdrawn, 6h no re-entry). A ratchet winner gets a 2h pause
   before re-buying.

Parameters: `RATCHET_*` in `bot/tradebot/config.py`. Implementation:
`bot/tradebot/ratchet.py`, called from `monitor.check_positions` once per tick after
the stop, liquidity and plan checks. Tests: `bot/tests/test_ratchet.py` (the four
worked examples: the operator's case, whipsaw, sniper sandwich, spike-and-dump).

## New data

- `price_bars`: 1-minute close, 5-minute buy/sell counts and pool liquidity per held
  asset, written from the monitor tick, 7-day retention. This replaces nothing; nothing
  persisted prices before.
- `ratchet_track`: one row per shadow would-sell with the price it would have sold at and
  the 72h peak afterwards, sampled by the calibration pass.
- Journal events: `ratchet_armed`, `ratchet_would_sell` (shadow), `ratchet_exit` (live),
  `ratchet_error`.

## Going live: the gate

`RATCHET` reports, on resolved shadow sells:

- **Gate 1 (net):** mean of `0.75 × shadow multiple + 0.25 × actual multiple` must beat the
  mean actual multiple by 0.05.
- **Gate 2 (tail):** no more than 10% of would-sells happened below 1.15x on a token that
  then reached 2x within 72h.

Both pass on ~30 armed positions → `RATCHET_MODE=live` in `secrets.env`, run
`split-credentials.sh`, restart the core. Gate 1 fails → do not enable. Gate 2 fails →
the lock or STALL is too tight.

## Operator's choices (2026-09-09)

$10 orders in phase 1. Ratchet share 75%. Defaults for the rest: 3-close arm, breakeven
stop on the runner, tokens and Coinbase both (Coinbase has no flow, so STALL cannot fire
there), 2h re-buy pause, shadow until 30 arms.
