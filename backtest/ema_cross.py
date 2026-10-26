"""Triple-EMA trend system (34/55/200 + ATR stop) through the harness.

The system as stated in the @quanttactic video (2026-09): the 200 EMA sets the
direction, the 34 EMA crossing the 55 EMA enters and exits, an ATR stop is the
backstop, long and short. His 4h result on 8 Binance pairs 2020-2026: 959
trades, 30% win rate, profit factor 1.52, drawdown 23.9%, +3714% summed.

What his video did not do, and this module does:
  * a RANDOM-ENTRY control with the same trade count, long/short ratio, exit
    rule and stop, so the only difference is whether the entry timing carried
    information;
  * a bootstrap confidence interval on the per-trade expectancy;
  * fees and slippage on every side (StrategyParams defaults, 15 bps per side);
  * a long-only variant, because his long trades carried 3597 of his 3714
    points.

Entries fill at the CLOSE of the cross bar. Exits: the opposite cross (via the
simulator's ribbon-flip path, fed a trend series of sign(ema34 - ema55)), or
the ATR stop, whichever first. No target, no time stop.

Usage:
    python3 -m backtest.ema_cross --product BTC-USD --source cache --start 2023-01-01 --end 2026-09-20
    python3 -m backtest.ema_cross ... --resample 1d      # 6 x 4h -> daily bars
"""

from __future__ import annotations

import argparse
import sys
import zlib
from typing import Any

import numpy as np
import pandas as pd

from backtest import ablation, data, strategy

EMA_FAST, EMA_SLOW, EMA_TREND = 34, 55, 200
ATR_LENGTH = 14
STOP_MULTS: tuple[float, ...] = (1.5, 2.0, 3.0)
WARMUP = 400  # EMA200 needs ~3x its length to converge; 400 bars is generous


def ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False).mean()


def build(bars: pd.DataFrame, *, long_only: bool = False) -> tuple[list[dict], pd.DataFrame]:
    """Return (entries, trend_frame). trend = +1 when ema34 > ema55 else -1."""
    close = bars["close"].astype("float64")
    f, s, t = ema(close, EMA_FAST), ema(close, EMA_SLOW), ema(close, EMA_TREND)
    above = f > s
    cross_up = above & ~above.shift(1, fill_value=False)
    cross_dn = ~above & above.shift(1, fill_value=True)
    long_sig = cross_up & (close > t)
    short_sig = cross_dn & (close < t)
    if long_only:
        short_sig = short_sig & False
    trend = pd.DataFrame({"trend": np.where(above, 1, -1).astype("int8")}, index=bars.index)

    idx = bars.index
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    entries: list[dict] = []
    for i in range(WARMUP, len(bars)):
        side = "long" if long_sig.iloc[i] else ("short" if short_sig.iloc[i] else None)
        if side is None:
            continue
        entries.append({
            "signal_ts": idx[i], "entry_ts": idx[i], "side": side,
            "signal_osc": float("nan"), "signal_high": float(high[i]),
            "signal_low": float(low[i]), "signal_delta_pct": float("nan"),
            "signal_trend": int(trend["trend"].iloc[i]),
        })
    return entries, trend


def random_entries(bars: pd.DataFrame, trend: pd.DataFrame, *, n: int, n_long: int, token: str) -> list[dict]:
    """Same count and long/short mix at uniformly random post-warmup bars."""
    if n <= 0:
        return []
    pool = np.arange(WARMUP, len(bars) - 1)
    take = min(n, pool.size)
    rng = np.random.default_rng([ablation.RANDOM_BASELINE_SEED, zlib.crc32(token.encode())])
    picks = np.sort(rng.choice(pool, size=take, replace=False))
    sides = np.array(["long"] * min(n_long, take) + ["short"] * (take - min(n_long, take)))
    rng.shuffle(sides)
    idx = bars.index
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    tr = trend["trend"].to_numpy()
    return [{
        "signal_ts": idx[i], "entry_ts": idx[i], "side": str(sides[k]),
        "signal_osc": float("nan"), "signal_high": float(high[i]), "signal_low": float(low[i]),
        "signal_delta_pct": float("nan"), "signal_trend": int(tr[i]),
    } for k, i in enumerate(picks)]


def exit_model(mult: float) -> strategy.ExitModel:
    return strategy.ExitModel(
        exit_id=f"ema_cross_atr{mult:g}", stop_kind="atr_from_entry", stop_param=mult,
        target_kind="none", target_param=0.0, time_stop_bars=None,
        exit_on_ribbon_flip=True, atr_length=ATR_LENGTH,
    )


def resample(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = bars.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return out


def run(bars: pd.DataFrame, *, params: strategy.StrategyParams = strategy.StrategyParams()) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for long_only in (False, True):
        entries, trend = build(bars, long_only=long_only)
        n_long = sum(e["side"] == "long" for e in entries)
        for mult in STOP_MULTS:
            em = exit_model(mult)
            label = "long_only" if long_only else "long_short"
            for arm, ents in (
                ("ema_cross", entries),
                ("random_ctrl", random_entries(bars, trend, n=len(entries), n_long=n_long,
                                               token=f"{label}:{mult}:{len(bars)}")),
            ):
                trades = strategy.simulate(bars, ents, em, params, vd=trend)
                m = ablation.compute_metrics(trades)
                rows.append({"variant": label, "stop": mult, "arm": arm, **m})
    return pd.DataFrame(rows)


def render(res: pd.DataFrame, bars: pd.DataFrame, product: str) -> str:
    bh = ablation.buy_and_hold_benchmark(bars, warmup_bars=WARMUP)
    lines = [
        f"TRIPLE EMA 34/55/200 + ATR stop -- {product} -- {bars.index[WARMUP]:%Y-%m-%d} .. {bars.index[-1]:%Y-%m-%d} "
        f"({len(bars) - WARMUP} bars after warmup)",
        f"buy & hold over the same span: {bh['return_pct']:+.0f}% (compounded; max DD {bh['max_drawdown_pct']:.0f}%)",
        "",
        f"{'variant':<11}{'stop':>5}  {'arm':<12}{'n':>5}{'win%':>6}{'PF':>6}{'sum%':>8}{'exp R':>7}  {'95% CI':<17}{'maxDD%':>7}  verdict",
    ]
    for (variant, stop), g in res.groupby(["variant", "stop"], sort=False):
        real = g[g["arm"] == "ema_cross"].iloc[0]
        ctrl = g[g["arm"] == "random_ctrl"].iloc[0]
        for r in (real, ctrl):
            lines.append(
                f"{r['variant']:<11}{r['stop']:>5.1f}  {r['arm']:<12}{int(r['n_trades']):>5}"
                f"{100 * r['win_rate']:>6.1f}{r['profit_factor']:>6.2f}{r['total_return_pct']:>8.0f}"
                f"{r['expectancy_r']:>7.2f}  [{r['expectancy_r_ci_lo']:+.2f}, {r['expectancy_r_ci_hi']:+.2f}]"
                f"{r['max_drawdown_pct']:>7.1f}  {r['sufficiency']}"
            )
        sep = (real["expectancy_r_ci_lo"] > ctrl["expectancy_r_ci_hi"])
        pos = real["expectancy_r_ci_lo"] > 0
        lines.append(
            "    -> " + ("BEATS RANDOM, CI above zero" if sep and pos else
                         "beats random but CI includes zero" if sep else
                         "does NOT separate from random")
        )
    lines.append("")
    lines.append("sum% is a SUM of per-trade net returns (never compounded); B&H is compounded. "
                 "Only 'BEATS RANDOM, CI above zero' is a finding.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backtest.ema_cross")
    p.add_argument("--product", default="BTC-USD")
    p.add_argument("--source", choices=("cache", "coinbase", "synthetic"), default="cache")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--cache-dir", default="backtest/.cache")
    p.add_argument("--resample", default=None, help="e.g. 1d (6 x 4h bars) or 12h")
    p.add_argument("--n", type=int, default=3000)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    if args.source == "synthetic":
        bars = data.synthetic_bars(n=args.n, seed=args.seed)
    else:
        kw: dict[str, Any] = {"cache_dir": args.cache_dir, "allow_network": args.source == "coinbase",
                              "start": args.start}
        if args.end:
            kw["end"] = args.end
        bars = data.load_bars(args.product, **kw)
    if args.resample:
        bars = resample(bars, args.resample)
    if len(bars) <= WARMUP + 50:
        print(f"ABORT: {len(bars)} bars is too few (need > {WARMUP + 50}).")
        return 1
    res = run(bars)
    print(render(res, bars, args.product + (f" {args.resample}" if args.resample else " 4h")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
