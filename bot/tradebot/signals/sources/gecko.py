"""GeckoTerminal trending and new pools. Keyless. JSON:API shape, the same
one ohlcv_dex has been parsing successfully for weeks:
  data[].attributes.{name, address, base_token_price_usd, reserve_in_usd,
                     volume_usd{h1,h24}, pool_created_at, transactions{h1{buys,sells}}}
  data[].relationships.base_token.data.id  = "<network>_<token address>"
"""
import time

import requests

from ... import config, journal
from .. import budget, store

NAME = "gecko"
KEYLESS = True
BASE = "https://api.geckoterminal.com/api/v2"
NETWORKS = {"solana": "solana", "base": "base"}
_last = [0.0]


def enabled():
    return "gecko" in config.SIGNAL_SOURCES


def _get(path):
    gap = config.GECKO_MIN_GAP - (time.time() - _last[0])
    if gap > 0:
        time.sleep(gap)
    _last[0] = time.time()
    r = requests.get(f"{BASE}{path}", headers={"Accept": "application/json;version=20230302"},
                     timeout=budget.TIMEOUT)
    r.raise_for_status()
    return r.json()


def _asset_of(chain, pool):
    rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    tid = rel.get("id") or ""
    # "solana_<mint>" / "base_0x..." -- split on the first underscore only
    addr = tid.split("_", 1)[1] if "_" in tid else None
    return f"{chain}:{addr}" if addr else None


def collect():
    n = 0
    for chain, net in NETWORKS.items():
        for kind, path in (("trending", f"/networks/{net}/trending_pools"),
                           ("new_pool", f"/networks/{net}/new_pools")):
            if budget.expired():
                return n
            try:
                pools = (_get(path).get("data") or [])[:config.GECKO_POOLS_PER_LIST]
            except Exception as e:
                journal.log_event("signal_source_fail", detail=f"gecko {chain} {kind}: {str(e)[:100]}")
                continue
            for p in pools:
                a = (p.get("attributes") or {})
                asset = _asset_of(chain, p)
                if not asset:
                    continue
                liq = float(a.get("reserve_in_usd") or 0)
                if liq < config.SIGNAL_MIN_LIQUIDITY_USD:
                    continue
                if store.record(NAME, asset, kind, ref=a.get("address")):
                    n += 1
                    journal.log_discovery(asset, f"gecko_{kind}", {
                        "name": a.get("name"), "liq": liq,
                        "vol_h1": (a.get("volume_usd") or {}).get("h1"),
                        "created": a.get("pool_created_at")})
    return n
