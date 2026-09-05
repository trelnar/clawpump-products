"""The signal layer: where attention is moving, from how many directions.

Discovery used to be two paid-promotion feeds and a slice of Coinbase movers,
so the research layer was structurally late -- it saw tokens once someone had
paid to show them. This package replaces that with sources that carry timing:
on-chain launch and graduation events, trending and new pools, social mentions
from places where calls actually originate, and holder growth. Every source is
optional and isolated; each is a module under sources/ with enabled() and
collect(). What the model receives per asset is not raw counts but the shape
of them: acceleration, breadth, first-seen age, and which hard events fired.
"""
import time

from .. import config, journal
from . import budget, store
from .sources import birdeye, clanker, farcaster, gecko, pumpfun, reddit

SOURCES = [gecko, pumpfun, clanker, reddit, farcaster, birdeye]


def enabled_sources():
    return [s for s in SOURCES if s.enabled()]


def collect_all():
    """Run every enabled source under its own guard. Returns {name: count}."""
    store.init()
    out = {}
    for src in enabled_sources():
        t0 = time.time()
        budget.arm(config.SIGNAL_SOURCE_BUDGET_SEC)
        try:
            n = src.collect()
            store.note_run(src.NAME, True, n)
            out[src.NAME] = n
        except Exception as e:
            store.note_run(src.NAME, False, 0, str(e))
            journal.log_event("signal_source_fail", detail=f"{src.NAME}: {str(e)[:120]}")
            out[src.NAME] = None
        journal.log_event("signal_collect", detail={"source": src.NAME, "new": out[src.NAME],
                                                    "sec": round(time.time() - t0, 1),
                                                    "cut": budget.expired()})
    budget.clear()
    store.prune()
    return out


def candidates(limit=None):
    """Discovery list for the research layer: assets with rising, broad
    attention, most interesting first. Each carries its feature dict."""
    return store.rising(limit or config.SIGNAL_CANDIDATES)


def features(asset_id):
    return store.features(asset_id)


def health_text():
    rows = store.source_health()
    if not rows:
        return "No signal sources have run yet."
    lines = ["SIGNALS — last run per source"]
    for r in rows:
        age = (time.time() - (r["last_ts"] or 0)) / 60
        st = "ok" if r["last_ok"] else f"FAIL {r['last_error'][:60]}"
        lines.append(f"  {r['source']:10s} {age:5.0f}m ago  +{r['last_count'] or 0:<4d} {st}")
    on = [s.NAME for s in enabled_sources()]
    off = [s.NAME for s in SOURCES if s not in enabled_sources()]
    lines.append(f"enabled: {', '.join(on) or 'none'}")
    if off:
        lines.append(f"off (no key or not configured): {', '.join(off)}")
    return "\n".join(lines)
