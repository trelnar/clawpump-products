#!/usr/bin/env python3
"""Hit every signal source once and show what came back. Read-only.

The sources were written against documented response shapes without live
access; this is the check that each one actually parses what its endpoint
returns today. Run it before trusting the discovery list, and again after any
source starts logging signal_source_fail.

    /opt/tradebot/venv/bin/python /opt/tradebot/scripts/signals_probe.py
"""
import os
import sys
import time

sys.path.insert(0, "/opt/tradebot/bot")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: E402
_env.load("/etc/tradebot/agent.env" if os.path.exists("/etc/tradebot/agent.env")
          else "/etc/tradebot/secrets.env")

from tradebot import journal, signals  # noqa: E402
from tradebot.signals import budget, store  # noqa: E402


def main():
    store.init()
    on = signals.enabled_sources()
    print("enabled :", ", ".join(s.NAME for s in on) or "none")
    print("off     :", ", ".join(s.NAME for s in signals.SOURCES if s not in on) or "none")
    print()
    bad = 0
    for src in on:
        t0 = time.time()
        budget.arm(120)          # generous here; the agent uses SIGNAL_SOURCE_BUDGET_SEC
        try:
            n = src.collect()
            print(f"  {src.NAME:10s} ok    +{n:<4d} new events   {time.time() - t0:4.1f}s")
            if n == 0:
                print(f"  {'':10s}       zero: either quiet, or the parser no longer matches "
                      f"the response -- check events for signal_source_fail")
        except Exception as e:
            bad += 1
            print(f"  {src.NAME:10s} FAIL  {str(e)[:120]}")
    budget.clear()
    print()
    # Per source and kind, last hour. A kind a source is supposed to produce
    # sitting at zero on a busy day is the parser-drift symptom to look for:
    # pumpfun should show launch, graduation AND trending; gecko trending AND
    # new_pool; reddit post.
    rows = journal.query(
        "SELECT source, kind, COUNT(*) n FROM signal_events WHERE ts > ? "
        "GROUP BY source, kind ORDER BY source, kind", (time.time() - 3600,))
    if rows:
        print("events in the last hour, by source and kind:")
        for r in rows:
            print(f"  {r['source']:12s} {r['kind']:14s} {r['n']}")
    expected = {"pumpfun": {"launch", "graduation"},
                "gecko": {"trending", "new_pool"}, "reddit": {"post"},
                "clanker": {"launch"}}
    got = {}
    for r in rows:
        got.setdefault(r["source"], set()).add(r["kind"])
    for src in on:
        missing = expected.get(src.NAME, set()) - got.get(src.NAME, set())
        if missing:
            print(f"  ! {src.NAME}: no {', '.join(sorted(missing))} events -- "
                  f"quiet hour, or the parser no longer matches")
    print()
    top = store.rising(limit=8)
    if top:
        print("rising now (accel x breadth):")
        for r in top:
            print(f"  {r['score']:6.1f}  {r['asset_id'][:52]:52s} accel {r['accel']:<5} "
                  f"breadth {r['breadth']} kinds {','.join(r['kinds'])}")
    else:
        print("nothing rising yet -- run again in 15 minutes")
    print()
    print("ALL SOURCES ANSWERED" if not bad else f"{bad} SOURCE(S) FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
