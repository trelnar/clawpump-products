# BTC 4h backtest harness — does the three-gate filter add expectancy or destroy it?

This measures the Two-Pole Oscillator + Volumatic VIDYA swing system before any more of it gets automated. It exists because six months of that system produced six evaluated signals, one taken trade, and zero measurements.

**It is a measurement tool. It does not trade, hold keys, or place orders.**

## The question

The three gates are: a dot in the ±0.5 quadrant, a matching VIDYA ribbon, and Delta Volume past ±20%.

The VIDYA ribbon flips **after** a turn. So the ribbon and delta gates structurally reject turn signals and admit only pullback-in-trend signals. The four alerts the live signal-bot produced hint at what that costs:

| Alert | Gates | Hindsight |
|---|---|---|
| Aug 13 LONG @ −1.087 | **rejected** (ribbon pink) | preceded 62k → 80k, +25% |
| Aug 22 SHORT @ +1.690 | **rejected** (ribbon green, ΔV +132%) | marked a local top |
| Aug 30 LONG @ −0.975 | passed | stopped at a sweep low, then +6% |
| Aug 7 SHORT @ +0.694 | passed | scratch |

Two of four hindsight-good signals were the two the gates threw away. n=4 proves nothing — which is the point of building this.

## Read this before you trust a single number

The oscillator and VIDYA here are **ports**. They reproduce parameters confirmed against your chart, but they have only ever run on synthetic data. Until the `--verify` CSV has been diffed against TradingView, every expectancy figure may be measuring a bug rather than a market.

The harness enforces this itself: `verify` fails loudly and tells you to stop when the four signal-log bars don't reproduce within ±0.02.

Full checklist: **SPEC.md §4**. The two that gate everything:

- **U-0 (P0)** — the four signal-log bars and the +0.135 anchor dot on 2026-07-31 20:00 must all match within ±0.02. If any fails, stop.
- **U-1 (P0)** — ΔV at the signal bars must read ≈ +132% (Aug 22), ≈ +53.88% (Aug 30), negative (Aug 13). If they match, the threshold sweep tells you the real gate level. If they don't, the accumulation semantics are wrong — not the threshold.

Then **U-3 (P1, high leverage)**: whether the VIDYA trend flips on a band break (default) or a line cross. That choice shifts every `leg_id`, and therefore every `delta_pct`.

## Setup — on the VPS, not in a Claude sandbox

Coinbase is reachable from your Vultr box; it is blocked by egress policy inside Claude Code sessions, which is why nothing here was ever run against real data.

```bash
git clone -b claude/hype-token-signals-4b6nld <repo> ~/backtest-work
cd ~/backtest-work
python3 -m venv .venv && source .venv/bin/activate
pip install -r backtest/requirements.txt
python3 -m pytest backtest/tests/ -q          # 36 tests, all must pass
```

Run it as `sigbot` or your own user — it needs no privileges and touches nothing the trading bot owns.

## Use

**1 — pull history** (~3 years of 1h candles, aggregated to UTC-aligned 4h, cached on disk):

```bash
python3 -m backtest.verify fetch --start 2023-01-01 --end 2026-09-05 --progress
```

**2 — verify the port against your chart. This is the step that matters.**

```bash
python3 -m backtest.verify verify --source cache --out verify_btc_4h.csv --check-lookahead
```

Open the CSV beside TradingView and walk the U-0 / U-1 checks. The command prints a pass/fail table for the four signal bars, the chart anchors, and the delta readings, and refuses to bless the data if any fail. `--check-lookahead` separately confirms no value at bar *i* depends on bars after *i*.

**3 — run the ablation:**

```bash
python3 -m backtest.verify ablate --source cache --out report.md --progress
```

**4 — read the report.** It leads with a provisional banner, compares arms at your live parameters (not at each arm's best cell), suppresses any cell under 10 trades, and states plainly when the sample can't support a conclusion.

Single-configuration run, if you want one cell in detail:

```bash
python3 -m backtest.verify backtest --source cache --exit-id beyond0.5_R2
```

## The arms

`A0` all three gates (your live rules) · `A1` quadrant only · `A2` quadrant+ribbon · `A3` quadrant+delta · `A4` dot only · `A5` **inverted ribbon** — takes the trades the gates reject.

A5 is the arm that matters. If it beats A0, the gates are removing your best trades.

Swept alongside: ΔV threshold {0, 10, 20, 30, 50, 80}, quadrant {0, 0.25, 0.5, 0.75}, entry offset {1, 2}, and a 15-cell exit grid — fixed-%, ATR-from-entry, **ATR beyond the signal bar's extreme** (the fix for getting stopped at a sweep low), R-multiple targets, ATR trail, ribbon-flip exit, and time stops at 21/42/84 bars. One cell, `live_bracket`, replicates your actual GMX trade (−1.6% stop, +3.1% target).

## What this cannot tell you

- **Whether the edge is real, if the gates stay this tight.** A dry run on 4,000 synthetic bars produced **1 trade** in 600 days on the all-gates arm. On real data you may find the same thing, and the honest verdict is "unmeasurable," not "unprofitable."
- **Anything about alts.** BTC-only. The confluence filter is untested (U-10).
- **Anything about live execution.** No fill quality, no funding, no venue outage.
- **Anything at all before U-0 passes.**

The grid evaluates ~3,360 cells. At α=0.05 roughly 17 will look significant by luck alone — the report prints that number next to the results rather than letting you forget it. Treat the best cell as a hypothesis, never as a setting to go trade.

## Delta Volume — a correction worth knowing

Δ% = 2 × (buy − sell) / (buy + sell) × 100 ranges over **[−200, +200]**, not [−100, +100] — which is why your chart has shown +179.71%.

So **+20% means buy volume is 55% of the leg**, not 60%. Your gate is far weaker than it reads. That's U-1, and the sweep is there to find where the real threshold sits.

## Layout

| File | Role |
|---|---|
| `SPEC.md` | Authoritative math, interface contract, uncertainty checklist |
| `data.py` | Coinbase fetch, 1h→4h aggregation, contiguity trim, cache, synthetic generator, fixtures |
| `two_pole.py` | Oscillator + zero-line-gated dot detection |
| `vidya.py` | VIDYA, ATR bands, trend flips, cumulative per-leg Delta Volume |
| `strategy.py` | Gates, staleness, entry timing, exit models, bar-level simulation |
| `ablation.py` | Grid runner, metrics, statistical guards, report rendering |
| `verify.py` | CLI + the `--verify` CSV and lookahead check |
| `tests/` | 36 tests, including the truncated-vs-full lookahead test |

## Provenance

Modules were drafted by parallel agents that could not execute anything, then extracted, run, corrected, and tested here. Everything reported above was actually executed: 36 tests pass, the lookahead check passes, and the full 3,360-cell grid renders in ~71 seconds on synthetic data.

No part of it has touched real market data. That's your next step, and step 2 is the one that decides whether any of it means anything.
