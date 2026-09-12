#!/usr/bin/env python3
"""Hyperliquid connectivity and account check. Read-only. Run as root or bot:

    sudo -u bot /opt/tradebot/venv/bin/python /opt/tradebot/scripts/hl_probe.py

Prints the bot's address, its Hyperliquid account value, open perp positions,
a few live mids, and today's short candidates. Prints no secrets.
"""
import os
import sys

sys.path.insert(0, "/opt/tradebot/bot")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _env  # noqa: E402
_env.load("/etc/tradebot/secrets.env")

from tradebot import config  # noqa: E402
from tradebot.exchanges import hyperliquid as hl  # noqa: E402


def main():
    print("address      :", hl.address())
    try:
        v = hl.account_value()
    except Exception as e:
        print("account      : FAIL", str(e)[:200])
        return 1
    print(f"account value: ${v:.2f}  (need >= ${config.PHASE1_ORDER_USD * config.HL_MARGIN_BUFFER:.2f} per short)")
    print("positions    :", hl.positions() or "none")
    m = hl.mids()
    print("mids         :", {k: m[k] for k in ("BTC", "ETH", "SOL") if k in m})
    print("universe     :", len(hl.universe()), "perps")
    config.SHORTS_ENABLED = True
    from tradebot import shorts
    cands = shorts.candidates()
    print("candidates   :", [(c["coin"], f"{c['chg24']:+.0%} 24h", f"{c['chg1h']:+.1%} 1h") for c in cands] or "none right now")
    print("\nOK. Set SHORTS_ENABLED=1 in secrets.env, run split-credentials.sh, restart core+agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
