"""Reddit new posts in memecoin subs. Keyless: the public .json endpoints
answer with a descriptive User-Agent at ~1 req / 2s. Shape is stable:
  data.children[].data.{title, selftext, ups, num_comments, created_utc, permalink}
Slower than Telegram by hours -- confirmation, not discovery -- but free and
legitimate, and a post with a contract address in it is unambiguous.
"""
import time

import requests

from ... import config, journal
from .. import extract, store

NAME = "reddit"
KEYLESS = True
UA = "tradebot-signals/1.0 (research; contact via repo)"
_last = [0.0]


def enabled():
    return "reddit" in config.SIGNAL_SOURCES


def _get(sub):
    gap = 2.5 - (time.time() - _last[0])
    if gap > 0:
        time.sleep(gap)
    _last[0] = time.time()
    r = requests.get(f"https://www.reddit.com/r/{sub}/new.json",
                     params={"limit": 50}, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    return r.json()


def collect():
    n = 0
    cutoff = time.time() - 6 * 3600
    for sub in config.REDDIT_SUBS:
        try:
            posts = ((_get(sub).get("data") or {}).get("children") or [])
        except Exception as e:
            journal.log_event("signal_source_fail", detail=f"reddit r/{sub}: {str(e)[:100]}")
            continue
        for p in posts:
            d = p.get("data") or {}
            if float(d.get("created_utc") or 0) < cutoff:
                continue
            text = f"{d.get('title') or ''}\n{d.get('selftext') or ''}"
            # weight by engagement, capped: a 500-upvote post is not 500 mentions
            w = 1.0 + min(float(d.get("ups") or 0) / 25.0, 3.0)
            for asset in extract.asset_ids(text):
                if store.record(NAME, asset, "post", ref=d.get("permalink"), weight=w,
                                ts=float(d.get("created_utc") or time.time())):
                    n += 1
                    journal.log_discovery(asset, f"reddit_{sub}", {
                        "title": (d.get("title") or "")[:140], "ups": d.get("ups"),
                        "comments": d.get("num_comments")})
    return n
