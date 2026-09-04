#!/usr/bin/env python3
"""Check both DEX aggregators answer, and that their responses still look the
way the code expects. Read-only: quotes only, nothing is signed or sent.

Jupiter retired quote-api.jup.ag without notice and every Solana swap died at
DNS resolution. This is the cheap check to run before a round trip, and after
any dependency or endpoint change.

    /opt/tradebot/venv/bin/python /opt/tradebot/scripts/dex_probe.py
"""
import os
import sys

sys.path.insert(0, "/opt/tradebot/bot")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: E402
_env.load()

from tradebot import config  # noqa: E402
from tradebot.exchanges import evm_dex, solana_dex  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
WETH_BASE = "0x4200000000000000000000000000000000000006"
FIVE_USDC = 5_000_000


def main():
    bad = 0

    print("JUPITER (Solana)")
    print("  bases configured:", ", ".join(config.JUPITER_BASES))
    try:
        q = solana_dex.quote(solana_dex.USDC_MINT, WSOL, FIVE_USDC, 300)
        base = solana_dex._jup_base
        out = int(q["outAmount"])
        print(f"  answering base : {base}")
        print(f"  $5 USDC -> {out / 1e9:.6f} wSOL   (outAmount present, parses as int)")
        missing = [k for k in ("inAmount", "outAmount", "routePlan") if k not in q]
        print("  missing fields :", missing or "none")
        bad += bool(missing)
    except Exception as e:
        print("  FAILED:", str(e)[:200]); bad += 1

    print("\nKYBERSWAP (Base)")
    try:
        rt = evm_dex.route(evm_dex.USDC, WETH_BASE, FIVE_USDC)
        rs = rt["routeSummary"]
        print(f"  $5 USDC -> {int(rs['amountOut']) / 1e18:.8f} WETH")
        router = rt.get("routerAddress")
        allowed = {a.lower() for a in config.EVM_ROUTER_ALLOWLIST}
        ok = router and router.lower() in allowed
        print(f"  router         : {router}  {'ALLOWLISTED' if ok else 'NOT IN ALLOWLIST'}")
        bad += (not ok)
    except Exception as e:
        print("  FAILED:", str(e)[:200]); bad += 1

    print("\nGAS")
    for chain, fn in (("solana", solana_dex.sol_balance), ("base", evm_dex.eth_balance)):
        try:
            held = fn()
            floor = config.GAS_COST_PER_EXIT[chain] * config.GAS_EXITS_FLOOR
            state = "ok" if held >= floor else "BELOW FLOOR"
            print(f"  {chain:7s}: {held:.6f} {config.CHAIN_GAS_TOKEN[chain]} "
                  f"(floor {floor:.6f}) {state}")
            bad += (held < floor)
        except Exception as e:
            print(f"  {chain:7s}: unreadable — {str(e)[:120]}"); bad += 1

    print("\n" + ("ALL CHECKS PASSED — safe to run the round trips."
                  if not bad else f"{bad} PROBLEM(S) — fix before spending money."))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
