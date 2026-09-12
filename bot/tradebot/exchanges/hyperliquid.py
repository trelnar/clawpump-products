"""Hyperliquid perpetuals: the venue for the short leg.

Wraps the official SDK (hyperliquid-python-sdk). The bot's EVM key signs;
the same address holds the margin, deposited through the Arbitrum bridge
(scripts/hl_deposit.py). Everything here is thin: sizes rounded to the
coin's szDecimals, market orders with a slippage cap, fills parsed from the
response, errors raised rather than guessed at. Leverage is pinned to
HL_LEVERAGE (1x) on every open.
"""
import math
import time

from .. import config, journal

_info = None
_ex = None
_universe = {}


def _sdk():
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    return Info, Exchange, constants


def info():
    global _info
    if _info is None:
        Info, _, constants = _sdk()
        _info = Info(constants.MAINNET_API_URL, skip_ws=True)
    return _info


def exchange():
    global _ex
    if _ex is None:
        _, Exchange, constants = _sdk()
        from eth_account import Account
        with open(config.EVM_KEYFILE) as f:
            acct = Account.from_key(f.read().strip())
        _ex = Exchange(acct, constants.MAINNET_API_URL)
    return _ex


def address():
    from .evm_dex import address as _addr
    return _addr()


def universe(refresh=False):
    global _universe
    if not _universe or refresh:
        meta = info().meta()
        _universe = {u["name"]: u for u in meta.get("universe", [])}
    return _universe


def sz_decimals(coin):
    return int(universe().get(coin, {}).get("szDecimals", 2))


def round_size(coin, sz, up=False):
    d = sz_decimals(coin)
    f = math.ceil if up else math.floor
    return f(sz * 10 ** d - 1e-9 if not up else sz * 10 ** d) / 10 ** d


def mids():
    return {k: float(v) for k, v in info().all_mids().items()}


def mid(coin):
    return mids().get(coin)


def contexts():
    """{coin: {markPx, prevDayPx, dayNtlVlm, funding, openInterest}} as floats."""
    meta, ctxs = info().meta_and_asset_ctxs()
    out = {}
    for u, c in zip(meta.get("universe", []), ctxs):
        try:
            out[u["name"]] = {
                "mark": float(c.get("markPx") or 0), "prev_day": float(c.get("prevDayPx") or 0),
                "volume_24h": float(c.get("dayNtlVlm") or 0), "funding": float(c.get("funding") or 0),
                "open_interest": float(c.get("openInterest") or 0),
                "sz_decimals": int(u.get("szDecimals", 2)),
            }
        except (TypeError, ValueError):
            continue
    return out


def candles(coin, interval="1h", hours=24):
    now_ms = int(time.time() * 1000)
    rows = info().candles_snapshot(coin, interval, now_ms - hours * 3600 * 1000, now_ms) or []
    return [{"t": r["t"], "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
             "c": float(r["c"]), "v": float(r["v"])} for r in rows]


def user_state():
    return info().user_state(address())


def account_value():
    us = user_state()
    return float((us.get("marginSummary") or {}).get("accountValue") or 0)


def positions():
    """{coin: {"size": signed float (negative = short), "entry": float, "upnl": float}}"""
    out = {}
    for ap in user_state().get("assetPositions", []) or []:
        p = ap.get("position") or {}
        try:
            out[p["coin"]] = {"size": float(p.get("szi") or 0), "entry": float(p.get("entryPx") or 0),
                              "upnl": float(p.get("unrealizedPnl") or 0)}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _parse(res):
    """-> (filled_size, avg_px, oid). Raises on an error status or no fill."""
    if not res or res.get("status") != "ok":
        raise RuntimeError(f"order rejected: {str(res)[:200]}")
    statuses = (((res.get("response") or {}).get("data") or {}).get("statuses") or [])
    for s in statuses:
        if "error" in s:
            raise RuntimeError(f"order error: {s['error']}")
        if "filled" in s:
            f = s["filled"]
            return float(f["totalSz"]), float(f["avgPx"]), f.get("oid")
    raise RuntimeError(f"no fill in response: {str(res)[:200]}")


def open_short(coin, notional_usd, slippage=None):
    """Market-sell to open. Returns (size, avg_px, oid)."""
    slippage = config.HL_SLIPPAGE if slippage is None else slippage
    px = mid(coin)
    if not px:
        raise RuntimeError(f"no mid for {coin}")
    # Round UP: flooring a $10 order to szDecimals landed under the $10
    # minimum on every coin, and the exchange evaluates the minimum at the
    # slippage-adjusted price, so clear it with margin.
    sz = round_size(coin, notional_usd / px, up=True)
    if sz <= 0 or sz * px * (1 - slippage) < config.HL_MIN_NOTIONAL_USD:
        raise RuntimeError(f"size {sz} x {px} below the ${config.HL_MIN_NOTIONAL_USD} minimum")
    ex = exchange()
    lev = ex.update_leverage(config.HL_LEVERAGE, coin, True)
    if isinstance(lev, dict) and lev.get("status") != "ok":
        raise RuntimeError(f"leverage not set: {str(lev)[:120]}")
    res = ex.market_open(coin, False, sz, None, slippage)
    filled, avg, oid = _parse(res)
    journal.log_event("hl_order", f"perp:{coin}", {"side": "short_open", "sz": filled, "px": avg,
                                                   "oid": oid})
    return filled, avg, oid


def close_short(coin, sz=None, slippage=None):
    """Market-buy to cover `sz` (None = whole position). Returns (size, avg_px, oid)."""
    slippage = config.HL_SLIPPAGE if slippage is None else slippage
    if sz is not None:
        sz = round_size(coin, sz)
        if sz <= 0:
            raise RuntimeError("cover size rounds to zero")
    res = exchange().market_close(coin, sz, None, slippage)
    filled, avg, oid = _parse(res)
    journal.log_event("hl_order", f"perp:{coin}", {"side": "short_close", "sz": filled, "px": avg,
                                                   "oid": oid})
    return filled, avg, oid
