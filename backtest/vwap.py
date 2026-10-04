"""Intraday VWAP reversal test on US stocks, with a random-entry control.

The system as pitched (TikTok, "$2,000 in five minutes"): put VWAP on the chart,
pick a high-ATR stock, wait for a pop and a hard reversal below VWAP, short it,
hold until "a sign of reversal". The pitch leaves the trigger, stop and exit
undefined. This module makes them mechanical so they can be measured:

  SHORT  a new session high prints after 09:45 ET with price above VWAP; within
         SETUP_WINDOW bars of that high the first 1-minute CLOSE below VWAP is
         the entry (at that close). Stop = the session high. Cover on the first
         1-minute close back above VWAP, or at 15:55 ET.
  LONG   the mirror: new session low, first close above VWAP, stop = session
         low, exit on a close back below VWAP or at 15:55.

Control: for every real trade, one random entry on the SAME day, SAME side,
random bar in the eligible window, same stop rule (session extreme so far) and
the same exit rule. If the setup carries information, real beats random.

Costs: no commission, SLIPPAGE_BPS per side (spread + impact on liquid names).

Data: Alpaca Market Data v2, 1-minute bars, SIP feed (full consolidated tape,
free on the Basic plan for history older than 15 minutes). Credentials are read
from the environment: APCA_API_KEY_ID and APCA_API_SECRET_KEY. They are never
printed, never written by this module, never passed on the command line.

Usage (on the VPS, after `set -a; source ~/.config/alpaca.env; set +a`):
    python3 -m backtest.vwap fetch --symbols TSLA,NVDA --start 2024-10-01 --end 2026-10-01
    python3 -m backtest.vwap run   --symbols TSLA,NVDA
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
import zlib
from typing import Any

import numpy as np
import pandas as pd

from backtest import ablation, strategy

CACHE_DIR = pathlib.Path("backtest/.cache/stocks")
SESSION_OPEN, SESSION_CLOSE = "09:30", "16:00"
ELIGIBLE_FROM, LAST_ENTRY, FLAT_AT = "09:45", "15:30", "15:55"
SETUP_WINDOW = 30          # bars after the session extreme in which the VWAP cross must happen
MIN_HOLD_BARS = 1          # the reclaim exit cannot fire on the entry bar
SLIPPAGE_BPS = 2.0         # per side
RANDOM_SEED = 20261004


# --------------------------------------------------------------------------- data

def _creds() -> dict[str, str]:
    k, s = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not k or not s:
        raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY are not set in the environment. "
                         "Run: set -a; source ~/.config/alpaca.env; set +a")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def fetch_minutes(symbol: str, start: str, end: str, *, feed: str = "sip") -> pd.DataFrame:
    """All 1-minute bars for `symbol` in [start, end], paginated. Returns UTC-indexed OHLCV + vw."""
    hdr = _creds()
    rows: list[dict] = []
    token: str | None = None
    while True:
        q = {"timeframe": "1Min", "start": f"{start}T00:00:00Z", "end": f"{end}T23:59:59Z",
             "limit": 10000, "adjustment": "raw", "feed": feed, "sort": "asc"}
        if token:
            q["page_token"] = token
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers=hdr)
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 4:
                    time.sleep(10 * (attempt + 1)); continue
                raise
        rows.extend(payload.get("bars") or [])
        token = payload.get("next_page_token")
        if not token:
            break
    if not rows:
        raise SystemExit(f"{symbol}: no bars returned for {start}..{end}")
    df = pd.DataFrame(rows).rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low",
                                            "c": "close", "v": "volume", "vw": "vw"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.set_index("timestamp")[["open", "high", "low", "close", "volume", "vw"]].astype("float64")


def cache_path(symbol: str) -> pathlib.Path:
    return CACHE_DIR / f"{symbol}_1min.csv"


def load_cached(symbol: str) -> pd.DataFrame:
    p = cache_path(symbol)
    if not p.exists():
        raise SystemExit(f"no cache for {symbol}: run `python3 -m backtest.vwap fetch --symbols {symbol} ...`")
    return pd.read_csv(p, parse_dates=["timestamp"]).set_index("timestamp")


# --------------------------------------------------------------------------- session prep

def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep 09:30-16:00 New York bars; add session date, minute-of-day, session VWAP."""
    ny = df.index.tz_convert("America/New_York")
    out = df.copy()
    out["ny"] = ny
    hm = ny.strftime("%H:%M")
    out = out[(hm >= SESSION_OPEN) & (hm < SESSION_CLOSE)].copy()
    out["day"] = out["ny"].dt.date
    out["hm"] = out["ny"].dt.strftime("%H:%M")
    pv = (out["vw"].fillna((out["high"] + out["low"] + out["close"]) / 3.0) * out["volume"])
    out["vwap"] = pv.groupby(out["day"]).cumsum() / out["volume"].groupby(out["day"]).cumsum().replace(0, np.nan)
    out["vwap"] = out["vwap"].ffill()
    return out


# --------------------------------------------------------------------------- the rule

def _day_trades(d: pd.DataFrame, symbol: str) -> list[dict]:
    """Real-rule trades for one session, one position at a time."""
    n = len(d)
    hi, lo, cl, vw, hm = (d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy(),
                          d["vwap"].to_numpy(), d["hm"].to_numpy())
    idx = d.index
    sess_hi = np.maximum.accumulate(hi)
    sess_lo = np.minimum.accumulate(lo)
    trades: list[dict] = []
    i = 0
    while i < n:
        if hm[i] < ELIGIBLE_FROM or hm[i] > LAST_ENTRY:
            i += 1; continue
        new_hi = hi[i] >= sess_hi[i] and cl[i] > vw[i]
        new_lo = lo[i] <= sess_lo[i] and cl[i] < vw[i]
        fired = None
        if new_hi or new_lo:
            for j in range(i + 1, min(n, i + 1 + SETUP_WINDOW)):
                if hm[j] > LAST_ENTRY:
                    break
                if (new_hi and hi[j] > sess_hi[i]) or (new_lo and lo[j] < sess_lo[i]):
                    break  # extreme extended: the setup re-arms on that bar
                if new_hi and cl[j] < vw[j]:
                    fired = _single(d, symbol, j, "short"); break
                if new_lo and cl[j] > vw[j]:
                    fired = _single(d, symbol, j, "long"); break
        if fired:
            trades.append(fired)
            i = idx.get_loc(fired["exit_ts"]) + 1
        else:
            i += 1
    return trades


def control_for(d: pd.DataFrame, real: list[dict], symbol: str, rng: np.random.Generator) -> list[dict]:
    """One random entry per real trade: same day, same side, random eligible bar."""
    hm = d["hm"].to_numpy()
    pool = np.flatnonzero((hm >= ELIGIBLE_FROM) & (hm <= LAST_ENTRY))
    out: list[dict] = []
    if pool.size == 0:
        return out
    for t in real:
        for _ in range(20):
            r = _single(d, symbol, int(rng.choice(pool)), t["side"])
            if r:
                out.append(r); break
    return out


def _single(d: pd.DataFrame, symbol: str, i: int, side: str) -> dict | None:
    # replicate the inner simulate for a single forced entry
    n = len(d)
    hi, lo, cl, vw, hm, op = (d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy(),
                              d["vwap"].to_numpy(), d["hm"].to_numpy(), d["open"].to_numpy())
    idx = d.index
    sess_hi = np.maximum.accumulate(hi); sess_lo = np.minimum.accumulate(lo)
    sign = 1 if side == "long" else -1
    stop = sess_lo[i] if side == "long" else sess_hi[i]
    entry = cl[i]
    if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
        return None
    risk = abs(entry - stop)
    exit_px = exit_i = reason = None
    mae = mfe = 0.0
    for j in range(i + 1, n):
        if side == "short" and hi[j] >= stop:
            exit_px, exit_i, reason = (op[j] if op[j] > stop else stop), j, "stop"; break
        if side == "long" and lo[j] <= stop:
            exit_px, exit_i, reason = (op[j] if op[j] < stop else stop), j, "stop"; break
        mae = max(mae, (entry - lo[j]) / entry if side == "long" else (hi[j] - entry) / entry)
        mfe = max(mfe, (hi[j] - entry) / entry if side == "long" else (entry - lo[j]) / entry)
        if j - i >= MIN_HOLD_BARS and ((side == "short" and cl[j] > vw[j]) or (side == "long" and cl[j] < vw[j])):
            exit_px, exit_i, reason = cl[j], j, "vwap_reclaim"; break
        if hm[j] >= FLAT_AT:
            exit_px, exit_i, reason = cl[j], j, "time"; break
    if exit_i is None:
        exit_px, exit_i, reason = cl[n - 1], n - 1, "time"
    gross = sign * (exit_px - entry) / entry
    net = gross - 2 * SLIPPAGE_BPS / 1e4
    return {
        "side": side, "signal_ts": idx[i], "entry_ts": idx[i], "exit_ts": idx[exit_i],
        "entry_price": float(entry), "exit_price": float(exit_px), "stop_price": float(stop),
        "target_price": float("nan"), "exit_reason": reason, "bars_held": int(exit_i - i),
        "return_pct": float(net * 100), "r_multiple": float(net * entry / risk),
        "mae_pct": float(mae * 100), "mfe_pct": float(mfe * 100),
        "signal_osc": float("nan"), "signal_delta_pct": float("nan"), "signal_trend": 0,
        "symbol": symbol, "day": str(d["day"].iloc[0]),
    }


def run_symbol(df: pd.DataFrame, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    s = regular_session(df)
    rng = np.random.default_rng([RANDOM_SEED, zlib.crc32(symbol.encode())])
    real: list[dict] = []
    ctrl: list[dict] = []
    for _, d in s.groupby("day", sort=True):
        if len(d) < 200:          # half days / holes: skip
            continue
        t = _day_trades(d, symbol)
        real.extend(t)
        ctrl.extend(control_for(d, t, symbol, rng))
    return _frame(real), _frame(ctrl)


def _frame(rows: list[dict]) -> pd.DataFrame:
    cols = list(strategy.TRADE_COLUMNS) + ["symbol", "day"]
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})
    df = pd.DataFrame(rows)
    df.insert(0, "trade_id", np.arange(len(df)))
    return df[cols]


# --------------------------------------------------------------------------- report

def _line(label: str, m: dict) -> str:
    return (f"{label:<14}{int(m['n_trades']):>6}{100 * m['win_rate']:>7.1f}{m['profit_factor']:>7.2f}"
            f"{m['avg_return_pct']:>8.3f}{m['total_return_pct']:>8.1f}{m['expectancy_r']:>8.2f}  "
            f"[{m['expectancy_r_ci_lo']:+.2f}, {m['expectancy_r_ci_hi']:+.2f}]  {m['sufficiency']}")


def render(results: dict[str, tuple[pd.DataFrame, pd.DataFrame]]) -> str:
    lines = ["VWAP REVERSAL (1-min, US stocks) -- real setup vs random entry, same day/side/exit",
             f"{'symbol/arm':<14}{'n':>6}{'win%':>7}{'PF':>7}{'avg%':>8}{'sum%':>8}{'exp R':>8}  95% CI          verdict"]
    all_real, all_ctrl = [], []
    for sym, (real, ctrl) in results.items():
        for side in ("short", "long"):
            r, c = real[real["side"] == side], ctrl[ctrl["side"] == side]
            mr, mc = ablation.compute_metrics(r), ablation.compute_metrics(c)
            lines.append(_line(f"{sym} {side}", mr))
            lines.append(_line(f"  random", mc))
            sep = mr["expectancy_r_ci_lo"] > mc["expectancy_r_ci_hi"]
            lines.append("    -> " + ("BEATS RANDOM, CI above zero" if sep and mr["expectancy_r_ci_lo"] > 0
                                     else "beats random but CI includes zero" if sep
                                     else "does NOT separate from random"))
        all_real.append(real); all_ctrl.append(ctrl)
    R, C = pd.concat(all_real), pd.concat(all_ctrl)
    mr, mc = ablation.compute_metrics(R), ablation.compute_metrics(C)
    lines += ["", _line("ALL real", mr), _line("ALL random", mc)]
    lines.append("    -> " + ("BEATS RANDOM, CI above zero" if mr["expectancy_r_ci_lo"] > max(0, mc["expectancy_r_ci_hi"])
                             else "does NOT separate from random"))
    if len(R):
        lines.append("")
        lines.append("exit reasons (real): " + ", ".join(f"{k} {v}" for k, v in R["exit_reason"].value_counts().items()))
        lines.append(f"avg hold (real): {R['bars_held'].mean():.0f} min; costs {2 * SLIPPAGE_BPS:.0f} bps per round trip")
    lines.append("")
    lines.append("avg%/sum% are per-trade net returns (sum never compounded). Only 'BEATS RANDOM, CI above zero' is a finding.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backtest.vwap")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("--symbols", required=True)
    f.add_argument("--start", required=True); f.add_argument("--end", required=True)
    f.add_argument("--feed", default="sip")
    r = sub.add_parser("run"); r.add_argument("--symbols", required=True)
    r.add_argument("--out", default=None, help="write all trades to this CSV")
    a = p.parse_args(argv)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.cmd == "fetch":
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for s in syms:
            df = fetch_minutes(s, a.start, a.end, feed=a.feed)
            df.index.name = "timestamp"
            df.to_csv(cache_path(s))
            print(f"{s}: {len(df)} bars {df.index[0]:%Y-%m-%d} .. {df.index[-1]:%Y-%m-%d}")
        return 0
    results = {s: run_symbol(load_cached(s), s) for s in syms}
    print(render(results))
    if a.out:
        pd.concat([x[0].assign(arm="real") for x in results.values()] +
                  [x[1].assign(arm="random") for x in results.values()]).to_csv(a.out, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
