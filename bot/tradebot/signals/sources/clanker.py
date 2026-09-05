"""Clanker token launches on Base -- the Farcaster-native launcher. Keyless,
unofficial API. Fields relied on: contract_address, symbol, name, created_at
(ISO), cast_hash. A Clanker launch is where most Base memecoin waves begin.
"""
import time
from datetime import datetime, timezone

import requests

from ... import config, journal
from .. import store

NAME = "clanker"
KEYLESS = True
BASE = "https://www.clanker.world/api"


def enabled():
    return "clanker" in config.SIGNAL_SOURCES


def _iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:
        return 0.0


def collect():
    n = 0
    now = time.time()
    try:
        r = requests.get(f"{BASE}/tokens", params={"page": 1, "sort": "desc"}, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 tradebot-signals"})
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        journal.log_event("signal_source_fail", detail=f"clanker: {str(e)[:100]}")
        return 0
    toks = j.get("data") if isinstance(j, dict) else j
    for t in toks or []:
        addr = (t.get("contract_address") or "").lower()
        if not addr.startswith("0x"):
            continue
        created = _iso(t.get("created_at"))
        if not created or now - created > 12 * 3600:
            continue
        asset = f"base:{addr}"
        if store.record(NAME, asset, "launch", ref=addr, ts=created):
            n += 1
            journal.log_discovery(asset, "clanker_launch", {
                "symbol": t.get("symbol"), "name": t.get("name"),
                "cast": t.get("cast_hash")})
    return n
