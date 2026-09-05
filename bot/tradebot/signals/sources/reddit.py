"""Reddit new posts in memecoin subs. Shape is stable:
  data.children[].data.{title, selftext, ups, num_comments, created_utc, permalink}
Slower than Telegram by hours -- confirmation, not discovery -- but free and
legitimate, and a post with a contract address in it is unambiguous.

The public .json endpoints answer from a home IP but return 403 "Blocked"
from datacenter ranges (the VPS). With a free 'script' app (REDDIT_CLIENT_ID
and REDDIT_CLIENT_SECRET in agent.env) the same reads go through
oauth.reddit.com with an app-only token, which is what Reddit wants anyway.
"""
import time

import requests

from ... import config, journal
from .. import budget, extract, store

NAME = "reddit"
KEYLESS = True
UA = "tradebot-signals/1.0 (research; contact via repo)"
_last = [0.0]
_token = [None, 0.0]        # bearer, expiry


def _bearer():
    """App-only OAuth token, cached until near expiry. None when unkeyed."""
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return None
    if _token[0] and time.time() < _token[1] - 60:
        return _token[0]
    r = requests.post("https://www.reddit.com/api/v1/access_token",
                      auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
                      data={"grant_type": "client_credentials"},
                      headers={"User-Agent": UA}, timeout=budget.TIMEOUT)
    r.raise_for_status()
    j = r.json()
    _token[0] = j["access_token"]
    _token[1] = time.time() + float(j.get("expires_in") or 3600)
    return _token[0]


def enabled():
    return "reddit" in config.SIGNAL_SOURCES


def _get(sub):
    gap = 2.5 - (time.time() - _last[0])
    if gap > 0:
        time.sleep(gap)
    _last[0] = time.time()
    tok = _bearer()
    if tok:
        r = requests.get(f"https://oauth.reddit.com/r/{sub}/new", params={"limit": 50},
                         headers={"User-Agent": UA, "Authorization": f"bearer {tok}"},
                         timeout=budget.TIMEOUT)
    else:
        r = requests.get(f"https://www.reddit.com/r/{sub}/new.json",
                         params={"limit": 50}, headers={"User-Agent": UA}, timeout=budget.TIMEOUT)
    if r.status_code == 403 and not tok:
        raise RuntimeError("403 Blocked: Reddit refuses unauthenticated reads from this IP; "
                           "set REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET (SIGNALS.md)")
    r.raise_for_status()
    return r.json()


def collect():
    n = 0
    cutoff = time.time() - 6 * 3600
    for sub in config.REDDIT_SUBS:
        if budget.expired():
            break
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
