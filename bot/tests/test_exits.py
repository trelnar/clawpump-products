"""Regression tests for the 2026-09-13 exit accounting and entry timing work.

- A sell's proceeds were one wallet balance read after confirmation. When
  that read lagged, a $10 exit booked as $0 proceeds: a phantom -100% loss.
- Exits booked that way are repaired from their own transaction.
- A buy went in while its token was being dumped and stopped out in two
  minutes; the gates now defer such a ticket until the 5-minute move settles.
- SCORE can now say whether the +30% printed BEFORE the -15% did.
"""
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

from tradebot import calibration, config, core, execution, journal, state  # noqa: E402
from tradebot.exchanges import evm_dex, solana_dex  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        state.init()
        for p in state.positions():
            state.close_position(p["asset_id"])
        state.set_mode("NORMAL")
        self.patches = []
        self.patch(config, "SETTLE_READ_SLEEP_SEC", 0)
        self.said = []
        self.patch(execution.alerts, "ops", self.said.append)

    def patch(self, mod, name, value):
        self.patches.append((mod, name, getattr(mod, name, None)))
        setattr(mod, name, value)

    def tearDown(self):
        for mod, name, old in reversed(self.patches):
            setattr(mod, name, old)

    def _clear(self, *kinds):
        with journal._lock:
            c = journal.conn()
            for k in kinds:
                c.execute("DELETE FROM events WHERE kind=?", (k,))
            c.commit()

    def _exit_rows(self, asset):
        return [json.loads(r["detail"]) for r in journal.query(
            "SELECT detail FROM events WHERE kind='exit_pnl' AND asset_id=? ORDER BY ts",
            (asset,))]


class SellProceeds(Base):
    """A confirmed sell is booked from its transaction, never from a lagging
    balance read, and never as a silent zero."""

    def _pos(self, asset="solana:SP1"):
        state.set_kv(f"symbol:{asset}", "sp1")
        state.upsert_position(asset, "solana", "solana", 100.0, 10.0)
        state.set_cash("solana", 50.0)
        self.patch(execution.marketdata, "price", lambda a: 0.10)
        self.patch(solana_dex, "token_balance", lambda m: (100_000_000, 6))
        self.patch(solana_dex, "swap", lambda *a, **k: ("sigP", {"outAmount": "8150000"}))
        self.patch(solana_dex, "confirm", lambda s: "confirmed")
        return asset

    def test_the_transaction_is_the_source_even_when_the_balance_lags(self):
        asset = self._pos()
        self.patch(solana_dex, "usdc_balance", lambda: 50.0)        # never rises
        self.patch(solana_dex, "tx_token_delta", lambda sig, mint, owner=None: 8.15)
        self.assertEqual(execution.execute_sell(asset, "invalidation 0.085 crossed", 1.0),
                         "filled")
        d = self._exit_rows(asset)[-1]
        self.assertAlmostEqual(d["proceeds"], 8.15)
        self.assertAlmostEqual(d["pnl"], -1.85)
        self.assertNotIn("unmeasured", d)
        self.assertAlmostEqual(state.cash()["solana"], 58.15)
        self.assertIsNone(state.get_position(asset))

    def test_the_balance_is_reread_until_it_rises_when_the_tx_is_not_there_yet(self):
        asset = self._pos()
        reads = iter([50.0, 50.0, 58.15, 58.15, 58.15])
        self.patch(solana_dex, "usdc_balance", lambda: next(reads))
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: None)
        # the first read is `before`, then the settle loop: lag, lag, arrived
        self.assertEqual(execution.execute_sell(asset, "test", 1.0), "filled")
        d = self._exit_rows(asset)[-1]
        self.assertAlmostEqual(d["proceeds"], 8.15)
        self.assertNotIn("unmeasured", d)

    def test_a_sale_the_chain_will_not_report_is_booked_at_the_quote_and_flagged(self):
        asset = self._pos()
        self.patch(config, "SETTLE_READ_TRIES", 2)
        self.patch(solana_dex, "usdc_balance", lambda: 50.0)
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: None)
        self.assertEqual(execution.execute_sell(asset, "test", 1.0), "filled")
        d = self._exit_rows(asset)[-1]
        self.assertAlmostEqual(d["proceeds"], 8.15)          # the quote's outAmount
        self.assertTrue(d.get("unmeasured"))
        self.assertTrue(any("Check the wallet" in m for m in self.said))

    def test_exit_friction_is_logged_against_the_print_sold_on(self):
        asset = self._pos()
        self._clear("sell_friction")
        self.patch(solana_dex, "usdc_balance", lambda: 50.0)
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: 8.15)
        execution.execute_sell(asset, "test", 1.0)
        fr = json.loads(journal.query(
            "SELECT detail FROM events WHERE kind='sell_friction' AND asset_id=?",
            (asset,))[-1]["detail"])
        self.assertAlmostEqual(fr["mid"], 0.10)
        self.assertAlmostEqual(fr["eff"], 0.0815)
        self.assertAlmostEqual(fr["pct"], -0.185)

    def test_a_partial_swap_exit_books_the_requested_share(self):
        asset = self._pos()
        self.patch(solana_dex, "usdc_balance", lambda: 50.0)
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: 9.0)
        self.assertEqual(execution.execute_sell(asset, "ratchet take", 0.75), "filled")
        left = state.get_position(asset)
        self.assertAlmostEqual(left["qty"], 25.0)
        self.assertAlmostEqual(left["cost_basis_usd"], 2.5)
        self.assertAlmostEqual(self._exit_rows(asset)[-1]["pnl"], 9.0 - 7.5)


class RepairZeroProceeds(Base):
    """Exits booked with $0 proceeds are re-read from their transaction."""

    def _seed(self, asset, tx="sigR"):
        state.set_kv(f"symbol:{asset}", "rz")
        journal.log_event("exit_pnl", asset, {"pnl": -10.0, "proceeds": 0.0, "cost": 10.0,
                                              "share": 1.0, "reason": "invalidation"})
        journal.log_fill(client_oid=tx, asset_id=asset, side="sell", qty=100.0, price=0.1,
                         fee_usd=None, venue="solana", tx_ref=tx)

    def test_a_phantom_total_loss_is_corrected_from_the_chain(self):
        self._clear("exit_pnl", "exit_pnl_repaired")
        self._seed("solana:RZ1")
        self.patch(solana_dex, "tx_token_delta", lambda sig, mint, owner=None: 8.15)
        fixed = execution.repair_zero_proceeds()
        self.assertEqual([(a, round(n, 2)) for a, _o, n in fixed], [("solana:RZ1", -1.85)])
        d = self._exit_rows("solana:RZ1")[-1]
        self.assertAlmostEqual(d["proceeds"], 8.15)
        self.assertAlmostEqual(d["pnl"], -1.85)
        self.assertTrue(d["repaired"])
        self.assertAlmostEqual(d["pnl_before_repair"], -10.0)
        self.assertTrue(any("Corrected 1 past exit" in m for m in self.said))
        # idempotent: nothing left to repair
        self.assertEqual(execution.repair_zero_proceeds(), [])
        txt = core.pnl_text("30")
        self.assertIn("Realised : $-1.85", txt)
        self.assertIn("corrected from $-10.00", txt)

    def test_a_genuine_zero_is_left_alone(self):
        self._clear("exit_pnl")
        self._seed("solana:RZ2", tx="sigZ")
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: 0.0)
        self.assertEqual(execution.repair_zero_proceeds(), [])
        self.assertAlmostEqual(self._exit_rows("solana:RZ2")[-1]["pnl"], -10.0)

    def test_an_unreadable_transaction_does_not_stop_the_pass(self):
        self._clear("exit_pnl")
        self._seed("solana:RZ3", tx="sigE")
        self._seed("solana:RZ4", tx="sigOK")

        def delta(sig, mint, owner=None):
            if sig == "sigE":
                raise RuntimeError("rpc down")
            return 8.0
        self.patch(solana_dex, "tx_token_delta", delta)
        fixed = execution.repair_zero_proceeds()
        self.assertEqual([a for a, _o, _n in fixed], ["solana:RZ4"])


class EntryTiming(Base):
    """A hype call that is being sold into is deferred, not bought."""

    def _ticket(self, asset="solana:KNIFE", age=0):
        state.set_kv(f"symbol:{asset}", "knife")
        tid = state.add_ticket(asset_id=asset, venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=10.0, ts=time.time() - age)
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.risk, "check_buy", lambda *a, **k: None)
        self.patch(solana_dex, "exit_safety", lambda *a, **k: (True, None, {"roundtrip_loss": 0.02}))
        self.patch(solana_dex, "sol_balance", lambda: 1.0)
        self.not_bought = []
        self.patch(execution.alerts, "not_bought", lambda *a: self.not_bought.append(a))
        self.asked = []
        self.patch(execution.approval, "request_buy_approval",
                   lambda t, p, f, fast=False: self.asked.append(t["asset_id"]))
        return t

    def test_a_falling_knife_is_deferred_and_the_ticket_stays_new(self):
        t = self._ticket()
        self.patch(execution.marketdata, "last_info", lambda a, max_age=60: {"change_m5": -12.0})
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "deferred")
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "deferred")
        self.assertEqual(self.not_bought, [])
        self.assertEqual(self.asked, [])
        self.assertIn(t["ticket_id"], [x["ticket_id"] for x in state.tickets("new")])
        waits = [m for m in self.said if "Not buying knife yet" in m]
        self.assertEqual(len(waits), 1)                  # said once, not every pass
        self.assertIn("down 12% in the last 5 min", waits[0])

    def test_it_is_bought_once_the_move_settles(self):
        t = self._ticket()
        info = {"change_m5": -9.0}
        self.patch(execution.marketdata, "last_info", lambda a, max_age=60: info)
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "deferred")
        info["change_m5"] = 1.5
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "awaiting_approval")
        self.assertEqual(self.asked, [t["asset_id"]])
        self.assertIsNone(state.get_kv(f"defer:{t['ticket_id']}"))

    def test_a_vertical_spike_is_deferred_too(self):
        t = self._ticket("solana:SPIKE")
        self.patch(execution.marketdata, "last_info", lambda a, max_age=60: {"change_m5": 45.0})
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "deferred")
        self.assertTrue(any("up 45% in the last 5 min" in m for m in self.said))

    def test_a_ticket_that_never_settled_is_waited_out(self):
        t = self._ticket("solana:WAIT", age=config.TICKET_MAX_AGE_SEC + 1)
        state.set_kv(f"defer:{t['ticket_id']}", "1")
        self.patch(execution.marketdata, "last_info", lambda a, max_age=60: {"change_m5": -9.0})
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "blocked")
        self.assertEqual(self.not_bought[-1][1], "waited_out")
        self.assertIsNone(state.get_kv(f"defer:{t['ticket_id']}"))

    def test_no_pair_info_means_no_timing_gate(self):
        t = self._ticket("solana:BLIND")
        self.patch(execution.marketdata, "last_info", lambda a, max_age=60: None)
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "awaiting_approval")

    def test_an_expensive_round_trip_is_refused(self):
        t = self._ticket("solana:TAX")
        self.patch(execution.marketdata, "last_info", lambda a, max_age=60: {"change_m5": 0.0})
        self.patch(solana_dex, "exit_safety",
                   lambda *a, **k: (True, None, {"roundtrip_loss": 0.07}))
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "blocked")
        self.assertEqual(self.not_bought[-1][1], "roundtrip_cost")


class PnlLastExits(Base):
    def test_each_exit_gets_a_line_with_hold_time_and_reason(self):
        self._clear("exit_pnl", "buy_friction", "sell_friction")
        state.set_kv("symbol:solana:LX1", "rat")
        now = time.time()
        with journal._lock:
            c = journal.conn()
            c.execute("INSERT INTO fills (ts, asset_id, side, qty, price, venue) VALUES (?,?,?,?,?,?)",
                      (now - 120, "solana:LX1", "buy", 100.0, 0.1, "solana"))
            c.execute("INSERT INTO events (ts, kind, asset_id, detail) VALUES (?,?,?,?)",
                      (now, "exit_pnl", "solana:LX1", json.dumps({
                          "pnl": -1.85, "proceeds": 8.15, "cost": 10.0, "share": 1.0,
                          "reason": "invalidation 0.085 crossed at 0.084"})))
            c.execute("INSERT INTO events (ts, kind, asset_id, detail) VALUES (?,?,?,?)",
                      (now, "buy_friction", "solana:LX1", json.dumps({"pct": 0.02})))
            c.execute("INSERT INTO events (ts, kind, asset_id, detail) VALUES (?,?,?,?)",
                      (now, "sell_friction", "solana:LX1", json.dumps({"pct": -0.018})))
            c.commit()
        txt = core.pnl_text("1")
        self.assertIn("Last exits (UTC):", txt)
        self.assertIn("rat: $-1.85 (-18%) after 2m, it hit the stop", txt)
        self.assertIn("Friction : ~3.8% per round trip (entry +2.0%, exit -1.8%)", txt)


class SimOutcome(Base):
    """SCORE tells the difference between 'reached +30%' and 'would have won'."""

    def setUp(self):
        super().setUp()
        with journal._lock:
            c = journal.conn()
            for t in ("outcomes", "forecast_tracking", "ratchet_track"):
                c.execute(f"DELETE FROM {t}")
            c.commit()
        self.patch(calibration.config, "P30_TARGET", 0.30)
        self.patch(calibration.config, "STOP_LOSS_PCT", 0.15)
        self.patch(calibration.config, "SIM_FRICTION", 0.04)

    def _run(self, asset, start, samples, action="BUY_NOW"):
        fid = journal.log_forecast({"asset_id": asset, "action": action, "p30": 0.5})
        calibration.open_tracking(fid, asset, action, start)
        for px in samples:
            self.patch(calibration.marketdata, "marks", lambda a, px=px: ({asset: px}, True))
            calibration.tick()
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        return journal.query("SELECT * FROM outcomes WHERE forecast_id=?", (fid,))[0]

    def test_the_stop_printing_first_is_a_loss_even_if_plus30_came_later(self):
        o = self._run("solana:SIM1", 1.0, [0.84, 1.35])
        self.assertEqual(o["hit_30"], 1)                 # +30% was there...
        self.assertEqual(o["sim_result"], "stop")        # ...after the stop had fired
        self.assertAlmostEqual(o["sim_return"], -0.15)
        txt = calibration.scorecard(1)
        self.assertIn("stopped 100%", txt)
        self.assertIn("$-1.90 each after 4% friction", txt)

    def test_the_target_printing_first_is_a_win(self):
        o = self._run("solana:SIM2", 1.0, [1.31, 0.8])
        self.assertEqual(o["sim_result"], "target")
        self.assertAlmostEqual(o["sim_return"], 0.30)
        fb = calibration.feedback()["BUY_NOW"]
        self.assertAlmostEqual(fb["target_before_stop_share"], 1.0)
        self.assertAlmostEqual(fb["stopped_first_share"], 0.0)
        self.assertAlmostEqual(fb["sim_pnl_per_10usd"], 2.6)

    def test_neither_is_the_move_at_the_end_of_the_window(self):
        o = self._run("solana:SIM3", 1.0, [1.05, 1.10])
        self.assertEqual(o["sim_result"], "flat")
        self.assertAlmostEqual(o["sim_return"], 0.10)

    def test_a_short_thesis_is_mirrored(self):
        self.patch(calibration.config, "HL_TARGET", 0.08)
        self.patch(calibration.config, "HL_STOP_PCT", 0.04)
        o = self._run("perp:SIMX", 100.0, [105.0, 90.0], action="SHORT_NOW")
        self.assertEqual(o["sim_result"], "stop")
        self.assertAlmostEqual(o["sim_return"], -0.04)
        o = self._run("perp:SIMY", 100.0, [91.0], action="SHORT_NOW")
        self.assertEqual(o["sim_result"], "target")
        self.assertAlmostEqual(o["sim_return"], 0.08)


class TxDeltaParsers(Base):
    def test_solana_delta_reads_the_owners_usdc_change(self):
        self.patch(solana_dex, "address", lambda: "OWNER")
        tx = {"meta": {"err": None,
                       "preTokenBalances": [
                           {"mint": solana_dex.USDC_MINT, "owner": "OWNER",
                            "uiTokenAmount": {"uiAmountString": "50.0"}},
                           {"mint": "OTHER", "owner": "OWNER",
                            "uiTokenAmount": {"uiAmountString": "100"}},
                           {"mint": solana_dex.USDC_MINT, "owner": "POOL",
                            "uiTokenAmount": {"uiAmountString": "9000"}}],
                       "postTokenBalances": [
                           {"mint": solana_dex.USDC_MINT, "owner": "OWNER",
                            "uiTokenAmount": {"uiAmountString": "58.15"}},
                           {"mint": "OTHER", "owner": "OWNER",
                            "uiTokenAmount": {"uiAmountString": "0"}},
                           {"mint": solana_dex.USDC_MINT, "owner": "POOL",
                            "uiTokenAmount": {"uiAmountString": "8991.85"}}]}}
        self.patch(solana_dex, "_rpc", lambda m, p: tx)
        self.assertAlmostEqual(solana_dex.tx_token_delta("sig", solana_dex.USDC_MINT), 8.15)
        self.patch(solana_dex, "_rpc", lambda m, p: None)
        self.assertIsNone(solana_dex.tx_token_delta("sig", solana_dex.USDC_MINT))
        self.patch(solana_dex, "_rpc", lambda m, p: {"meta": {"err": {"x": 1}}})
        self.assertIsNone(solana_dex.tx_token_delta("sig", solana_dex.USDC_MINT))

    def test_base_delta_sums_transfer_logs_to_and_from_the_owner(self):
        owner = "0x" + "ab" * 20
        pad = lambda a: bytes.fromhex("00" * 12 + a[2:])  # noqa: E731
        topic = bytes.fromhex(evm_dex.TRANSFER_TOPIC[2:])
        word = lambda n: ("0x%064x" % n)  # noqa: E731
        logs = [
            {"address": evm_dex.USDC, "topics": [topic, pad("0x" + "11" * 20), pad(owner)],
             "data": word(8_150_000)},                       # +8.15 to us
            {"address": evm_dex.USDC, "topics": [topic, pad(owner), pad("0x" + "22" * 20)],
             "data": word(150_000)},                         # -0.15 from us (a fee leg)
            {"address": "0x" + "33" * 20, "topics": [topic, pad("0x" + "11" * 20), pad(owner)],
             "data": word(999)},                             # another token: ignored
        ]

        class Eth:
            def get_transaction_receipt(self, h):
                return {"status": 1, "logs": logs}

        class W3:
            eth = Eth()
        self.patch(evm_dex, "_w3", lambda: W3())
        self.patch(evm_dex, "address", lambda: owner)
        self.assertAlmostEqual(evm_dex.tx_token_delta("0xh", evm_dex.USDC, decimals=6), 8.0)


class PairInfo(Base):
    def _pairs(self, pairs):
        from tradebot import marketdata
        self.patch(marketdata, "_get", lambda url, **k: {"pairs": pairs})
        return marketdata

    def test_five_minute_change_rides_with_the_price(self):
        md = self._pairs([{"chainId": "solana", "baseToken": {"address": "TOK"},
                           "quoteToken": {"address": "SOL"}, "priceUsd": "0.5",
                           "liquidity": {"usd": 9000}, "priceChange": {"m5": -12.5, "h1": "40"}}])
        info = md.dexscreener_token("solana", "TOK")
        self.assertAlmostEqual(info["change_m5"], -12.5)
        self.assertAlmostEqual(info["change_h1"], 40.0)
        p, i = md.price_info("solana:TOK")
        self.assertAlmostEqual(p, 0.5)
        self.assertIs(md.last_info("solana:TOK"), i)
        self.assertIsNone(md.last_info("solana:NOPE"))

    def test_a_quote_side_pair_reports_our_move_not_the_base_tokens(self):
        md = self._pairs([{"chainId": "solana", "baseToken": {"address": "SOL"},
                           "quoteToken": {"address": "TOK"}, "priceUsd": "200",
                           "priceNative": "400", "liquidity": {"usd": 9000},
                           "priceChange": {"m5": 25.0}}])
        info = md.dexscreener_token("solana", "TOK")
        self.assertAlmostEqual(info["price"], 0.5)
        self.assertAlmostEqual(info["change_m5"], -20.0)     # base +25% == ours -20%


class SymbolLookup(Base):
    def test_the_pnl_listing_never_goes_to_the_network_for_a_name(self):
        from tradebot import alerts, marketdata
        alerts._symbol_cache.pop("solana:NONET123", None)
        calls = []
        self.patch(marketdata, "dexscreener_token", lambda c, a: calls.append(a) or None)
        self.assertEqual(alerts.symbol("solana:NONET123", lookup=False), "NONET1")
        self.assertEqual(calls, [])
        self.assertIsNone(state.get_kv("symbol:solana:NONET123"))


class TrackerAsync(Base):
    def test_a_pass_does_not_overlap_the_previous_one(self):
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(5)
        self.patch(calibration, "tick", slow)
        self.assertTrue(calibration.tick_async())
        started.wait(2)
        self.assertFalse(calibration.tick_async())      # still running: skipped
        release.set()
        calibration._tick_thread[0].join(2)
        self.assertTrue(calibration.tick_async())
        calibration._tick_thread[0].join(2)


if __name__ == "__main__":
    unittest.main()
