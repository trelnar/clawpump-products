"""Regressions for the second external validation (2026-09-15, fifteen findings)."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import calibration, config, core, execution, journal, monitor, risk, rugcheck, shorts, state  # noqa: E402
from tradebot.exchanges import coinbase, evm_dex, hyperliquid as hl, solana_dex  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        state.init()
        for p in state.positions():
            state.close_position(p["asset_id"])
        state.set_mode("NORMAL", reason="test")
        self.patches = []
        self.said = []
        self.patch(execution.alerts, "ops", self.said.append)
        self.patch(shorts.alerts, "ops", self.said.append)
        self.patch(config, "SETTLE_READ_SLEEP_SEC", 0)
        self.patch(config, "SETTLE_READ_TRIES", 2)
        self.patch(execution.time, "sleep", lambda s: None)
        with journal._lock:
            c = journal.conn()
            c.execute("DELETE FROM orders WHERE status='unresolved'")
            c.execute("DELETE FROM shorts")
            c.execute("DELETE FROM tickets WHERE status IN ('submitting','approved','new')")
            c.commit()

    def patch(self, mod, name, value):
        self.patches.append((mod, name, getattr(mod, name, None)))
        setattr(mod, name, value)

    def tearDown(self):
        for mod, name, old in reversed(self.patches):
            setattr(mod, name, old)
        state.set_mode("NORMAL", reason="cleanup")


def _ticket(asset, venue, chain, tid=1, usd=10.0):
    return {"ticket_id": tid, "asset_id": asset, "venue": venue, "chain": chain,
            "notional_usd": usd, "ts": time.time(), "invalidation_price": None}


# F1 ------------------------------------------------------------------------------------
class SolanaLateConfirm(Base):
    def test_a_timed_out_solana_buy_is_remembered_by_its_signature(self):
        asset = "solana:LATE1"
        state.set_kv(f"symbol:{asset}", "late")
        state.set_cash("solana", 100.0)
        self.patch(execution.marketdata, "dexscreener_token", lambda c, a: None)
        self.patch(solana_dex, "token_decimals", lambda m: 6)
        self.patch(solana_dex, "token_balance", lambda m: (0, 6))
        self.patch(solana_dex, "swap", lambda *a, **k: ("sigLATE", {"outAmount": "0"}))

        def down(sig):
            raise RuntimeError("rpc down")
        self.patch(solana_dex, "confirm", down)
        self.patch(config, "FILL_TIMEOUT_SOL_SEC", 0.2)
        self.assertEqual(execution.execute_buy(_ticket(asset, "solana", "solana", 11), 0.002), "failed")
        self.assertEqual(len(execution.unresolved_orders(asset)), 1)
        self.assertEqual(execution.unresolved_orders(asset)[0]["client_oid"], "sigLATE")
        # later, the chain has it
        self.patch(solana_dex, "confirm", lambda s: "confirmed")
        self.patch(solana_dex, "tx_token_delta", lambda s, m, owner=None: 5000.0)
        execution.resolve_unresolved_orders()
        self.assertAlmostEqual(state.get_position(asset)["qty"], 5000.0)


# F2 / F3 ----------------------------------------------------------------------------------
class UnresolvedBookkeeping(Base):
    def _unresolved(self, asset, tx, usd=10.0, chain="solana"):
        execution._mark_order_unresolved(tx, chain, asset, usd, 0.002, "test")

    def test_a_landed_addition_is_added_to_the_position_once(self):
        asset = "solana:ADD1"
        state.set_kv(f"symbol:{asset}", "add1")
        state.set_cash("solana", 100.0)
        state.upsert_position(asset, "solana", "solana", 5000.0, 10.0)
        self._unresolved(asset, "sigADD")
        self.patch(solana_dex, "confirm", lambda s: "confirmed")
        self.patch(solana_dex, "tx_token_delta", lambda s, m, owner=None: 5000.0)
        execution.resolve_unresolved_orders()
        p = state.get_position(asset)
        self.assertAlmostEqual(p["qty"], 10000.0)
        self.assertAlmostEqual(p["cost_basis_usd"], 20.0)
        self.assertAlmostEqual(state.cash()["solana"], 90.0)
        # the same transaction marked unresolved again is not booked twice
        self._unresolved(asset, "sigADD")
        execution.resolve_unresolved_orders()
        self.assertAlmostEqual(state.get_position(asset)["qty"], 10000.0)

    def test_an_unresolved_buy_blocks_another_buy_of_the_asset_and_counts_as_exposure(self):
        asset = "solana:PEND1"
        state.set_kv(f"symbol:{asset}", "pend")
        self._unresolved(asset, "sigPEND", usd=10.0)
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        blocked = []
        self.patch(execution.alerts, "not_bought", lambda a, g, m: blocked.append(g))
        self.assertEqual(execution.process_ticket(_ticket(asset, "solana", "solana", 21), 1000.0, True),
                         "blocked")
        self.assertEqual(blocked, ["unresolved_order"])
        # the pending $10 is exposure: it fills the 5% cap of a $200 book with a $0 position
        with self.assertRaises(risk.Reject) as cm:
            risk.check_buy(asset, "solana", "solana", 1.0, 1.0, 1.0, 200.0)
        self.assertEqual(cm.exception.rule, "hard_position_cap")


# F4 ----------------------------------------------------------------------------------------
class CoinbaseCancelRace(Base):
    def test_units_filled_between_the_last_poll_and_the_cancel_are_booked(self):
        coinbase._products["RACE-USDC"] = {"quote_increment": "0.01", "base_increment": "0.00000001"}
        self.addCleanup(coinbase._products.clear)
        state.set_cash("coinbase", 100.0)
        self.patch(coinbase, "best_price", lambda p: (1.99, 2.0))
        self.patch(coinbase, "limit_buy", lambda p, n, l: ("srv-race", {}))
        cancelled = []
        polls = {"n": 0}

        def status(o):
            polls["n"] += 1
            if not cancelled:                       # before the cancel: open, nothing filled
                return {"status": "OPEN", "filled_size": "0", "average_filled_price": "0",
                        "total_fees": "0", "filled_value": "0", "order_id": o}
            return {"status": "CANCELLED", "filled_size": "5", "average_filled_price": "2.0",
                    "total_fees": "0.02", "filled_value": "10", "order_id": o}
        self.patch(coinbase, "order_status", status)
        self.patch(coinbase, "cancel", lambda oid: cancelled.append(oid))
        self.patch(config, "FILL_TIMEOUT_CEX_SEC", 0.1)
        self.assertEqual(execution.execute_buy(_ticket("cex:RACE-USDC", "coinbase", None, 31), 2.0), "filled")
        self.assertAlmostEqual(state.get_position("cex:RACE-USDC")["qty"], 5.0)
        self.assertEqual(cancelled, ["srv-race"])


# F5 ----------------------------------------------------------------------------------------
class StopAtTheSend(Base):
    def test_a_stop_during_the_price_read_still_wins(self):
        state.whitelist_add("cex:STP3-USDC", "coinbase")
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.risk, "check_buy", lambda *a, **k: None)
        self.patch(coinbase, "quote_balance", lambda q: 1000.0)

        def best_price(p):
            state.set_mode("USER_STOP", reason="operator")
            return (1.99, 2.0)
        self.patch(coinbase, "best_price", best_price)
        placed = []
        self.patch(coinbase, "limit_buy", lambda *a: placed.append(a) or ("srv", {}))
        self.assertEqual(execution.process_ticket(_ticket("cex:STP3-USDC", "coinbase", None, 41), 1000.0, True),
                         "blocked")
        self.assertEqual(placed, [])

    def test_a_stop_during_the_short_account_read_still_wins(self):
        self.patch(config, "SHORTS_ENABLED", True)
        state.set_kv("symbol:perp:ETH", "ETH")
        state.whitelist_add("perp:ETH", "hyperliquid")

        def account_value():
            state.set_mode("USER_STOP", reason="operator")
            return 100.0
        self.patch(hl, "account_value", account_value)
        opened = []
        self.patch(hl, "open_short", lambda *a: opened.append(a) or (0.01, 2600.0, 1))
        tid = state.add_ticket(asset_id="perp:ETH", venue="hyperliquid", chain=None,
                               action="SHORT_NOW", notional_usd=15.0, ts=time.time())
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.assertEqual(shorts.execute(t), "blocked")
        self.assertEqual(opened, [])


# F6 / F13 ----------------------------------------------------------------------------------
class HolderBound(Base):
    def _rpc(self, amounts, owners, supply, census=None):
        def rpc(method, params):
            if method == "getProgramAccounts":
                if census is None:
                    raise RuntimeError("disabled")
                return [{"account": {"data": {"parsed": {"info": {"owner": o, "tokenAmount": {"amount": str(a)}}}}}}
                        for o, a in census]
            if method == "getTokenLargestAccounts":
                return {"value": [{"address": f"A{i}", "amount": str(a)} for i, a in enumerate(amounts)]}
            if method == "getTokenSupply":
                return {"value": {"amount": str(supply)}}
            return {"value": [({"data": {"parsed": {"info": {"owner": o}}}} if o else None) for o in owners]}
        self.patch(solana_dex, "_rpc", rpc)

    def test_an_unreadable_owner_is_a_refusal(self):
        self._rpc([450] + [25] * 19, ["POOL"] + [None] + ["DEV"] * 18, 1000)
        with self.assertRaises(rugcheck.Unbounded):
            rugcheck.top_holders_share("MINT", "POOL")

    def test_thin_coverage_is_a_refusal(self):
        self.patch(config, "RUG_MIN_HOLDER_COVERAGE", 0.40)
        # pool 45%; the 20 largest non-pool accounts sum to 19% of 55%: 35% seen
        self._rpc([450] + [10] * 19, ["POOL"] + [f"w{i}" for i in range(19)], 1000)
        with self.assertRaises(rugcheck.Unbounded) as cm:
            rugcheck.top_holders_share("MINT", "POOL")
        self.assertIn("could only see 35%", str(cm.exception))

    def test_a_full_census_bounds_a_developer_spread_over_fifty_accounts(self):
        census = [("POOL", 450)] + [("DEV", 10)] * 50 + [(f"w{i}", 1) for i in range(50)]
        self._rpc([450] + [10] * 19, ["POOL"] + ["DEV"] * 19, 1000, census=census)
        share, m = rugcheck.top_holders_share("MINT", "POOL")
        self.assertTrue(m["census"])
        self.assertAlmostEqual(share, 0.509)          # the developer plus nine 0.1% wallets

    def test_unknown_pool_age_is_a_refusal(self):
        ok, why, _ = rugcheck.check_pair({"liquidity_usd": 50000, "dex": "uniswap"})
        self.assertFalse(ok)
        self.assertEqual(why, "I can't tell how old the pool is")


# F7 / F8 / F9 ---------------------------------------------------------------------------------
class ShortSupervision(Base):
    def _row(self, qty=1.0):
        state.set_kv("symbol:perp:ETH", "ETH")
        shorts._write({"asset_id": "perp:ETH", "coin": "ETH", "qty": qty, "entry_price": 100.0,
                       "notional_usd": 100.0 * qty, "entry_ts": time.time() - 60,
                       "stop_price": 104.0, "ratchet": json.dumps(shorts._new_ratchet()),
                       "lwm": 100.0, "last_alert_ts": 0})

    def test_a_short_opened_before_shorts_were_turned_off_still_has_its_stop(self):
        self.patch(config, "SHORTS_ENABLED", False)
        self._row()
        self.patch(hl, "mids", lambda: {"ETH": 150.0})
        covered = []
        self.patch(shorts, "cover", lambda a, f, r: covered.append((a, f, r)) or "filled")
        shorts.monitor(time.time())
        self.assertEqual(len(covered), 1)
        self.assertIn("stop", covered[0][2])

    def test_sdk_clients_get_a_timeout(self):
        seen = {}

        class FakeInfo:
            def __init__(self, url, skip_ws=False, timeout=None):
                seen["info"] = timeout

        class FakeEx:
            def __init__(self, acct, url, timeout=None):
                seen["ex"] = timeout

        class C:
            MAINNET_API_URL = "x"
        self.patch(hl, "_sdk", lambda: (FakeInfo, FakeEx, C))
        self.patch(hl, "_info", None)
        self.patch(config, "HL_HTTP_TIMEOUT", 15.0)
        hl.info()
        self.assertEqual(seen["info"], 15.0)

    def test_a_ticket_that_was_mid_send_is_settled_against_the_exchange_not_replayed(self):
        self.patch(config, "SHORTS_ENABLED", True)
        state.set_kv("symbol:perp:ETH", "ETH")
        tid = state.add_ticket(asset_id="perp:ETH", venue="hyperliquid", chain=None,
                               action="SHORT_NOW", notional_usd=15.0, ts=time.time(),
                               status="submitting")
        self.patch(hl, "positions", lambda: {"ETH": {"size": -0.15, "entry": 100.0, "upnl": 0}})
        self.patch(hl, "mid", lambda c: 100.0)
        opened = []
        self.patch(hl, "open_short", lambda *a: opened.append(a) or (0.15, 100.0, 1))
        shorts.resolve_submitting()
        self.assertEqual(opened, [])
        self.assertEqual(journal.query("SELECT status FROM tickets WHERE ticket_id=?", (tid,))[0]["status"],
                         "filled")
        self.assertAlmostEqual(shorts.get("perp:ETH")["qty"], 0.15)
        # and one whose send never landed is closed, not replayed
        tid2 = state.add_ticket(asset_id="perp:WIF", venue="hyperliquid", chain=None,
                                action="SHORT_NOW", notional_usd=15.0, ts=time.time(),
                                status="submitting")
        shorts.resolve_submitting()
        self.assertEqual(journal.query("SELECT status FROM tickets WHERE ticket_id=?", (tid2,))[0]["status"],
                         "failed")

    def test_reconcile_takes_the_exchanges_size(self):
        self.patch(config, "SHORTS_ENABLED", True)
        self._row(qty=0.15)
        shorts._last_recon[0] = 0
        self.patch(hl, "positions", lambda: {"ETH": {"size": -0.30, "entry": 100.0, "upnl": 0}})
        shorts.reconcile(time.time())
        self.assertAlmostEqual(shorts.get("perp:ETH")["qty"], 0.30)
        self.assertTrue(any("not the 0.15 in my book" in m for m in self.said))


# F11 / F12 ---------------------------------------------------------------------------------
class TicketsAndCash(Base):
    def test_a_failed_sell_ticket_is_not_marked_done(self):
        asset = "solana:SELLF"
        state.upsert_position(asset, "solana", "solana", 100.0, 10.0)
        tid = state.add_ticket(asset_id=asset, venue="solana", chain="solana", action="SELL_NOW",
                               notional_usd=None, sell_fraction=1.0, ts=time.time())
        self.patch(execution, "execute_sell", lambda a, r, f=1.0: "failed")
        core.run_new_tickets(1000.0, True)
        self.assertEqual(journal.query("SELECT status FROM tickets WHERE ticket_id=?", (tid,))[0]["status"],
                         "failed")

    def test_cash_reconciliation_waits_for_the_order_lock(self):
        held = []
        self.patch(monitor.state, "set_cash", lambda v, u: held.append(execution._order_lock._is_owned()))
        self.patch(coinbase, "usdc_balance", lambda: 1.0)
        self.patch(solana_dex, "usdc_balance", lambda: 1.0)
        self.patch(evm_dex, "usdc_balance", lambda: 1.0)
        monitor.reconcile_cash()
        self.assertEqual(held, [True, True, True])


# F14 / F15 ---------------------------------------------------------------------------------
class ScoreCoverage(Base):
    def setUp(self):
        super().setUp()
        with journal._lock:
            c = journal.conn()
            for t in ("outcomes", "forecast_tracking", "ratchet_track"):
                c.execute(f"DELETE FROM {t}")
            c.commit()
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 12 * 3600)

    def test_an_unwatched_window_is_not_a_miss(self):
        fid = journal.log_forecast({"asset_id": "solana:BLIND", "action": "PASS", "p30": 0.3})
        calibration.open_tracking(fid, "solana:BLIND", "PASS", 1.0)
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:BLIND": 1.05}, True))
        calibration.tick()                                  # one print at +0, then silence
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        o = journal.query("SELECT * FROM outcomes WHERE forecast_id=?", (fid,))[0]
        self.assertIsNone(o["hit_30"])
        self.assertIsNone(o["sim_result"])
        self.assertNotIn("reached_30pct_in_6h_share", calibration.feedback()["PASS"])

    def test_a_level_removed_and_restored_keeps_being_watched(self):
        self.patch(calibration.config, "SIM_STOPS", [0.10, 0.20])
        self.patch(calibration.config, "SIM_TARGETS", [0.15])
        fid = journal.log_forecast({"asset_id": "solana:GAPLV", "action": "PASS", "p30": 0.3})
        calibration.open_tracking(fid, "solana:GAPLV", "PASS", 1.0)
        with journal._lock:
            journal.conn().execute("UPDATE forecast_tracking SET start_ts=start_ts-? WHERE forecast_id=?",
                                   (calibration.config.P30_WINDOW_SEC - 120, fid))
            journal.conn().commit()
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:GAPLV": 1.0}, True))
        calibration.tick()                                  # watched lists recorded
        self.patch(calibration.config, "SIM_STOPS", [0.10])  # operator removes 20%
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:GAPLV": 0.79}, True))
        calibration.tick()                                  # -21% prints meanwhile
        self.patch(calibration.config, "SIM_STOPS", [0.10, 0.20])   # and restores it
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:GAPLV": 1.16}, True))
        calibration.tick()
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        g = json.loads(journal.query("SELECT sim_grid FROM outcomes WHERE forecast_id=?", (fid,))[0]["sim_grid"])
        self.assertEqual(g["20/15"][0], "stop")
        self.assertAlmostEqual(g["20/15"][1], -0.24)


if __name__ == "__main__":
    unittest.main()
