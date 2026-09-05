"""Regression tests for the three critical execution/kill-switch defects:

C1  Solana positions were booked in raw base units, not whole tokens.
C2  Buys were booked as filled with no confirmation, on all three venues.
C3  One malformed Telegram update killed the listener -- and the kill switch.

Run: TRADEBOT_DB=/tmp/t.db python3 -m unittest discover -s bot/tests
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import (approval, config, execution, monitor,  # noqa: E402
                      state, telegram)
from tradebot import calibration, monitor as _mon  # noqa: E402,F401
from tradebot.agent import runner  # noqa: E402
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
        self.patch(execution, "_entry_liquidity", lambda a, c: 50_000.0)
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

    def test_a_lagging_balance_read_is_retried_not_called_zero(self):
        # A just-confirmed swap is not yet visible; the first reads see nothing.
        self.patch(execution, "_entry_liquidity", lambda a, c: 50_000.0)
        self.patch(config, "SETTLE_READ_SLEEP_SEC", 0)
        self.patch(solana_dex, "token_decimals", lambda m: 6)
        reads = iter([(0, 6), (0, 6), (5_000_000_000, 6)])
        self.patch(solana_dex, "token_balance", lambda m: next(reads))
        self.patch(solana_dex, "swap", lambda *a, **k: ("sigL", {"outAmount": "5000000000"}))
        self.patch(solana_dex, "confirm", lambda s: "confirmed")
        self.patch(execution.marketdata, "price", lambda a: 0.001)
        r = execution.execute_buy(ticket("solana:LAG", "solana", "solana"), 0.001)
        self.assertEqual(r, "filled")
        self.assertAlmostEqual(state.get_position("solana:LAG")["qty"], 5000.0)

    def test_an_unreadable_balance_books_from_the_quote_and_freezes(self):
        # The money is spent and the tokens are ours -- never report NOT BOUGHT.
        self.patch(execution, "_entry_liquidity", lambda a, c: 50_000.0)
        self.patch(config, "SETTLE_READ_SLEEP_SEC", 0)
        self.patch(solana_dex, "token_decimals", lambda m: 6)
        calls = []

        def flaky(mint):
            calls.append(1)
            if len(calls) == 1:
                return (0, 6)          # pre-swap baseline reads fine
            raise RuntimeError("rpc 429")   # every post-swap read fails

        self.patch(solana_dex, "token_balance", flaky)
        self.patch(solana_dex, "swap", lambda *a, **k: ("sigX", {"outAmount": "5000000000"}))
        self.patch(solana_dex, "confirm", lambda s: "confirmed")
        self.patch(execution.marketdata, "price", lambda a: 0.001)
        self.addCleanup(state.set_mode, "NORMAL", "test cleanup")
        r = execution.execute_buy(ticket("solana:RPCX", "solana", "solana"), 0.001)
        self.assertEqual(r, "sanity_freeze")
        self.assertAlmostEqual(state.get_position("solana:RPCX")["qty"], 5000.0)
        self.assertEqual(state.get_mode(), "RECON_FREEZE")

    def test_unconfirmed_swap_books_nothing(self):
        self._stub(0, 5_000_000_000, 6, confirm="unknown")
        self.patch(config, "FILL_TIMEOUT_SOL_SEC", 0)
        r = execution.execute_buy(ticket("solana:MINT2", "solana", "solana"), 0.001)
        self.assertEqual(r, "failed")
        self.assertIsNone(state.get_position("solana:MINT2"))


class BaseChainBuy(Base):
    def setUp(self):
        super().setUp()
        self.patch(execution, "_entry_liquidity", lambda a, c: 50_000.0)

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


class QuoteCurrency(Base):
    """A BTC-USD order cannot spend USDC. The first real order was refused with
    INSUFFICIENT_FUND while the books showed $275 of cash -- all of it USDC."""

    def test_the_quote_currency_comes_from_the_product(self):
        self.assertEqual(execution.asset_quote_currency("cex:BTC-USDC"), "USDC")
        self.assertEqual(execution.asset_quote_currency("cex:BTC-USD"), "USD")
        self.assertEqual(execution.asset_quote_currency("cex:1INCH-USDC"), "USDC")

    def test_a_buy_in_a_currency_we_do_not_hold_is_blocked(self):
        self.patch(coinbase, "balances", lambda: {"USDC": 275.0, "USD": 0.0})
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.risk, "check_buy", lambda *a, **k: None)
        state.set_mode("NORMAL", reason="test")
        t = ticket("cex:BTC-USD", "coinbase")
        t["ts"] = time.time()
        with self.assertRaises(execution.risk.Reject) as cm:
            execution._gates_buy(t, 1000.0, True)
        self.assertEqual(cm.exception.rule, "wrong_quote_currency")

    def test_a_buy_in_the_currency_we_hold_passes(self):
        self.patch(coinbase, "balances", lambda: {"USDC": 275.0})
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.risk, "check_buy", lambda *a, **k: None)
        state.set_mode("NORMAL", reason="test")
        t = ticket("cex:BTC-USDC", "coinbase")
        t["ts"] = time.time()
        self.assertEqual(execution._gates_buy(t, 1000.0, True), 1.0)

    def test_usdc_balance_still_totals_spendable_cash(self):
        self.patch(coinbase, "balances", lambda: {"USDC": 200.0, "USD": 75.0, "BTC": 3.0})
        self.assertAlmostEqual(coinbase.usdc_balance(), 275.0)


class VenuePrecision(Base):
    """The first real Coinbase order was rejected with INVALID_PRICE_PRECISION:
    a limit price formatted with %.8g gave BTC-USD three decimals against a
    0.01 tick. Price and size must be quantised to the product's increments."""

    def setUp(self):
        super().setUp()
        coinbase._products["BTC-USD"] = {
            "quote_increment": "0.01", "base_increment": "0.00000001",
            "base_min_size": "0.00000001", "quote_min_size": "1"}
        coinbase._products["COARSE-USD"] = {
            "quote_increment": "0.5", "base_increment": "0.1",
            "base_min_size": "1", "quote_min_size": "10"}
        self.addCleanup(coinbase._products.clear)

    def test_a_buy_price_rounds_up_to_the_tick(self):
        self.assertEqual(coinbase.fmt_price("BTC-USD", 79880.6153), "79880.62")

    def test_a_price_already_on_the_tick_is_unchanged(self):
        self.assertEqual(coinbase.fmt_price("BTC-USD", 79880.62), "79880.62")

    def test_size_rounds_down_never_up(self):
        self.assertEqual(coinbase.fmt_size("COARSE-USD", 3.99), "3.9")

    def test_a_coarse_tick_is_respected(self):
        self.assertEqual(coinbase.fmt_price("COARSE-USD", 100.2), "100.5")

    def test_no_float_artefacts_in_the_output(self):
        for v in (0.1 + 0.2, 1e-8, 123456.785):
            self.assertNotIn("e", coinbase.fmt_price("BTC-USD", v).lower())

    def test_a_sub_minimum_order_is_refused_before_it_is_sent(self):
        with self.assertRaises(RuntimeError):
            coinbase.check_min_size("COARSE-USD", "0.5", 50.0)     # below base min
        with self.assertRaises(RuntimeError):
            coinbase.check_min_size("COARSE-USD", "5", 5.0)        # below quote min

    def test_a_valid_order_passes(self):
        coinbase.check_min_size("BTC-USD", "0.00006", 5.0)


class PlacementIdentity(Base):
    """list_orders has no client_order_id filter, so the only handle on an order
    we placed is the order_id the venue returns at placement."""

    def test_top_level_order_id_is_used(self):
        oid, _ = coinbase._placed("tb-1", {"success": True, "order_id": "srv-9"})
        self.assertEqual(oid, "srv-9")

    def test_nested_success_response_order_id_is_used(self):
        oid, _ = coinbase._placed(
            "tb-1", {"success": True, "success_response": {"order_id": "srv-10"}})
        self.assertEqual(oid, "srv-10")

    def test_rejected_placement_raises(self):
        with self.assertRaises(RuntimeError):
            coinbase._placed("tb-1", {"success": False,
                                      "error_response": {"error": "INSUFFICIENT_FUND"}})

    def test_placement_without_an_order_id_raises(self):
        # Never fall back to a client id the API cannot query.
        with self.assertRaises(RuntimeError):
            coinbase._placed("tb-1", {"success": True})


class CoinbaseBuy(Base):
    def _order(self, status, filled, avg, fee=0.0, value=None):
        o = {"status": status, "filled_size": str(filled),
             "average_filled_price": str(avg), "total_fees": str(fee),
             "order_id": "srv-1", "client_order_id": "tb-1"}
        if value is not None:
            o["filled_value"] = str(value)
        return o

    def setUp(self):
        super().setUp()
        coinbase._products["AAA-USD"] = coinbase._products["BBB-USD"] = \
            coinbase._products["VVV-USD"] = coinbase._products["IDD-USD"] = {
                "quote_increment": "0.01", "base_increment": "0.00000001"}
        self.addCleanup(coinbase._products.clear)

    def test_books_the_actual_fill_not_the_intent(self):
        self.patch(coinbase, "best_price", lambda p: (1.49, 1.5))
        self.patch(coinbase, "limit_buy", lambda p, n, l: ("srv-1", {}))
        self.patch(coinbase, "order_status", lambda o: self._order("FILLED", 3, 1.5, 0.05))
        r = execution.execute_buy(ticket("cex:AAA-USD", "coinbase", None, 5.0), 1.5)
        self.assertEqual(r, "filled")
        pos = state.get_position("cex:AAA-USD")
        self.assertAlmostEqual(pos["qty"], 3.0)
        self.assertAlmostEqual(pos["cost_basis_usd"], 4.55)

    def test_filled_value_beats_our_own_arithmetic(self):
        # A multi-fill order's average loses the price mix; the venue's total does not.
        self.patch(coinbase, "best_price", lambda p: (1.49, 1.5))
        self.patch(coinbase, "limit_buy", lambda p, n, l: ("srv-1", {}))
        self.patch(coinbase, "order_status",
                   lambda o: self._order("FILLED", 3, 1.5, 0.05, value=4.62))
        execution.execute_buy(ticket("cex:VVV-USD", "coinbase", None, 5.0), 1.5)
        self.assertAlmostEqual(state.get_position("cex:VVV-USD")["cost_basis_usd"], 4.67)

    def test_the_order_polled_is_the_order_placed(self):
        seen = []
        self.patch(coinbase, "best_price", lambda p: (1.49, 1.5))
        self.patch(coinbase, "limit_buy", lambda p, n, l: ("srv-42", {}))
        self.patch(coinbase, "order_status",
                   lambda o: seen.append(o) or self._order("FILLED", 3, 1.5))
        execution.execute_buy(ticket("cex:IDD-USD", "coinbase", None, 5.0), 1.5)
        self.assertEqual(set(seen), {"srv-42"})

    def test_rejected_order_books_nothing_and_cancels_that_order(self):
        cancelled = []
        self.patch(coinbase, "best_price", lambda p: (1.49, 1.5))
        self.patch(coinbase, "limit_buy", lambda p, n, l: ("srv-7", {}))
        self.patch(coinbase, "order_status", lambda o: self._order("REJECTED", 0, 0))
        self.patch(coinbase, "cancel", lambda oid: cancelled.append(oid))
        r = execution.execute_buy(ticket("cex:BBB-USD", "coinbase", None, 5.0), 5.0)
        self.assertEqual(r, "failed")
        self.assertIsNone(state.get_position("cex:BBB-USD"))
        self.assertEqual(cancelled, ["srv-7"])


class ApprovedBuyGates(Base):
    """A tapped YES satisfies gate 5 only -- it must not walk past the others."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.patch(execution, "execute_buy",
                   lambda t, ref: self.calls.append(t["asset_id"]) or "filled")
        self.addCleanup(state.set_mode, "NORMAL", "test cleanup")

    def test_a_clean_approved_buy_still_executes(self):
        # Positive control: the two blocks below must be the gates, not the setup.
        state.set_mode("NORMAL", reason="test")
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.risk, "check_buy", lambda *a, **k: None)
        self.patch(coinbase, "balances", lambda: {"USD": 1000.0})
        t = ticket("cex:GGG-USD", "coinbase")
        t["ts"] = time.time()
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "filled")
        self.assertEqual(self.calls, ["cex:GGG-USD"])

    def test_halt_blocks_an_approved_buy(self):
        self.patch(coinbase, "balances", lambda: {"USD": 1000.0})
        state.set_mode("USER_STOP", reason="test")
        r = execution.execute_approved(ticket("cex:DDD-USD", "coinbase"), 1000.0, True)
        self.assertEqual(r, "blocked")
        self.assertEqual(self.calls, [])

    def test_stale_ticket_blocks_an_approved_buy(self):
        state.set_mode("NORMAL", reason="test")
        t = ticket("cex:EEE-USD", "coinbase")
        t["ts"] = 0  # older than TICKET_MAX_AGE_SEC
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "blocked")
        self.assertEqual(self.calls, [])


class ApprovalCodes(Base):
    def setUp(self):
        super().setUp()
        self.approved = []
        self.cmds = approval.Commands(
            self.approved.append, lambda *a: None,
            lambda: "status", lambda a=None: "report", lambda a: "why")
        self.patch(config, "TELEGRAM_USER_ID", 42)
        self.addCleanup(state.set_mode, "NORMAL", "test cleanup")

    def _pending(self, code):
        state.add_pending(code, "buy", "cex:FFF-USD", "t-cex:FFF-USD", 600)

    def test_yes_while_stopped_neither_buys_nor_burns_the_code(self):
        state.set_mode("USER_STOP", reason="test")
        self._pending("A1B2")
        self.cmds.handle("YES A1B2")
        self.assertEqual(self.approved, [])
        self.assertEqual(state.get_pending("A1B2")["status"], "pending")

    def test_the_same_code_works_after_resume(self):
        state.set_mode("USER_STOP", reason="test")
        self._pending("C3D4")
        self.cmds.handle("YES C3D4")
        state.set_mode("NORMAL", reason="test")
        self.cmds.handle("YES C3D4")
        self.assertEqual(len(self.approved), 1)
        self.assertEqual(state.get_pending("C3D4")["status"], "approved")


class ProfitPlan(Base):
    """The standing scale-out position-monitor requires the fast path to run
    without a model call."""

    def setUp(self):
        super().setUp()
        self.sells = []
        self.patch(monitor.execution, "execute_sell",
                   lambda a, r, f=1.0: self.sells.append((a, round(f, 4))) or "filled")

    def _pos(self, asset, plan):
        state.close_position(asset)
        state.upsert_position(asset, "solana", "solana", 1000.0, 5.0, plan=plan)
        return state.get_position(asset)

    def test_leg_fires_at_its_multiple_and_only_once(self):
        # entry = $5 / 1000 units = $0.005; 2x = $0.010
        p = self._pos("solana:PLAN1", {"profit_plan": [{"multiple": 2, "sell_fraction": 0.5}]})
        self.assertTrue(monitor.run_profit_plan(p, 0.010))
        self.assertEqual(self.sells, [("solana:PLAN1", 0.5)])
        self.assertFalse(monitor.run_profit_plan(p, 0.020))  # leg already done
        self.assertEqual(len(self.sells), 1)

    def test_below_the_multiple_nothing_fires(self):
        p = self._pos("solana:PLAN2", {"profit_plan": [{"multiple": 2, "sell_fraction": 0.5}]})
        self.assertFalse(monitor.run_profit_plan(p, 0.009))
        self.assertEqual(self.sells, [])

    def test_legs_fire_in_order_one_per_tick(self):
        p = self._pos("solana:PLAN3", {"profit_plan": [
            {"multiple": 2, "sell_fraction": 0.5}, {"multiple": 3, "sell_fraction": 1.0}]})
        monitor.run_profit_plan(p, 0.020)   # past both levels
        monitor.run_profit_plan(p, 0.020)
        self.assertEqual(self.sells, [("solana:PLAN3", 0.5), ("solana:PLAN3", 1.0)])

    def test_resending_the_same_plan_does_not_rearm_a_fired_leg(self):
        plan = {"profit_plan": [{"multiple": 2, "sell_fraction": 0.5}]}
        p = self._pos("solana:PLAN6", plan)
        monitor.run_profit_plan(p, 0.010)
        self.assertEqual(len(self.sells), 1)
        state.set_position_plan("solana:PLAN6", plan)   # the next HOLD cycle
        self.assertFalse(monitor.run_profit_plan(state.get_position("solana:PLAN6"), 0.010))
        self.assertEqual(len(self.sells), 1)

    def test_resizing_a_taken_level_does_not_sell_there_twice(self):
        p = self._pos("solana:PLAN7", {"profit_plan": [{"multiple": 2, "sell_fraction": 0.5}]})
        monitor.run_profit_plan(p, 0.010)
        state.set_position_plan("solana:PLAN7",
                                {"profit_plan": [{"multiple": 2, "sell_fraction": 0.25}]})
        self.assertFalse(monitor.run_profit_plan(state.get_position("solana:PLAN7"), 0.010))
        self.assertEqual(self.sells, [("solana:PLAN7", 0.5)])

    def test_a_new_level_added_by_a_revision_does_arm(self):
        p = self._pos("solana:PLAN8", {"profit_plan": [{"multiple": 2, "sell_fraction": 0.5}]})
        monitor.run_profit_plan(p, 0.010)
        state.set_position_plan("solana:PLAN8", {"profit_plan": [
            {"multiple": 2, "sell_fraction": 0.5}, {"multiple": 3, "sell_fraction": 1.0}]})
        self.assertTrue(monitor.run_profit_plan(state.get_position("solana:PLAN8"), 0.015))
        self.assertEqual(self.sells, [("solana:PLAN8", 0.5), ("solana:PLAN8", 1.0)])

    def test_a_reentered_asset_starts_with_every_leg_armed(self):
        plan = {"profit_plan": [{"multiple": 2, "sell_fraction": 1.0}]}
        p = self._pos("solana:PLAN9", plan)
        monitor.run_profit_plan(p, 0.010)
        state.close_position("solana:PLAN9")
        p2 = self._pos("solana:PLAN9", plan)          # bought again later
        self.assertTrue(monitor.run_profit_plan(p2, 0.010))
        self.assertEqual(len(self.sells), 2)

    def test_no_plan_is_not_an_error(self):
        p = self._pos("solana:PLAN4", None)
        self.assertFalse(monitor.run_profit_plan(p, 99.0))
        self.assertEqual(self.sells, [])

    def test_malformed_plan_does_not_raise(self):
        p = self._pos("solana:PLAN5", None)
        state.set_position_plan("solana:PLAN5", {"profit_plan": [{"multiple": "x"}, None]})
        self.assertFalse(monitor.run_profit_plan(state.get_position("solana:PLAN5"), 99.0))
        self.assertEqual(self.sells, [])


class PlanValidation(Base):
    def test_malformed_legs_are_dropped(self):
        self.assertIsNone(runner._plan_of({
            "profit_plan_multiples": [1, 2, 2, "x", None],
            "profit_plan_fractions": [0.5, 0, 1.5, 0.5, 0.5]}))

    def test_good_legs_survive(self):
        self.assertEqual(
            runner._plan_of({"profit_plan_multiples": [2],
                             "profit_plan_fractions": [0.5]}),
            {"profit_plan": [{"multiple": 2.0, "sell_fraction": 0.5}]})

    def test_an_unpaired_tail_is_ignored(self):
        # The model can return arrays of different length; never invent a leg.
        self.assertEqual(
            runner._plan_of({"profit_plan_multiples": [2, 5],
                             "profit_plan_fractions": [0.5]}),
            {"profit_plan": [{"multiple": 2.0, "sell_fraction": 0.5}]})

    def test_an_empty_plan_is_none(self):
        self.assertIsNone(runner._plan_of({}))


class SubmitRouting(Base):
    def setUp(self):
        super().setUp()
        self.patch(runner.marketdata, "marks", lambda a: ({}, True))
        self.patch(runner.marketdata, "price", lambda a: 1.0)
        self.patch(runner.state, "total_value", lambda m: 1000.0)
        self.patch(runner.risk, "compute_size", lambda v: 5.0)
        for a in ("solana:HELD", "solana:GHOST"):
            state.close_position(a)
        state.upsert_position("solana:HELD", "solana", "solana", 1000.0, 5.0)

    def _tickets(self, asset):
        return [t for t in state.tickets("new") if t["asset_id"] == asset]

    def test_sell_now_on_a_held_asset_files_a_ticket(self):
        runner.submit([{"asset_id": "solana:HELD", "action": "SELL_NOW",
                        "sell_fraction": 0.25}])
        t = self._tickets("solana:HELD")[-1]
        self.assertEqual(t["action"], "SELL_NOW")
        self.assertAlmostEqual(t["sell_fraction"], 0.25)

    def test_sell_now_on_nothing_held_is_dropped(self):
        self.assertEqual(runner.submit([{"asset_id": "solana:GHOST",
                                         "action": "SELL_NOW"}]), 0)
        self.assertEqual(self._tickets("solana:GHOST"), [])

    def test_add_without_a_position_is_dropped(self):
        self.assertEqual(runner.submit([{"asset_id": "solana:GHOST",
                                         "action": "ADD"}]), 0)

    def test_hold_updates_the_standing_plan_and_files_nothing(self):
        before = len(state.tickets("new"))
        runner.submit([{"asset_id": "solana:HELD", "action": "HOLD",
                        "profit_plan_multiples": [3],
                        "profit_plan_fractions": [1.0]}])
        self.assertEqual(len(state.tickets("new")), before)
        self.assertEqual(state.position_plan("solana:HELD"),
                         {"profit_plan": [{"multiple": 3.0, "sell_fraction": 1.0}]})


class GasFloor(Base):
    """A chain that cannot pay for the way out is a chain we do not enter."""

    def test_an_empty_gas_wallet_blocks_entry(self):
        self.patch(solana_dex, "sol_balance", lambda: 0.0)
        with self.assertRaises(execution.risk.Reject) as cm:
            execution._check_gas("solana")
        self.assertEqual(cm.exception.rule, "gas_floor")

    def test_a_funded_wallet_passes(self):
        self.patch(solana_dex, "sol_balance", lambda: 0.0965)
        execution._check_gas("solana")

    def test_an_unreadable_balance_fails_closed(self):
        self.patch(evm_dex, "eth_balance",
                   lambda: (_ for _ in ()).throw(RuntimeError("rpc down")))
        with self.assertRaises(execution.risk.Reject) as cm:
            execution._check_gas("base")
        self.assertEqual(cm.exception.rule, "gas_unknown")


class PairSide(Base):
    """priceUsd is the BASE token's price. USDT under a SOL/USDT pool was
    marked at $0.067 -- a different asset's number, feeding the stop."""

    USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

    def _pairs(self, pairs):
        from tradebot import marketdata
        self.patch(marketdata, "_get", lambda url, **k: {"pairs": pairs})
        return marketdata

    def _pair(self, base, quote, price_usd, price_native, liq):
        return {"chainId": "solana", "priceUsd": str(price_usd),
                "priceNative": str(price_native), "liquidity": {"usd": liq},
                "baseToken": {"address": base, "symbol": "B"},
                "quoteToken": {"address": quote, "symbol": "Q"}}

    def test_a_base_side_pair_is_preferred_over_a_deeper_quote_side_one(self):
        md = self._pairs([
            self._pair("OTHERMINT", self.USDT, 0.067, 0.067, 9_000_000),   # we are quote
            self._pair(self.USDT, "USDCMINT", 1.0004, 1.0004, 500_000),    # we are base
        ])
        self.assertAlmostEqual(md.dexscreener_token("solana", self.USDT)["price"], 1.0004)

    def test_a_quote_only_token_is_inverted_correctly(self):
        # SOL/USDT pool: SOL is base at $100, priceNative 100 USDT per SOL.
        md = self._pairs([self._pair("SOLMINT", self.USDT, 100.0, 100.0, 5_000_000)])
        self.assertAlmostEqual(md.dexscreener_token("solana", self.USDT)["price"], 1.0)

    def test_evm_addresses_match_case_insensitively(self):
        md = self._pairs([self._pair("0xABCDEF", "0xUSDC", 2.5, 2.5, 1000)])
        md_pairs = [self._pair("0xABCDEF", "0xUSDC", 2.5, 2.5, 1000)]
        md_pairs[0]["chainId"] = "base"
        self.patch(md, "_get", lambda url, **k: {"pairs": md_pairs})
        self.assertAlmostEqual(md.dexscreener_token("base", "0xabcdef")["price"], 2.5)

    def test_a_pair_we_are_on_neither_side_of_is_ignored(self):
        md = self._pairs([self._pair("X", "Y", 3.0, 3.0, 1000)])
        self.assertIsNone(md.dexscreener_token("solana", self.USDT))


class NoBalanceOnSell(Base):
    """A zero balance read is not proof the position is gone -- it is also a
    lagging RPC, a wSOL unwrap, or the wrong mint. The wSOL round trip deleted
    a position whose $5 had become native SOL."""

    def _pos(self, asset, cost):
        state.close_position(asset)
        state.upsert_position(asset, "solana", "solana", 100.0, cost)
        self.patch(execution.marketdata, "price", lambda a: 0.05)
        self.patch(solana_dex, "token_balance", lambda m: (0, 6))
        self.patch(solana_dex, "usdc_balance", lambda: 0.0)

    def test_a_valuable_position_is_kept_and_escalated(self):
        said = []
        self.patch(execution.alerts, "ops", said.append)
        self._pos("solana:GHOSTED", 5.0)
        self.assertEqual(execution.execute_sell("solana:GHOSTED", "test", 1.0),
                         "no_balance")
        self.assertIsNotNone(state.get_position("solana:GHOSTED"))
        self.assertTrue(any("NOT closed" in m for m in said))

    def test_real_dust_is_still_closed(self):
        self.patch(execution.alerts, "ops", lambda m: None)
        self._pos("solana:TINY", 0.02)
        self.assertEqual(execution.execute_sell("solana:TINY", "test", 1.0), "dust")
        self.assertIsNone(state.get_position("solana:TINY"))


class PartialExit(Base):
    """A partial fill on a full exit must reduce the position, never delete it:
    nothing reconciles positions back from the venue."""

    def setUp(self):
        super().setUp()
        self.patch(execution.marketdata, "price", lambda a: 2.0)
        state.close_position("cex:PART-USD")
        state.upsert_position("cex:PART-USD", "coinbase", None, 10.0, 10.0)

    def _order(self, filled, avg):
        return {"status": "FILLED", "filled_size": str(filled),
                "average_filled_price": str(avg), "total_fees": "0",
                "order_id": "srv-p"}

    def test_a_half_filled_full_exit_keeps_the_remainder(self):
        self.patch(coinbase, "market_sell", lambda p, q: ("srv-p", {}))
        self.patch(coinbase, "order_status", lambda o: self._order(4, 2.0))
        self.assertEqual(execution.execute_sell("cex:PART-USD", "test", 1.0), "filled")
        pos = state.get_position("cex:PART-USD")
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos["qty"], 6.0)
        self.assertAlmostEqual(pos["cost_basis_usd"], 6.0)

    def test_a_complete_exit_still_closes(self):
        self.patch(coinbase, "market_sell", lambda p, q: ("srv-p", {}))
        self.patch(coinbase, "order_status", lambda o: self._order(10, 2.0))
        self.assertEqual(execution.execute_sell("cex:PART-USD", "test", 1.0), "filled")
        self.assertIsNone(state.get_position("cex:PART-USD"))


class FractionBounds(Base):
    def test_negative_and_zero_become_a_full_exit(self):
        for bad in (-0.5, 0, "", None, "junk", float("nan")):
            self.assertEqual(execution.clamp_fraction(bad), 1.0)

    def test_over_one_is_capped(self):
        self.assertEqual(execution.clamp_fraction(3.0), 1.0)

    def test_a_real_fraction_survives(self):
        self.assertAlmostEqual(execution.clamp_fraction(0.25), 0.25)


class CommandParsing(Base):
    def setUp(self):
        super().setUp()
        self.patch(config, "TELEGRAM_USER_ID", 42)
        self.cmds = approval.Commands(lambda p: None, lambda *a: None,
                                      lambda: "s", lambda a=None: "r", lambda a: "w")

    def test_revoke_preserves_the_asset_id_case(self):
        aid = "solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        state.whitelist_add(aid, "solana")
        self.assertTrue(state.is_whitelisted(aid))
        self.cmds.handle(f"revoke {aid}")
        self.assertFalse(state.is_whitelisted(aid))

    def test_the_verb_is_case_insensitive(self):
        seen = []
        self.patch(approval.state, "set_mode", lambda m, reason="": seen.append(m))
        self.cmds.handle("stop")
        self.assertEqual(seen, ["USER_STOP"])


class AddSemantics(Base):
    def test_an_add_raises_the_stop(self):
        state.close_position("solana:ADDP")
        state.upsert_position("solana:ADDP", "solana", "solana", 100.0, 5.0,
                              invalidation=0.04)
        state.upsert_position("solana:ADDP", "solana", "solana", 100.0, 5.0,
                              invalidation=0.09)
        pos = state.get_position("solana:ADDP")
        self.assertAlmostEqual(pos["qty"], 200.0)
        self.assertAlmostEqual(pos["invalidation_price"], 0.09)


class ApprovalIsNonBlocking(Base):
    """The kill switch lives on the listener thread; a tap must not occupy it."""

    def test_the_tap_only_queues_and_the_core_loop_executes(self):
        from tradebot import core
        ran = []
        self.patch(execution, "execute_approved",
                   lambda t, v, f: ran.append(t["asset_id"]) or "filled")
        tid = state.add_ticket(asset_id="cex:QUE-USD", venue="coinbase", chain=None,
                               action="BUY_NOW", notional_usd=5.0)
        core.on_approved_buy({"ticket_id": tid})
        self.assertEqual(ran, [])                      # nothing executed on the tap
        self.assertIn(tid, [t["ticket_id"] for t in state.tickets("approved")])
        core.run_approved_tickets(1000.0, True)
        self.assertEqual(ran, ["cex:QUE-USD"])         # the core loop did it


class Calibration(Base):
    """Every forecast is observed, not only the ones that became trades."""

    def test_a_forecast_resolves_into_an_outcome(self):
        from tradebot import journal
        fid = journal.log_forecast({"asset_id": "solana:CAL1", "action": "PASS"})
        calibration.open_tracking(fid, "solana:CAL1", "PASS", 1.0)
        # it ran to 3x, then the window closed
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:CAL1": 3.0}, True))
        calibration.tick()
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        row = journal.query("SELECT * FROM outcomes WHERE forecast_id=?", (fid,))[0]
        self.assertAlmostEqual(row["max_multiple"], 3.0)
        self.assertEqual(row["hit_2x"], 1)
        self.assertEqual(row["hit_5x"], 0)

    def test_a_zero_start_price_is_not_tracked(self):
        from tradebot import journal
        fid = journal.log_forecast({"asset_id": "solana:CAL2", "action": "PASS"})
        calibration.open_tracking(fid, "solana:CAL2", "PASS", 0)
        self.assertEqual(
            journal.query("SELECT * FROM forecast_tracking WHERE forecast_id=?", (fid,)), [])


class TimeStop(Base):
    def test_a_stale_position_is_flagged_once(self):
        state.close_position("solana:OLD")
        state.upsert_position("solana:OLD", "solana", "solana", 100.0, 5.0)
        p = state.get_position("solana:OLD")
        p["entry_ts"] = 0                      # ancient
        self.assertTrue(_mon.time_stop_check(p))
        self.assertFalse(_mon.time_stop_check(p))   # not re-flagged every tick

    def test_a_fresh_position_is_not_flagged(self):
        state.close_position("solana:NEW")
        state.upsert_position("solana:NEW", "solana", "solana", 100.0, 5.0)
        self.assertFalse(_mon.time_stop_check(state.get_position("solana:NEW")))


class WhitelistBounds(Base):
    def setUp(self):
        super().setUp()
        self.aid = "solana:WL1"
        state.whitelist_revoke(self.aid)

    def test_a_fresh_approval_authorises(self):
        state.whitelist_add(self.aid, "solana")
        self.assertTrue(state.is_whitelisted(self.aid))

    def test_an_expired_approval_does_not(self):
        state.whitelist_add(self.aid, "solana")
        self.patch(config, "WHITELIST_TTL_SEC", 0)
        ok, why = state.whitelist_state(self.aid)
        self.assertFalse(ok)
        self.assertIn("expired", why)

    def test_the_reentry_cap_ends_authorisation(self):
        state.whitelist_add(self.aid, "solana")
        for _ in range(config.WHITELIST_MAX_REENTRIES):
            state.note_reentry(self.aid)
        ok, why = state.whitelist_state(self.aid)
        self.assertFalse(ok)
        self.assertIn("re-entry cap", why)

    def test_reapproval_resets_the_budget(self):
        state.whitelist_add(self.aid, "solana")
        for _ in range(config.WHITELIST_MAX_REENTRIES):
            state.note_reentry(self.aid)
        state.whitelist_add(self.aid, "solana")
        self.assertTrue(state.is_whitelisted(self.aid))


class AgentWatchdog(Base):
    """The research layer holds no Telegram credentials by design, so the core
    is the only thing that can report its silence."""

    def setUp(self):
        super().setUp()
        from tradebot import core, journal
        self.core, self.journal = core, journal
        self.said = []
        self.patch(core.alerts, "ops", self.said.append)
        state.set_kv("agent_alerted", "")
        # MAX(ts) reads the whole table, so a row from a sibling test would
        # decide this one's verdict.
        journal.conn().execute("DELETE FROM events WHERE kind='agent_cycle'")
        journal.conn().commit()

    def _cycle(self, ago):
        import time as t
        self.journal.conn().execute(
            "INSERT INTO events (ts, kind) VALUES (?, 'agent_cycle')", (t.time() - ago,))
        self.journal.conn().commit()

    def test_a_recent_cycle_is_quiet(self):
        self._cycle(60)
        self.core.supervise_agent()
        self.assertEqual(self.said, [])

    def test_a_long_silence_alerts_once(self):
        self._cycle(10 * 3600)
        self.core.supervise_agent()
        self.core.supervise_agent()
        self.assertEqual(len(self.said), 1)
        self.assertIn("no cycle", self.said[0])

    def test_recovery_is_reported(self):
        self._cycle(10 * 3600)
        self.core.supervise_agent()
        self._cycle(10)
        self.core.supervise_agent()
        self.assertEqual(len(self.said), 2)
        self.assertIn("again", self.said[1])


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
