"""Regression tests for two correctness traps from the audit's open list.

- A DexScreener pair with no priceUsd came back as 0.0, which the monitor read
  as a price below every invalidation level and liquidated the position on.
- An explicit NO was forgotten by the next research cycle: the same asset came
  straight back for approval every 15 minutes.
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import approval, config, execution, marketdata, monitor, state  # noqa: E402
from tradebot.agent import runner  # noqa: E402
from tradebot import journal  # noqa: E402
from tradebot.exchanges import coinbase, solana_dex  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        state.init()
        for p in state.positions():
            state.close_position(p["asset_id"])
        self.patches = []

    def patch(self, mod, name, value):
        self.patches.append((mod, name, getattr(mod, name, None)))
        setattr(mod, name, value)

    def tearDown(self):
        for mod, name, old in reversed(self.patches):
            setattr(mod, name, old)


class ZeroPrice(Base):
    def test_zero_price_is_blind_not_a_price(self):
        marketdata._price_cache.clear()
        self.patch(marketdata, "dexscreener_token",
                   lambda chain, addr: {"price": 0.0, "liquidity_usd": 1.0})
        self.assertIsNone(marketdata.price("solana:MintA"))
        self.assertIsNone(marketdata.cached_price("solana:MintA"))
        marks, fresh = marketdata.marks(["solana:MintA"])
        self.assertEqual(marks, {})
        self.assertFalse(fresh)

    def test_monitor_does_not_sell_on_blind_read(self):
        state.upsert_position("solana:MintA", "solana", "solana", 100.0, 5.0)
        state.upsert_position("solana:MintA", "solana", "solana", 0, 0, invalidation=0.02)
        marketdata._price_cache.clear()
        self.patch(marketdata, "dexscreener_token", lambda chain, addr: {"price": 0.0})
        sold = []
        self.patch(execution, "execute_sell", lambda *a, **k: sold.append(a))
        monitor.check_positions()
        self.assertEqual(sold, [])
        self.assertIsNotNone(state.get_position("solana:MintA"))


class RejectCooldown(Base):
    def _ticket(self, asset="solana:MintB"):
        tid = state.add_ticket(asset_id=asset, venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=5.0)
        return [t for t in state.tickets("new") if t["ticket_id"] == tid][0]

    def test_no_holds_for_the_cooldown(self):
        self.patch(execution, "_run_gates", lambda t, v, f: 1.0)
        asked = []
        self.patch(approval, "request_buy_approval", lambda t, p, f: asked.append(t))
        # first sighting: asks
        t1 = self._ticket()
        self.assertEqual(execution.process_ticket(t1, 100.0, True), "awaiting_approval")
        self.assertEqual(len(asked), 1)
        # the user says NO
        state.add_pending("ABC123", "buy", "solana:MintB", t1["ticket_id"], 600)
        state.resolve_pending("ABC123", "rejected")
        self.assertIsNotNone(state.rejected_recently("solana:MintB"))
        # next cycle proposes it again: blocked, nobody is asked
        t2 = self._ticket()
        self.assertEqual(execution.process_ticket(t2, 100.0, True), "blocked")
        self.assertEqual(len(asked), 1)
        # cooldown over: asks again
        self.patch(config, "REJECT_COOLDOWN_SEC", 0)
        self.assertIsNone(state.rejected_recently("solana:MintB"))
        t3 = self._ticket()
        self.assertEqual(execution.process_ticket(t3, 100.0, True), "awaiting_approval")
        self.assertEqual(len(asked), 2)

    def test_expired_is_not_a_no(self):
        state.add_pending("DEF456", "buy", "solana:MintC", 1, 600)
        state.resolve_pending("DEF456", "expired")
        self.assertIsNone(state.rejected_recently("solana:MintC"))

    def test_discovery_skips_a_rejected_asset(self):
        state.add_pending("GHI789", "buy", "solana:MintD", 1, 600)
        state.resolve_pending("GHI789", "rejected")
        from tradebot import signals
        self.patch(signals, "collect_all", lambda: {})
        self.patch(signals, "candidates", lambda: [
            {"asset_id": "solana:MintD", "score": 9.0},
            {"asset_id": "solana:MintE", "score": 1.0}])
        self.patch(signals, "features", lambda a: {})
        looked = []

        def dex(chain, addr):
            looked.append(addr)
            return {"price": 1.0, "liquidity_usd": 1e6, "volume_h24": 1e5,
                    "base_symbol": "X", "created_ms": 0, "pair_address": "p"}
        self.patch(marketdata, "dexscreener_token", dex)
        self.patch(marketdata, "ohlcv_dex", lambda *a, **k: [])
        self.patch(marketdata, "coinbase_movers", lambda: [])
        self.patch(config, "PAID_PROMO_SOURCES", False)
        out = runner.gather()
        self.assertEqual(looked, ["MintE"])
        self.assertEqual([c["address"] for c in out], ["MintE"])


class StopoutCooldown(Base):
    def test_no_reentry_after_a_losing_exit(self):
        self.patch(execution, "_run_gates", lambda t, v, f: 1.0)
        asked = []
        self.patch(approval, "request_buy_approval", lambda t, p, f: asked.append(t))
        state.note_stopout("solana:MintS")
        tid = state.add_ticket(asset_id="solana:MintS", venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=5.0)
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.assertEqual(execution.process_ticket(t, 100.0, True), "blocked")
        self.assertEqual(asked, [])
        self.patch(config, "STOPOUT_COOLDOWN_SEC", 0)
        tid = state.add_ticket(asset_id="solana:MintS", venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=5.0)
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.assertEqual(execution.process_ticket(t, 100.0, True), "awaiting_approval")

    def test_losing_exit_records_the_stopout(self):
        state.upsert_position("cex:STP-USDC", "coinbase", None, 2.0, 10.0)
        state.whitelist_add("cex:STP-USDC", "coinbase")
        state.set_cash("coinbase", 100.0)
        from tradebot.exchanges import coinbase as cb
        cb._products["STP-USDC"] = {"quote_increment": "0.01", "base_increment": "0.00000001"}
        self.addCleanup(cb._products.clear)
        self.patch(marketdata, "price", lambda a: 3.0)
        self.patch(cb, "market_sell", lambda p, q: ("srv-s", {}))
        self.patch(cb, "order_status", lambda o: {
            "status": "FILLED", "filled_size": "2", "average_filled_price": "3",
            "total_fees": "0.02", "filled_value": "6", "order_id": "srv-s"})
        self.assertEqual(execution.execute_sell("cex:STP-USDC", "invalidation"), "filled")
        self.assertIsNotNone(state.stopped_out_recently("cex:STP-USDC"))
        self.assertFalse(state.is_whitelisted("cex:STP-USDC"))


class OrderSerialisation(Base):
    def test_concurrent_sells_place_one_order(self):
        """FLATTEN on the Telegram thread and the monitor's sell on the core
        thread hit the same position at once: exactly one swap is sent."""
        state.upsert_position("solana:MintF", "solana", "solana", 100.0, 5.0)
        marketdata._price_cache.clear()
        self.patch(marketdata, "price", lambda a: 0.05)
        sent, go = [], threading.Event()

        def swap(*a, **k):
            sent.append(a)
            go.wait(2)            # hold the lock so the second caller queues
            return "sig1", {}
        self.patch(solana_dex, "swap", swap)
        # balance: 100 tokens before the first sell, zero once it is done
        self.patch(solana_dex, "token_balance",
                   lambda m: (0, 6) if sent else (100_000_000, 6))
        cash = iter([0.0, 5.0, 5.0, 5.0])
        self.patch(solana_dex, "usdc_balance", lambda: next(cash, 5.0))
        self.patch(execution, "_await_solana", lambda sig: "ok")
        self.patch(execution, "_no_balance", lambda a, p: "no_balance")
        results = []
        ts = [threading.Thread(target=lambda: results.append(
            execution.execute_sell("solana:MintF", r))) for r in ("FLATTEN", "monitor")]
        for t in ts:
            t.start()
        time.sleep(0.2)
        go.set()
        for t in ts:
            t.join(5)
        self.assertEqual(len(sent), 1, results)
        self.assertIn("no_position", results)


class CoinbaseFees(Base):
    """The venue reports fees; a sell yields gross minus fee, a buy costs
    gross plus fee, and the fill row carries the fee."""

    def _order(self, filled, avg, fee, value):
        return {"status": "FILLED", "filled_size": str(filled),
                "average_filled_price": str(avg), "total_fees": str(fee),
                "filled_value": str(value), "order_id": "srv-9"}

    def test_sell_proceeds_are_net_of_fee(self):
        coinbase._products["FEE-USDC"] = {"quote_increment": "0.01",
                                          "base_increment": "0.00000001"}
        self.addCleanup(coinbase._products.clear)
        state.upsert_position("cex:FEE-USDC", "coinbase", None, 2.0, 4.0)
        state.set_cash("coinbase", 100.0)
        self.patch(marketdata, "price", lambda a: 3.0)
        self.patch(coinbase, "market_sell", lambda p, q: ("srv-9", {}))
        self.patch(coinbase, "order_status", lambda o: self._order(2.0, 3.0, 0.04, 6.0))
        self.assertEqual(execution.execute_sell("cex:FEE-USDC", "test"), "filled")
        self.assertAlmostEqual(state.cash("coinbase"), 105.96)
        row = journal.query("SELECT * FROM fills WHERE asset_id='cex:FEE-USDC' "
                            "AND side='sell' ORDER BY ts DESC LIMIT 1")[0]
        self.assertAlmostEqual(row["fee_usd"], 0.04)


if __name__ == "__main__":
    unittest.main()


class AutoMode(Base):
    """AUTO <hours>: a bounded standing YES. Confirmed with a code, expires,
    and changes nothing but gate 5."""

    def setUp(self):
        super().setUp()
        self.said = []
        self.patch(approval.alerts, "ops", self.said.append)
        self.patch(execution.alerts, "ops", self.said.append)
        state.set_auto_approve(0)
        state.set_kv("auto_ended_said", "")

    def _cmds(self):
        return approval.Commands(lambda p: None, lambda: None, lambda: "", lambda a: "",
                                 lambda a: "")

    def test_auto_needs_a_code_and_then_buys_without_a_tap(self):
        c = self._cmds()
        c.handle("AUTO 24")
        self.assertFalse(state.auto_approve_active())
        code = [w for w in self.said[-1].split() if len(w) == 6 and w.isupper()
                and w.isalnum()][-1]
        c.handle(f"YES {code}")
        self.assertTrue(state.auto_approve_active())
        self.patch(execution, "_run_gates", lambda t, v, f: 1.0)
        bought = []
        self.patch(execution, "execute_buy", lambda t, r: bought.append(t) or "filled")
        asked = []
        self.patch(approval, "request_buy_approval", lambda t, p, f: asked.append(t))
        tid = state.add_ticket(asset_id="solana:MintAuto", venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=5.0)
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.assertEqual(execution.process_ticket(t, 100.0, True), "filled")
        self.assertEqual(asked, [])
        self.assertTrue(state.is_whitelisted("solana:MintAuto"))   # same grant as a tap

    def test_auto_does_not_bypass_a_stopout_or_a_no(self):
        state.set_auto_approve(24)
        self.patch(execution, "_run_gates", lambda t, v, f: 1.0)
        bought = []
        self.patch(execution, "execute_buy", lambda t, r: bought.append(t) or "filled")
        state.note_stopout("solana:MintStopped")
        tid = state.add_ticket(asset_id="solana:MintStopped", venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=5.0)
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.assertEqual(execution.process_ticket(t, 100.0, True), "blocked")
        self.assertEqual(bought, [])

    def test_auto_off_and_expiry(self):
        c = self._cmds()
        state.set_auto_approve(24)
        c.handle("AUTO OFF")
        self.assertFalse(state.auto_approve_active())
        c.handle("AUTO 500")
        self.assertIn("must be", self.said[-1])
        from tradebot import core
        self.patch(core.alerts, "ops", self.said.append)
        state.set_kv("auto_until", str(time.time() - 1))    # just expired
        core.supervise_auto()
        core.supervise_auto()
        self.assertEqual(sum("expired" in s for s in self.said), 1)


class PnlTally(Base):
    def test_exit_logs_pnl_and_the_tally_sums_it(self):
        from tradebot import core, journal
        with journal._lock:
            journal.conn().execute("DELETE FROM events WHERE kind IN ('exit_pnl','agent_usage')")
            journal.conn().commit()
        coinbase._products["PNL-USDC"] = {"quote_increment": "0.01",
                                          "base_increment": "0.00000001"}
        self.addCleanup(coinbase._products.clear)
        state.upsert_position("cex:PNL-USDC", "coinbase", None, 2.0, 4.0)
        state.set_cash("coinbase", 100.0)
        self.patch(marketdata, "price", lambda a: 3.0)
        self.patch(coinbase, "market_sell", lambda p, q: ("srv-p", {}))
        self.patch(coinbase, "order_status", lambda o: {
            "status": "FILLED", "filled_size": "2", "average_filled_price": "3",
            "total_fees": "0.04", "filled_value": "6", "order_id": "srv-p"})
        execution.execute_sell("cex:PNL-USDC", "test")          # +1.96 realised
        journal.log_event("agent_usage", detail={"in": 1_000_000, "out": 0,
                                                 "cache_read": 0, "cache_write": 0})  # $5
        state.upsert_position("cex:OPEN-USDC", "coinbase", None, 1.0, 5.0)  # marks 3.0: -2
        txt = core.pnl_text("7")
        self.assertIn("Realised : $+1.96", txt)
        self.assertIn("Open     : $-2.00", txt)
        self.assertIn("API cost : $5.00", txt)
        self.assertIn("Net      : $-5.04", txt)
        state.close_position("cex:OPEN-USDC")
