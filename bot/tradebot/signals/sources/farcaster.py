"""Farcaster via Neynar. Needs NEYNAR_API_KEY (free tier). Farcaster is where
Base memecoins are born -- Clanker launches are Farcaster casts -- so this is
the social side of the Base picture. Casts mentioning a contract address are
recorded as mentions; engagement weights them, capped.

Neynar v2 search: GET /v2/farcaster/cast/search?q=...&limit=...
  -> result.casts[].{hash, text, timestamp, reactions{likes_count,recasts_count}}
Validate the shape with scripts/signals_probe.py before trusting it.
"""
import time
from datetime import datetime, timezone

import requests

from ... import config, journal
from .. import budget, extract, store

NAME = "farcaster"
KEYLESS = False
BASE = "https://api.neynar.com/v2/farcaster"
QUERIES = ("clanker", "0x", "base memecoin", "$")


def enabled():
    return "farcaster" in config.SIGNAL_SOURCES and bool(config.NEYNAR_API_KEY)


def _iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:
        return time.time()


def collect():
    n = 0
    cutoff = time.time() - 6 * 3600
    for q in QUERIES:
        if budget.expired():
            break
        try:
            r = requests.get(f"{BASE}/cast/search", params={"q": q, "limit": 50},
                             headers={"x-api-key": config.NEYNAR_API_KEY, "accept": "application/json"},
                             timeout=budget.TIMEOUT)
            r.raise_for_status()
            casts = ((r.json().get("result") or {}).get("casts") or [])
        except Exception as e:
            journal.log_event("signal_source_fail", detail=f"farcaster '{q}': {str(e)[:100]}")
            continue
        for c in casts:
            ts = _iso(c.get("timestamp"))
            if ts < cutoff:
                continue
            rx = c.get("reactions") or {}
            w = 1.0 + min((float(rx.get("likes_count") or 0)
                           + 2 * float(rx.get("recasts_count") or 0)) / 20.0, 3.0)
            for asset in extract.asset_ids(c.get("text") or ""):
                if store.record(NAME, asset, "mention", ref=c.get("hash"), weight=w, ts=ts):
                    n += 1
                    journal.log_discovery(asset, "farcaster", {
                        "text": (c.get("text") or "")[:140], "likes": rx.get("likes_count")})
        time.sleep(1)
    return n
