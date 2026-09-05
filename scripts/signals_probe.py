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

from tradebot import signals  # noqa: E402
from tradebot.signals import store  # noqa: E402


def main():
    store.init()
    on = signals.enabled_sources()
    print("enabled :", ", ".join(s.NAME for s in on) or "none")
    print("off     :", ", ".join(s.NAME for s in signals.SOURCES if s not in on) or "none")
    print()
    bad = 0
    for src in on:
        t0 = time.time()
        try:
            n = src.collect()
            print(f"  {src.NAME:10s} ok    +{n:<4d} new events   {time.time() - t0:4.1f}s")
            if n == 0:
                print(f"  {'':10s}       zero: either quiet, or the parser no longer matches "
                      f"the response -- check events for signal_source_fail")
        except Exception as e:
            bad += 1
            print(f"  {src.NAME:10s} FAIL  {str(e)[:120]}")
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
