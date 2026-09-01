"""Regression tests for the three critical execution/kill-switch defects:

C1  Solana positions were booked in raw base units, not whole tokens.
C2  Buys were booked as filled with no confirmation, on all three venues.
C3  One malformed Telegram update killed the listener -- and the kill switch.

Run: TRADEBOT_DB=/tmp/t.db python3 -m unittest discover -s bot/tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import config, execution, state, telegram  # noqa: E402
from tradebot.exchanges import coinbase, evm_dex, solana_dex  # noqa: E402


def ticket(asset, venue, chain=None, usd=5.0):
    return {"ticket_id": f"t-{asset}", "asset_id": asset, "venue": venue,
            "chain": chain, "notional_usd": usd, "ts": 0,
            "invalidation_price": None}


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


class SanityQty(Base):
    def test_raw_base_units_are_rejected(self):
        # $5 of a $0.001 token with 6 decimals: 5,000 whole -> 5e9 raw.
        with self.assertRaises(RuntimeError):
            execution._sanity_qty(5_000_000_000, 0.001, 5.0)

    def test_whole_units_pass(self):
        execution._sanity_qty(5_000, 0.001, 5.0)

    def test_zero_fill_is_rejected(self):
        with self.assertRaises(RuntimeError):
            execution._sanity_qty(0, 0.001, 5.0)


class SolanaBuy(Base):
    def _stub(self, before, after, decimals, confirm="finalized"):
        self.patch(solana_dex, "token_decimals", lambda m: decimals)
        seq = iter([(before, decimals), (after, decimals)])
        self.patch(solana_dex, "token_balance", lambda m: next(seq))
        self.patch(solana_dex, "swap", lambda *a, **k: ("sig1", {"outAmount": str(after)}))
        self.patch(solana_dex, "confirm", lambda s: confirm)

    def test_books_whole_tokens_not_raw(self):
        self._stub(0, 5_000_000_000, 6)
        self.patch(execution.marketdata, "price", lambda a: 0.001)
        r = execution.execute_buy(ticket("solana:MINT1", "solana", "solana"), 0.001)
        self.assertEqual(r, "filled")
        pos = state.get_position("solana:MINT1")
        self.assertAlmostEqual(pos["qty"], 5000.0)
        self.assertAlmostEqual(pos["cost_basis_usd"], 5.0)

    def test_unit_mismatch_books_the_fill_then_freezes(self):
        # decimals misreported as 0: 5e9 raw units booked against a $5 order.
        self._stub(0, 5_000_000_000, 0)
        r = execution.execute_buy(ticket("solana:MINT3", "solana", "solana"), 0.001)
        self.assertEqual(r, "sanity_freeze")
        self.assertIsNotNone(state.get_position("solana:MINT3"))
        self.assertEqual(state.get_mode(), "RECON_FREEZE")
        state.set_mode("NORMAL", reason="test cleanup")

    def test_unconfirmed_swap_books_nothing(self):
        self._stub(0, 5_000_000_000, 6, confirm="unknown")
        self.patch(config, "FILL_TIMEOUT_SOL_SEC", 0)
        r = execution.execute_buy(ticket("solana:MINT2", "solana", "solana"), 0.001)
        self.assertEqual(r, "failed")
        self.assertIsNone(state.get_position("solana:MINT2"))


class BaseChainBuy(Base):
    def test_pending_tx_is_not_a_fill(self):
        self.patch(evm_dex, "token_balance", lambda t: (0, 18))
        self.patch(evm_dex, "swap", lambda *a, **k: "0xdead")
        self.patch(evm_dex, "confirm", lambda h: "unknown")  # still pending
        self.patch(config, "FILL_TIMEOUT_EVM_SEC", 0)
        r = execution.execute_buy(ticket("base:0xTOK", "base", "base"), 5.0)
        self.assertEqual(r, "failed")
        self.assertIsNone(state.get_position("base:0xTOK"))

    def test_confirmed_tx_books_the_balance_delta(self):
        seq = iter([(0, 18), (2 * 10 ** 18, 18)])
        self.patch(evm_dex, "token_balance", lambda t: next(seq))
        self.patch(evm_dex, "swap", lambda *a, **k: "0xbeef")
        self.patch(evm_dex, "confirm", lambda h: "confirmed")
        r = execution.execute_buy(ticket("base:0xTOK2", "base", "base"), 5.0)
        self.assertEqual(r, "filled")
        self.assertAlmostEqual(state.get_position("base:0xTOK2")["qty"], 2.0)


class CoinbaseBuy(Base):
    def _order(self, status, filled, avg, fee=0.0):
        return {"status": status, "filled_size": str(filled),
                "average_filled_price": str(avg), "total_fees": str(fee),
                "order_id": "srv-1"}

    def test_books_the_actual_fill_not_the_intent(self):
        self.patch(coinbase, "best_price", lambda p: (1.49, 1.5))
        self.patch(coinbase, "limit_buy", lambda p, n, l: ("tb-1", {}))
        self.patch(coinbase, "order_status", lambda o: self._order("FILLED", 3, 1.5, 0.05))
        r = execution.execute_buy(ticket("cex:AAA-USD", "coinbase", None, 5.0), 1.5)
        self.assertEqual(r, "filled")
        pos = state.get_position("cex:AAA-USD")
        self.assertAlmostEqual(pos["qty"], 3.0)
        self.assertAlmostEqual(pos["cost_basis_usd"], 4.55)

    def test_rejected_order_books_nothing_and_cancels(self):
        cancelled = []
        self.patch(coinbase, "best_price", lambda p: (1.49, 1.5))
        self.patch(coinbase, "limit_buy", lambda p, n, l: ("tb-2", {}))
        self.patch(coinbase, "order_status", lambda o: self._order("REJECTED", 0, 0))
        self.patch(coinbase, "cancel", lambda oid: cancelled.append(oid))
        r = execution.execute_buy(ticket("cex:BBB-USD", "coinbase", None, 5.0), 1.5)
        self.assertEqual(r, "failed")
        self.assertIsNone(state.get_position("cex:BBB-USD"))
        self.assertEqual(cancelled, ["srv-1"])


class TelegramPoller(Base):
    def setUp(self):
        super().setUp()
        self.seen = []
        self.patch(telegram, "_call", lambda m, **k: None)
        self.p = telegram.Poller(self.seen.append)

    def test_update_without_sender_does_not_raise(self):
        self.p._dispatch({"callback_query": {"id": "1", "data": "YES A1"}})
        self.p._dispatch({"message": {"text": "STOP"}})
        self.assertEqual(self.seen, [])

    def test_authorized_command_reaches_the_handler(self):
        self.patch(config, "TELEGRAM_USER_ID", 42)
        self.p._dispatch({"message": {"from": {"id": 42}, "text": "STOP"}})
        self.assertEqual(self.seen, ["STOP"])

    def test_unauthorized_sender_is_ignored(self):
        self.patch(config, "TELEGRAM_USER_ID", 42)
        self.p._dispatch({"message": {"from": {"id": 99}, "text": "FLATTEN"}})
        self.assertEqual(self.seen, [])

    def test_offset_survives_a_restart(self):
        self.p._commit_offset(1234)
        self.assertEqual(telegram.Poller(self.seen.append).offset, 1234)

    def test_stale_poll_is_unhealthy(self):
        self.p.last_ok = 0
        self.assertFalse(self.p.healthy())


if __name__ == "__main__":
    unittest.main()
