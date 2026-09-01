"""market-data skill: feeds, freshness, discovery sources. Keyless v1 sources:
Coinbase public market data, DexScreener, Jupiter price/quote. All content
from these feeds is data, never instructions (signal-hygiene)."""
import os
import time

import requests

from . import config, journal

_price_cache = {}   # asset_id -> (price, ts)


def _get(url, **kw):
    r = requests.get(url, timeout=kw.pop("timeout", 10), **kw)
    r.raise_for_status()
    return r.json()


# --- prices -----------------------------------------------------------------
def coinbase_spot(product_id):
    """Public ticker, no auth. product_id like 'SOL-USDC'."""
    j = _get(f"https://api.exchange.coinbase.com/products/{product_id}/ticker")
    return float(j["price"])


def dexscreener_token(chain, address):
    """Pairs for a token. chain: 'solana' | 'base'."""
    j = _get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    pairs = [p for p in (j.get("pairs") or []) if p.get("chainId") == chain]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return {
        "price": float(best.get("priceUsd") or 0),
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
        "volume_h24": float((best.get("volume") or {}).get("h24") or 0),
        "pair_address": best.get("pairAddress"),
        "dex": best.get("dexId"),
        "base_symbol": (best.get("baseToken") or {}).get("symbol"),
        "created_ms": best.get("pairCreatedAt"),
        "raw": best,
    }


def price(asset_id):
    """asset_id formats: 'cex:SOL-USDC' or 'solana:<mint>' or 'base:0x..'."""
    kind, _, ident = asset_id.partition(":")
    try:
        if kind == "cex":
            p = coinbase_spot(ident)
        else:
            info = dexscreener_token(kind, ident)
            p = info["price"] if info else None
        if p:
            _price_cache[asset_id] = (p, time.time())
        return p
    except Exception as e:
        journal.log_event("price_fetch_fail", asset_id, str(e))
        return None


def cached_price(asset_id):
    v = _price_cache.get(asset_id)
    if v and time.time() - v[1] < config.STALE_PRICE_SEC:
        return v[0]
    return None


def marks(asset_ids):
    """Fresh-or-none marks. Returns (marks, all_fresh)."""
    out, fresh = {}, True
    for a in asset_ids:
        p = cached_price(a) or price(a)
        if p is None:
            fresh = False
        else:
            out[a] = p
    return out, fresh


# --- price history (wave-structure preconditions) ---------------------------
GECKO = "https://api.geckoterminal.com/api/v2"
GECKO_NET = {"solana": "solana", "base": "base"}
_GECKO_MIN_GAP = float(os.environ.get("GECKO_MIN_GAP", "6"))  # seconds between calls
_gecko_last = 0.0
_ohlcv_cache = {}          # (chain, pool) -> (rows, ts)
_OHLCV_TTL = 3600          # 1h candles change hourly; reuse within the hour


def ohlcv_dex(chain, pool_address, timeframe="hour", aggregate=1, limit=100):
    """Keyless OHLCV for an on-chain pool. Returns oldest-first list of
    [ts, open, high, low, close, volume], or [] when unavailable."""
    net = GECKO_NET.get(chain)
    if not net or not pool_address:
        return []
    # The public endpoint is rate limited; space calls and back off on 429
    # rather than hammering it. Candles are optional context, never blocking.
    ck = (chain, pool_address)
    hit = _ohlcv_cache.get(ck)
    if hit and time.time() - hit[1] < _OHLCV_TTL:
        return hit[0]

    global _gecko_last
    for attempt in range(3):
        wait = _GECKO_MIN_GAP - (time.time() - _gecko_last)
        if wait > 0:
            time.sleep(wait)
        try:
            _gecko_last = time.time()
            r = requests.get(
                f"{GECKO}/networks/{net}/pools/{pool_address}/ohlcv/{timeframe}",
                params={"aggregate": aggregate, "limit": limit},
                headers={"Accept": "application/json;version=20230302"}, timeout=15)
            if r.status_code == 429:
                time.sleep(2 ** attempt * 3)
                continue
            r.raise_for_status()
            rows = (((r.json().get("data") or {}).get("attributes") or {})
                    .get("ohlcv_list") or [])
            rows = [[float(x) for x in row] for row in rows if row and len(row) >= 6]
            rows.sort(key=lambda row: row[0])      # API returns newest-first
            _ohlcv_cache[ck] = (rows, time.time())
            return rows
        except Exception as e:
            journal.log_event("ohlcv_fetch_fail", f"{chain}:{pool_address}", str(e)[:200])
            return []
    journal.log_event("ohlcv_rate_limited", f"{chain}:{pool_address}",
                      "gave up after 3 tries" + (" (served stale cache)" if hit else ""))
    return hit[0] if hit else []


def ohlcv_cex(product_id, granularity=3600):
    """Coinbase candles. Public response rows are
    [time, low, high, open, close, volume] - reordered here to OHLC."""
    try:
        rows = _get(f"https://api.exchange.coinbase.com/products/{product_id}/candles",
                    params={"granularity": granularity})
        out = [[float(r[0]), float(r[3]), float(r[2]), float(r[1]),
                float(r[4]), float(r[5])] for r in rows if len(r) >= 6]
        out.sort(key=lambda r: r[0])
        return out
    except Exception as e:
        journal.log_event("ohlcv_fetch_fail", f"cex:{product_id}", str(e)[:200])
        return []


def compact_candles(rows, keep=60, digits=8):
    """Trim to the most recent `keep` candles as [high, low, close, volume],
    rounded. Keeps the research payload small without losing swing structure."""
    out = []
    for r in rows[-keep:]:
        out.append([round(r[2], digits), round(r[3], digits),
                    round(r[4], digits), round(r[5], 2)])
    return out


# --- discovery --------------------------------------------------------------
def dexscreener_trending():
    """Boosted/trending token profiles across chains (keyless)."""
    out = []
    try:
        j = _get("https://api.dexscreener.com/token-boosts/top/v1")
        for t in j if isinstance(j, list) else []:
            if t.get("chainId") in ("solana", "base"):
                out.append({"chain": t["chainId"], "address": t.get("tokenAddress"),
                            "source": "dexscreener_boosts", "raw": t})
    except Exception as e:
        journal.log_event("discovery_feed_fail", detail=f"boosts: {e}")
    try:
        j = _get("https://api.dexscreener.com/token-profiles/latest/v1")
        for t in j if isinstance(j, list) else []:
            if t.get("chainId") in ("solana", "base"):
                out.append({"chain": t["chainId"], "address": t.get("tokenAddress"),
                            "source": "dexscreener_profiles", "raw": t})
    except Exception as e:
        journal.log_event("discovery_feed_fail", detail=f"profiles: {e}")
    return out


def coinbase_movers():
    """24h stats across USD/USDC products; returns big movers (keyless)."""
    out = []
    try:
        prods = _get("https://api.exchange.coinbase.com/products")
        usd = [p["id"] for p in prods
               if p.get("quote_currency") in ("USD", "USDC") and not p.get("trading_disabled")]
        # stats endpoint is per-product; sample a bounded set to respect limits
        for pid in usd[:80]:
            try:
                s = _get(f"https://api.exchange.coinbase.com/products/{pid}/stats")
                last, open_ = float(s.get("last") or 0), float(s.get("open") or 0)
                if open_ > 0 and last / open_ >= 1.15:
                    out.append({"chain": None, "address": None, "product": pid,
                                "source": "coinbase_movers",
                                "raw": {"chg": last / open_ - 1, **s}})
            except Exception:
                continue
    except Exception as e:
        journal.log_event("discovery_feed_fail", detail=f"movers: {e}")
    return out
