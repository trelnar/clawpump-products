"""pump.fun launches and graduations. Keyless but UNOFFICIAL -- this is the
site's frontend API and its shape has changed before. Everything is read with
.get() and a missing field skips the coin rather than raising.

Fields relied on: mint, created_timestamp (ms), complete (bool: graduated
to Raydium), king_of_the_hill_timestamp, usd_market_cap, reply_count.
A graduation is the single hardest momentum event on Solana memecoins.
Validate with scripts/signals_probe.py before trusting it.
"""
import time

import requests

from ... import config, journal
from .. import store

NAME = "pumpfun"
KEYLESS = True
BASES = ["https://frontend-api-v3.pump.fun", "https://frontend-api-v2.pump.fun",
         "https://frontend-api.pump.fun"]
_base = [None]


def enabled():
    return "pumpfun" in config.SIGNAL_SOURCES


def _get(path, params):
    order = ([_base[0]] if _base[0] else []) + [b for b in BASES if b != _base[0]]
    last = None
    for b in order:
        try:
            r = requests.get(f"{b}{path}", params=params, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0 tradebot-signals"})
            r.raise_for_status()
            _base[0] = b
            return r.json()
        except Exception as e:
            last = e
    raise RuntimeError(f"pump.fun unreachable: {last}")


def collect():
    n = 0
    now = time.time()
    for sort, kind in (("created_timestamp", "launch"), ("last_trade_timestamp", None)):
        try:
            coins = _get("/coins", {"offset": 0, "limit": config.PUMPFUN_LIMIT,
                                    "sort": sort, "order": "DESC", "includeNsfw": "false"})
        except Exception as e:
            journal.log_event("signal_source_fail", detail=f"pumpfun {sort}: {str(e)[:100]}")
            continue
        if not isinstance(coins, list):
            coins = (coins or {}).get("coins") or []
        for c in coins:
            mint = c.get("mint")
            if not mint:
                continue
            asset = f"solana:{mint}"
            created = float(c.get("created_timestamp") or 0) / 1000.0
            mcap = float(c.get("usd_market_cap") or 0)
            if c.get("complete"):
                # graduated: the bonding curve filled and it moved to a real pool
                if store.record(NAME, asset, "graduation", ref=mint):
                    n += 1
                    journal.log_discovery(asset, "pumpfun_graduation",
                                          {"symbol": c.get("symbol"), "mcap": mcap})
            elif (kind == "launch" and created and now - created < 6 * 3600
                  and mcap >= config.PUMPFUN_MIN_MCAP_USD):
                if store.record(NAME, asset, "launch", ref=mint, ts=created):
                    n += 1
                    journal.log_discovery(asset, "pumpfun_launch", {
                        "symbol": c.get("symbol"), "mcap": mcap,
                        "replies": c.get("reply_count")})
            koth = c.get("king_of_the_hill_timestamp")
            if koth and now - float(koth) / 1000.0 < 3600:
                if store.record(NAME, asset, "trending", ref=f"koth:{mint}"):
                    n += 1
    return n
