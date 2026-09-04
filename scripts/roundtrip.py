#!/usr/bin/env python3
"""Commissioning test: one real buy and sell per venue, smallest viable size.

Why this exists: execute_sell has never sold anything, anywhere. Every safety
mechanism in the system -- the invalidation stop, the standing profit plan, the
liquidity-drain exit, agent SELL_NOW and the owner's FLATTEN -- terminates in
that one function. Gate 4's exit-safety check is a QUOTE, and quotes are
systematically optimistic for exactly the tokens this bot hunts. Only contact
with a real venue answers whether a sell clears.

It deliberately trades a large, liquid asset rather than a microcap: the point
is to exercise the code path with something you can always get out of.

Usage, on the VPS as the bot user:
    python3 -m scripts.roundtrip coinbase [--product BTC-USD] [--usd 5]
    python3 -m scripts.roundtrip solana   [--usd 5]
    python3 -m scripts.roundtrip base     [--usd 5]
    python3 -m scripts.roundtrip <venue> --sell-only   # if a leg was interrupted

Run one venue at a time and read the output before the next.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, "/opt/tradebot/bot")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: E402
_env.load()          # systemd gives the daemons their env; a script must load its own

from tradebot import config, execution, journal, marketdata, state  # noqa: E402
from tradebot.exchanges import evm_dex, solana_dex  # noqa: E402

# Liquid, boring, always exitable. Not what the bot hunts -- that is the point.
TARGETS = {
    "coinbase": {"asset": "cex:BTC-USDC", "venue": "coinbase", "chain": None},
    "solana": {"asset": "solana:So11111111111111111111111111111111111111112",
               "venue": "solana", "chain": "solana"},          # wrapped SOL
    "base": {"asset": "base:0x4200000000000000000000000000000000000006",
             "venue": "base", "chain": "base"},                 # WETH on Base
}


def show(label, **kw):
    print(f"\n=== {label} ===")
    for k, v in kw.items():
        print(f"  {k:24s} {v}")


def venue_truth(t):
    """What the venue says we hold, independent of our own books."""
    try:
        if t["chain"] == "solana":
            raw, dec = solana_dex.token_balance(t["asset"].split(":", 1)[1])
            return raw / (10 ** dec) if dec else raw
        if t["chain"] == "base":
            raw, dec = evm_dex.token_balance(t["asset"].split(":", 1)[1])
            return raw / (10 ** dec) if dec else raw
        from tradebot.exchanges import coinbase
        product = t["asset"].split(":", 1)[1].rsplit("-", 1)[0]
        for a in coinbase._to_dict(coinbase.client().get_accounts(limit=250)).get("accounts", []):
            if a.get("currency") == product:
                return float((a.get("available_balance") or {}).get("value") or 0)
        return 0.0
    except Exception as e:
        return f"<unreadable: {e}>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("venue", choices=sorted(TARGETS))
    ap.add_argument("--usd", type=float, default=config.PHASE1_ORDER_USD)
    ap.add_argument("--product", help="override the Coinbase product, e.g. ETH-USD")
    ap.add_argument("--sell-only", action="store_true",
                    help="skip the buy and exit a position this script already opened")
    a = ap.parse_args()

    state.init()
    t = dict(TARGETS[a.venue])
    if a.product and a.venue == "coinbase":
        t["asset"] = f"cex:{a.product}"
    asset = t["asset"]

    if state.get_mode() != "NORMAL":
        print(f"Mode is {state.get_mode()}. This script uses the live gates; "
              f"RESUME first or it will be blocked.")
        return 2

    print(f"\nROUND TRIP  {a.venue}  {asset}  ${a.usd:.2f}")
    print("This spends real money. Ctrl-C within 5s to abort.")
    time.sleep(5)

    if not a.sell_only:
        before_cash = state.cash(a.venue)
        before_venue = venue_truth(t)
        show("before buy", books_cash=f"${before_cash:.2f}", venue_holds=before_venue,
             mark=marketdata.price(asset))

        ticket = {"ticket_id": state.add_ticket(
            asset_id=asset, venue=t["venue"], chain=t["chain"], action="BUY_NOW",
            notional_usd=a.usd, detail="roundtrip commissioning test"),
            "asset_id": asset, "venue": t["venue"], "chain": t["chain"],
            "notional_usd": a.usd, "ts": time.time(), "invalidation_price": None}
        # Whitelisted so gate 5 passes without a Telegram round trip.
        state.whitelist_add(asset, t["chain"] or t["venue"])
        result = execution.execute_buy(ticket, marketdata.price(asset))
        pos = state.get_position(asset)
        show("after buy", result=result,
             books_qty=pos and pos["qty"], books_cost=pos and pos["cost_basis_usd"],
             venue_holds=venue_truth(t), books_cash=f"${state.cash(a.venue):.2f}")
        if result not in ("filled", "sanity_freeze") or not pos:
            print("\nBUY DID NOT BOOK. Check the venue directly before doing anything "
                  "else -- money may have moved without a position row.")
            return 1
        print("\nCompare books_qty against venue_holds. They should agree.")
        time.sleep(5)

    pos = state.get_position(asset)
    if not pos:
        print("No position to sell.")
        return 1
    before_cash = state.cash(a.venue)
    result = execution.execute_sell(asset, "roundtrip commissioning test", 1.0)
    show("after sell", result=result,
         books_cash=f"${state.cash(a.venue):.2f}", cash_delta=f"${state.cash(a.venue)-before_cash:+.2f}",
         venue_holds=venue_truth(t), position=state.get_position(asset))

    state.whitelist_revoke(asset)   # do not leave the test asset authorised
    if result != "filled":
        print("\nSELL FAILED. This is the finding the test exists to produce. "
              "The position is still on the books; exit it manually.")
        return 1
    print("\nRound trip complete. The sell path works on this venue.")
    print(f"Cost of the test: roughly the round-trip spread plus fees on ${a.usd:.2f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
