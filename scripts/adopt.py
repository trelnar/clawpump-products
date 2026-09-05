#!/usr/bin/env python3
"""Bring a holding the wallet has but the books do not onto the books.

This is the orphan case: a swap landed but its confirmation was never read, so
the buy was recorded as failed and no position row exists. The tokens are
real; the bot's exits cannot see them. Adopting the position makes every exit
path -- invalidation, profit plan, FLATTEN, --sell-only -- work on it again.

    /opt/tradebot/venv/bin/python scripts/adopt.py base:0x4200...0006 --cost 5
    (quantity is read from the wallet unless --qty is given)
"""
import argparse
import os
import sys

sys.path.insert(0, "/opt/tradebot/bot")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: E402
_env.load()

from tradebot import journal, state  # noqa: E402
from tradebot.exchanges import evm_dex, solana_dex  # noqa: E402


def wallet_qty(asset):
    chain, addr = asset.split(":", 1)
    if chain == "solana":
        raw, dec = solana_dex.token_balance(addr)
    elif chain == "base":
        raw, dec = evm_dex.token_balance(addr)
    else:
        raise SystemExit("adopt handles solana:<mint> and base:<0xaddr> only")
    return raw / (10 ** dec) if dec else raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asset", help="solana:<mint> or base:<0xaddr>")
    ap.add_argument("--cost", type=float, required=True, help="USD actually spent")
    ap.add_argument("--qty", type=float, help="override the wallet reading")
    a = ap.parse_args()

    state.init()
    if state.get_position(a.asset):
        print("already on the books:", state.get_position(a.asset))
        return 1
    chain = a.asset.split(":", 1)[0]
    qty = a.qty if a.qty is not None else wallet_qty(a.asset)
    if qty <= 0:
        print("wallet reports no balance for", a.asset)
        return 1
    state.upsert_position(a.asset, chain, chain, qty, a.cost)
    journal.log_event("position_adopted", a.asset, {"qty": qty, "cost": a.cost})
    print(f"adopted {a.asset}: qty {qty:.10g}, cost ${a.cost:.2f}")
    print("exit it with:  scripts/roundtrip.py", chain, "--sell-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
