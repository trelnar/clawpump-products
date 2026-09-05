"""pump.fun launches, king-of-the-hill and graduations. Keyless but
UNOFFICIAL -- this is the site's frontend API and its shape has changed
before. Everything is read with .get() and a missing field skips the coin
rather than raising.

Three reads per pass, each budget-checked:
  /coins?sort=market_cap&order=DESC       -> launch: created < 6h and mcap >= min.
     (sorting by created_timestamp only ever showed the last few minutes of
      10-20 launches/min, all at $4-6k mcap, so 'launch' was ~0 forever)
  /coins?sort=last_trade_timestamp&complete=true -> graduation (bonding curve
     filled; the hardest momentum event on Solana memecoins)
  /coins/king-of-the-hill                  -> trending

Fields relied on: mint, created_timestamp (ms), complete, usd_market_cap,
king_of_the_hill_timestamp, reply_count. Validate with signals_probe.py.
"""
import time

import requests

from ... import config, journal
from .. import budget, store

NAME = "pumpfun"
KEYLESS = True
BASES = ["https://frontend-api-v3.pump.fun", "https://frontend-api-v2.pump.fun",
         "https://frontend-api.pump.fun"]
_base = [BASES[0]]
_UA = {"User-Agent": "Mozilla/5.0 tradebot-signals"}


def enabled():
    return "pumpfun" in config.SIGNAL_SOURCES


def _get(path, params):
    """One base per call: the remembered good one, and on failure the next in
    the ring. Fanning out over all three per read made a dead host cost
    45s x reads instead of 15s."""
    i = BASES.index(_base[0])
    last = None
    for b in (BASES[i], BASES[(i + 1) % len(BASES)]):
        try:
            r = requests.get(f"{b}{path}", params=params, timeout=budget.TIMEOUT, headers=_UA)
            r.raise_for_status()
            _base[0] = b
            return r.json()
        except Exception as e:
            last = e
            _base[0] = BASES[(BASES.index(b) + 1) % len(BASES)]
    raise RuntimeError(f"pump.fun unreachable: {last}")


def _coins(j):
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        if j.get("mint"):
            return [j]                      # king-of-the-hill returns one coin
        return j.get("coins") or j.get("data") or []
    return []


def _launch(c, now):
    created = float(c.get("created_timestamp") or 0) / 1000.0
    mcap = float(c.get("usd_market_cap") or 0)
    if c.get("complete") or not created or now - created > 6 * 3600:
        return 0
    if mcap < config.PUMPFUN_MIN_MCAP_USD:
        return 0
    asset = f"solana:{c['mint']}"
    if store.record(NAME, asset, "launch", ref=c["mint"], ts=created):
        journal.log_discovery(asset, "pumpfun_launch", {
            "symbol": c.get("symbol"), "mcap": mcap, "replies": c.get("reply_count")})
        return 1
    return 0


def _graduation(c, now):
    if not c.get("complete"):
        return 0
    asset = f"solana:{c['mint']}"
    if store.record(NAME, asset, "graduation", ref=c["mint"]):
        journal.log_discovery(asset, "pumpfun_graduation", {
            "symbol": c.get("symbol"), "mcap": float(c.get("usd_market_cap") or 0)})
        return 1
    return 0


def _koth(c, now):
    koth = c.get("king_of_the_hill_timestamp")
    if not koth or now - float(koth) / 1000.0 > 3600:
        return 0
    return 1 if store.record(NAME, f"solana:{c['mint']}", "trending", ref=f"koth:{c['mint']}") else 0


READS = (
    ("/coins", {"sort": "market_cap", "order": "DESC"}, _launch),
    ("/coins", {"sort": "last_trade_timestamp", "order": "DESC", "complete": "true"}, _graduation),
    ("/coins/king-of-the-hill", {}, _koth),
)


def collect():
    n = 0
    now = time.time()
    for path, params, handler in READS:
        if budget.expired():
            break
        try:
            j = _get(path, {"offset": 0, "limit": config.PUMPFUN_LIMIT,
                            "includeNsfw": "false", **params})
        except Exception as e:
            journal.log_event("signal_source_fail", detail=f"pumpfun {path}: {str(e)[:100]}")
            continue
        for c in _coins(j):
            if not c.get("mint"):
                continue
            n += handler(c, now)
            n += _koth(c, now) if handler is not _koth else 0
    return n
