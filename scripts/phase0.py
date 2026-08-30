#!/usr/bin/env python3
"""go-live Phase 0 wiring checklist. Run as the bot user on the VPS.
Exits 0 only when every check passes."""
import os
import sys

sys.path.insert(0, "/opt/tradebot/bot")
os.environ.setdefault("TRADEBOT_DB", "/var/lib/tradebot/tradebot.db")

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or "ok"))
    except Exception as e:
        RESULTS.append((name, False, str(e)[:200]))


def main():
    from tradebot import config, heartbeat, journal, state, telegram
    from tradebot.exchanges import coinbase, evm_dex, solana_dex

    state.init()

    def secrets_present():
        missing = [n for n in ("TELEGRAM_TOKEN", "TELEGRAM_USER_ID", "HEALTHCHECK_URL",
                               "COINBASE_API_KEY", "COINBASE_API_SECRET", "ANTHROPIC_API_KEY")
                   if not os.environ.get(n)]
        if missing:
            raise RuntimeError("missing: " + ", ".join(missing))
        return "all set"

    def must(ok, msg):
        if not ok:
            raise RuntimeError(msg)
        return "ok"

    check("secrets present", secrets_present)
    check("telegram send", lambda: must(telegram.send("Phase 0 wiring test"), "send failed"))
    check("heartbeat ping", lambda: must(heartbeat.ping(), "ping failed"))
    check("coinbase auth (trade key, HypeBot)", lambda: f"USDC ${coinbase.usdc_balance():.2f}")
    check("solana wallet", lambda: f"{solana_dex.address()} SOL {solana_dex.sol_balance():.4f} "
                                   f"USDC {solana_dex.usdc_balance():.2f}")
    check("evm wallet", lambda: f"{evm_dex.address()} ETH {evm_dex.eth_balance():.5f} "
                                f"USDC {evm_dex.usdc_balance():.2f}")
    check("db writable", lambda: journal.log_event("phase0_run") and "ok")
    check("mode is not NORMAL yet",
          lambda: must(state.get_mode() != "NORMAL",
                       "mode already NORMAL before Phase 0 done") and state.get_mode())

    ok = all(r[1] for r in RESULTS)
    for name, passed, detail in RESULTS:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")
    print("\nPHASE 0:", "ALL CHECKS PASS" if ok else "FAILURES ABOVE")
    if ok:
        print("Remaining manual items: STOP/RESUME drill, FLATTEN drill, "
              "48h clean reconciliation. Then reply YES to the phase-advance code.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
