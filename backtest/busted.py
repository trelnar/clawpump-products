"""Reproduce Burry's Leaders / Broken / Busted screen on today's S&P 500.

Leaders: within 10% of the ten-year high on the record day.
Broken:  20-40% below it.   Busted: more than 40% below it.

Data: S&P 500 constituents from the public `datasets/s-and-p-500-companies`
CSV on GitHub; daily bars from Alpaca (APCA_API_KEY_ID / APCA_API_SECRET_KEY
in the environment, never printed). Current members only, so the ten-year high
is each name's own, as Burry does it.

Usage:
    set -a; source ~/.config/alpaca.env; set +a
    python3 -m backtest.busted --asof 2026-10-06
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

CONSTITUENTS = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def _creds() -> dict[str, str]:
    k, s = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not k or not s:
        raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set. Run: set -a; source ~/.config/alpaca.env; set +a")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def constituents() -> pd.DataFrame:
    with urllib.request.urlopen(CONSTITUENTS, timeout=60) as r:
        df = pd.read_csv(io.StringIO(r.read().decode()))
    df = df.rename(columns={"Symbol": "symbol", "Security": "name", "GICS Sector": "sector"})
    df["symbol"] = df["symbol"].str.replace("-", ".", regex=False)
    return df[["symbol", "name", "sector"]]


def daily_bars(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    hdr = _creds()
    frames = []
    for k in range(0, len(symbols), 100):
        chunk = symbols[k:k + 100]
        token = None
        while True:
            q = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": f"{start}T00:00:00Z",
                 "end": f"{end}T23:59:59Z", "limit": 10000, "adjustment": "split", "feed": "sip", "sort": "asc"}
            if token:
                q["page_token"] = token
            req = urllib.request.Request("https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(q), headers=hdr)
            for attempt in range(5):
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        payload = json.loads(r.read().decode())
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429 and attempt < 4:
                        time.sleep(10 * (attempt + 1)); continue
                    raise
            for sym, bars in (payload.get("bars") or {}).items():
                frames.append(pd.DataFrame(bars).assign(symbol=sym))
            token = payload.get("next_page_token")
            if not token:
                break
        print(f"  fetched {min(k + 100, len(symbols))}/{len(symbols)} symbols", file=sys.stderr)
    df = pd.concat(frames, ignore_index=True)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert("America/New_York").dt.date
    return df.rename(columns={"c": "close"})[["symbol", "t", "close"]]


def screen(bars: pd.DataFrame, members: pd.DataFrame, asof: str) -> pd.DataFrame:
    asof_d = pd.Timestamp(asof).date()
    b = bars[bars["t"] <= asof_d]
    last = b.sort_values("t").groupby("symbol").tail(1).set_index("symbol")["close"]
    high = b.groupby("symbol")["close"].max()
    out = pd.DataFrame({"close": last, "high10y": high})
    out["pct_below"] = (out["close"] / out["high10y"] - 1.0) * 100.0
    out = out.join(members.set_index("symbol")[["name", "sector"]])
    out["bucket"] = pd.cut(-out["pct_below"], [-1, 10, 20, 40, 1000],
                           labels=["leader", "middle", "broken", "busted"])
    return out.sort_values("pct_below")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backtest.busted")
    p.add_argument("--asof", default="2026-10-06", help="the record day to measure at")
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--out", default="busted.csv")
    a = p.parse_args(argv)
    members = constituents()
    start = (pd.Timestamp(a.asof) - pd.DateOffset(years=a.years)).strftime("%Y-%m-%d")
    print(f"{len(members)} members; fetching daily closes {start}..{a.asof} ...", file=sys.stderr)
    bars = daily_bars(members["symbol"].tolist(), start, a.asof)
    res = screen(bars, members, a.asof)
    res.to_csv(a.out)
    counts = res["bucket"].value_counts()
    n = len(res)
    print(f"As of {a.asof}: {n} members scored. leaders {counts.get('leader', 0)} ({100*counts.get('leader', 0)/n:.0f}%), "
          f"broken {counts.get('broken', 0)} ({100*counts.get('broken', 0)/n:.0f}%), busted {counts.get('busted', 0)} ({100*counts.get('busted', 0)/n:.0f}%)")
    print("\nBUSTED (more than 40% below the ten-year high), worst first:")
    for sym, r in res[res["bucket"] == "busted"].iterrows():
        print(f"  {sym:<7}{r['pct_below']:>7.0f}%  {str(r['name'])[:32]:<33}{r['sector']}")
    print(f"\nfull table (all buckets) written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
