"""Birdeye holder counts. Needs BIRDEYE_API_KEY (free tier). The strategy skill
names holder growth explicitly and nothing else here provides it. For each
asset already in the signal store (last 6h), read the holder count, compare
with the last reading, and record a holder_growth event when it rose by more
than HOLDER_GROWTH_PCT. Solana only on the free tier.

  GET /defi/token_overview?address=<mint>  header X-API-KEY, x-chain: solana
    -> data.{holder, mc, liquidity, v24hUSD}
"""
import time

import requests

from ... import config, journal
from .. import budget, store

NAME = "birdeye"
KEYLESS = False
BASE = "https://public-api.birdeye.so"


def enabled():
    return "birdeye" in config.SIGNAL_SOURCES and bool(config.BIRDEYE_API_KEY)


def _overview(mint):
    r = requests.get(f"{BASE}/defi/token_overview", params={"address": mint},
                     headers={"X-API-KEY": config.BIRDEYE_API_KEY, "x-chain": "solana",
                              "accept": "application/json"}, timeout=budget.TIMEOUT)
    r.raise_for_status()
    return (r.json() or {}).get("data") or {}


def collect():
    n = 0
    assets = [r["asset_id"] for r in store.rising(limit=config.BIRDEYE_MAX_PER_PASS)
              if r["asset_id"].startswith("solana:")]
    for asset in assets:
        if budget.expired():
            break
        mint = asset.split(":", 1)[1]
        try:
            d = _overview(mint)
        except Exception as e:
            journal.log_event("signal_source_fail", detail=f"birdeye {mint[:8]}: {str(e)[:80]}")
            time.sleep(1)
            continue
        holders = int(d.get("holder") or 0)
        if not holders:
            continue
        key = f"holders:{asset}"
        prev = journal.query("SELECT v FROM kv WHERE k=?", (key,))
        prev_n = int(prev[0]["v"]) if prev else None
        from ... import state
        state.set_kv(key, holders)
        if prev_n and holders > prev_n * (1 + config.HOLDER_GROWTH_PCT):
            if store.record(NAME, asset, "holder_growth", ref=f"{prev_n}->{holders}"):
                n += 1
                journal.log_discovery(asset, "birdeye_holders", {
                    "from": prev_n, "to": holders, "mc": d.get("mc")})
        time.sleep(0.6)   # free tier: ~1-2 rps
    return n
