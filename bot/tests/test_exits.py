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
        self.assertTrue(any("check the wallet" in m for m in self.said))

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

    def test_an_unmeasured_sale_logs_no_slippage_row(self):
        asset = self._pos()
        self._clear("sell_friction")
        self.patch(config, "SETTLE_READ_TRIES", 1)
        self.patch(solana_dex, "usdc_balance", lambda: 50.0)
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: None)
        execution.execute_sell(asset, "test", 1.0)
        self.assertEqual(journal.query("SELECT COUNT(*) n FROM events WHERE kind='sell_friction' "
                                       "AND asset_id=?", (asset,))[0]["n"], 0)
        self.assertIn("entry_ts", self._exit_rows(asset)[-1])

    def test_selling_every_unit_the_books_know_of_closes_the_position(self):
        asset = self._pos()
        # the wallet holds twice what the books say (an earlier orphaned buy)
        self.patch(solana_dex, "token_balance", lambda m: (200_000_000, 6))
        self.patch(solana_dex, "usdc_balance", lambda: 50.0)
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: 15.0)
        self.assertEqual(execution.execute_sell(asset, "ratchet take", 0.75), "filled")
        self.assertIsNone(state.get_position(asset))          # not a -50 unit row
        d = self._exit_rows(asset)[-1]
        self.assertAlmostEqual(d["cost"], 10.0)
        self.assertAlmostEqual(d["pnl"], 5.0)

    def test_a_partial_swap_exit_books_the_requested_share(self):
        asset = self._pos()
        self.patch(solana_dex, "usdc_balance", lambda: 50.0)
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: 9.0)
        self.assertEqual(execution.execute_sell(asset, "ratchet take", 0.75), "filled")
        left = state.get_position(asset)
        self.assertAlmostEqual(left["qty"], 25.0)
        self.assertAlmostEqual(left["cost_basis_usd"], 2.5)
        self.assertAlmostEqual(self._exit_rows(asset)[-1]["pnl"], 9.0 - 7.5)


class DeferredApproval(Base):
    """A deferral turns 'the moment of the tap' into a window of minutes."""

    def _approved(self, asset):
        state.set_kv(f"symbol:{asset}", "rvk")
        tid = state.add_ticket(asset_id=asset, venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=10.0, ts=time.time(),
                               status="approved")
        t = [x for x in state.tickets("approved") if x["ticket_id"] == tid][0]
        state.whitelist_add(asset, "solana")
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.risk, "check_buy", lambda *a, **k: None)
        self.patch(solana_dex, "exit_safety", lambda *a, **k: (True, None, {"roundtrip_loss": 0.02}))
        self.patch(solana_dex, "sol_balance", lambda: 1.0)
        self.patch(execution.rugcheck, "check", lambda *a, **k: (True, None, {}))
        self.bought = []
        def buy(t, ref):
            self.bought.append(t["asset_id"])
            state.set_ticket_status(t["ticket_id"], "filled")
            return "filled"
        self.patch(execution, "execute_buy", buy)
        self.info = {"change_m5": -8.0}
        self.patch(execution.marketdata, "last_info", lambda a, max_age=60: self.info)
        return t

    def test_a_revoke_during_the_wait_stops_the_buy(self):
        t = self._approved("solana:REVOKEME")
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "deferred")
        state.whitelist_revoke("solana:REVOKEME")
        self.info["change_m5"] = 1.0
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "blocked")
        self.assertEqual(self.bought, [])
        self.assertTrue(any("approval was withdrawn" in m for m in self.said))
        self.assertIsNone(state.get_kv(f"defer:{t['ticket_id']}"))

    def test_a_stop_out_during_the_wait_stops_the_buy(self):
        t = self._approved("solana:STOPPED")
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "deferred")
        state.note_stopout("solana:STOPPED")
        self.info["change_m5"] = 1.0
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "blocked")
        self.assertEqual(self.bought, [])
        self.assertIn("blocked:stopout_cooldown",
                      [x["status"] for x in journal.query(
                          "SELECT status FROM tickets WHERE ticket_id=?", (t["ticket_id"],))])

    def test_the_wait_does_not_eat_the_tickets_life(self):
        t = self._approved("solana:PATIENT")
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "deferred")
        # 14 minutes pass while it waits; the call itself is now 'old'
        t = dict(t, ts=t["ts"] - 14 * 60)
        state.set_kv(f"defer:{t['ticket_id']}", f"{time.time() - 60:.0f}")
        self.info["change_m5"] = 0.5
        self.assertEqual(execution.execute_approved(t, 1000.0, True), "filled")
        self.assertEqual(self.bought, ["solana:PATIENT"])


class RepairZeroProceeds(Base):
    """Exits booked with $0 proceeds are re-read from their transaction."""

    def setUp(self):
        super().setUp()
        self._clear("exit_pnl", "exit_pnl_repaired")

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
        self.assertTrue(any("I re-checked 1 past sale" in m for m in self.said))
        # idempotent: nothing left to repair
        self.assertEqual(execution.repair_zero_proceeds(), [])
        txt = core.pnl_text("30")
        self.assertIn("Realised : $-1.85", txt)
        self.assertIn("corrected from $-10.00", txt)

    def test_two_exits_minutes_apart_each_get_their_own_transaction(self):
        self._clear("exit_pnl", "exit_pnl_repaired")
        state.set_kv("symbol:solana:TWO", "two")
        now = time.time()
        with journal._lock:
            c = journal.conn()
            for ts, cost, tx in ((now - 400, 7.5, "sig75"), (now - 100, 2.5, "sig25")):
                c.execute("INSERT INTO events (ts, kind, asset_id, detail) VALUES (?,?,?,?)",
                          (ts, "exit_pnl", "solana:TWO", json.dumps(
                              {"pnl": -cost, "proceeds": 0.0, "cost": cost, "share": 1.0,
                               "reason": "x"})))
                c.execute("INSERT INTO fills (ts, asset_id, side, qty, price, venue, tx_ref) "
                          "VALUES (?,?,?,?,?,?,?)", (ts + 1, "solana:TWO", "sell", 1, 1,
                                                     "solana", tx))
            c.commit()
        deltas = {"sig75": 9.6, "sig25": 2.2}
        self.patch(solana_dex, "tx_token_delta", lambda sig, mint, owner=None: deltas[sig])
        fixed = execution.repair_zero_proceeds()
        self.assertEqual(sorted(round(n, 2) for _a, _o, n in fixed), [-0.3, 2.1])
        rows = self._exit_rows("solana:TWO")
        self.assertEqual([r["tx"] for r in rows], ["sig75", "sig25"])
        self.assertAlmostEqual(rows[0]["proceeds"], 9.6)
        self.assertAlmostEqual(rows[1]["proceeds"], 2.2)

    def test_an_exit_that_carries_its_transaction_uses_it_and_an_estimate_is_rechecked(self):
        self._clear("exit_pnl")
        state.set_kv("symbol:solana:EST", "est")
        journal.log_event("exit_pnl", "solana:EST", {"pnl": -1.5, "proceeds": 8.5, "cost": 10.0,
                                                     "share": 1.0, "reason": "x",
                                                     "tx": "sigEst", "unmeasured": True})
        self.patch(solana_dex, "tx_token_delta", lambda sig, mint, owner=None: 7.9 if sig == "sigEst" else 0)
        fixed = execution.repair_zero_proceeds()
        self.assertEqual([(a, round(n, 2)) for a, _o, n in fixed], [("solana:EST", -2.1)])
        d = self._exit_rows("solana:EST")[-1]
        self.assertNotIn("unmeasured", d)
        self.assertAlmostEqual(d["proceeds"], 7.9)

    def test_a_corrected_non_loss_lifts_the_stop_out_cooldown(self):
        self._clear("exit_pnl")
        self._seed("solana:RZ5", tx="sigW")
        state.note_stopout("solana:RZ5")
        self.patch(solana_dex, "tx_token_delta", lambda *a, **k: 11.2)
        execution.repair_zero_proceeds()
        self.assertIsNone(state.stopped_out_recently("solana:RZ5"))

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
        self.patch(execution.rugcheck, "check", lambda *a, **k: (True, None, {}))
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
        self.assertEqual(self.not_bought, [])
        self.assertTrue(any("I didn't buy knife" in m for m in self.said))
        self.assertEqual(state.tickets("new"), [x for x in state.tickets("new")
                                                if x["ticket_id"] != t["ticket_id"]])
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


class RugFilter(Base):
    """A stop is a promise the pool has to keep; these are the conditions
    under which it can."""

    def setUp(self):
        super().setUp()
        from tradebot import rugcheck
        self.rc = rugcheck
        self.patch(config, "RUG_MIN_LIQUIDITY_USD", 30000.0)
        self.patch(config, "RUG_MIN_PAIR_AGE_SEC", 3600)
        self.patch(config, "RUG_BLOCKED_DEXES", ["pumpfun"])
        self.patch(config, "RUG_MAX_TOP10_SHARE", 0.40)
        self.patch(config, "RUG_HOLDER_CHECK", True)

    def _info(self, liq=50000, age_min=120, dex="pumpswap"):
        return {"liquidity_usd": liq, "created_ms": (time.time() - age_min * 60) * 1000,
                "dex": dex, "pair_address": "POOL"}

    def test_pair_checks_in_plain_english(self):
        ok, why, _ = self.rc.check_pair(self._info())
        self.assertTrue(ok)
        self.assertEqual(self.rc.check_pair(self._info(liq=8000))[1],
                         "only $8,000 of liquidity (I want $30,000+)")
        self.assertEqual(self.rc.check_pair(self._info(age_min=12))[1],
                         "the pool is only 12 min old (I want 60+)")
        self.assertEqual(self.rc.check_pair(self._info(dex="pumpfun"))[1],
                         "still on the pumpfun bonding curve")
        self.assertEqual(self.rc.check_pair(None)[1], "no pair data")

    def _rpc(self, amounts, owners, supply):
        def rpc(method, params):
            if method == "getTokenLargestAccounts":
                return {"value": [{"address": f"A{i}", "amount": str(a)}
                                  for i, a in enumerate(amounts)]}
            if method == "getTokenSupply":
                return {"value": {"amount": str(supply)}}
            if method == "getMultipleAccounts":
                return {"value": [{"data": {"parsed": {"info": {"owner": o}}}} for o in owners]}
            raise AssertionError(method)
        self.patch(solana_dex, "_rpc", rpc)

    def test_the_pool_vault_is_not_a_holder(self):
        # vault 60% (owned by the pool), then wallets 10%, 8%, 7%, ... of 1000
        self._rpc([600, 100, 80, 70, 50], ["POOL", "w1", "w2", "w3", "w4"], 1000)
        share, m = self.rc.top_holders_share("MINT", "POOL")
        self.assertAlmostEqual(share, 0.30)
        self.assertEqual(m["vaults_excluded"], 1)
        self.assertFalse(m["pool_assumed"])

    def test_an_unrecognised_largest_account_is_assumed_to_be_the_pool(self):
        self._rpc([600, 100, 80], ["x", "w1", "w2"], 1000)
        share, m = self.rc.top_holders_share("MINT", "POOL")
        self.assertAlmostEqual(share, 0.18)
        self.assertTrue(m["pool_assumed"])

    def test_concentrated_supply_is_refused_and_a_blind_read_is_not_a_pass(self):
        self._rpc([500, 450, 30], ["POOL", "dev", "w1"], 1000)
        ok, why, m = self.rc.check("solana", "MINT", self._info())
        self.assertFalse(ok)
        self.assertEqual(why, "the top 10 wallets hold 48% of the supply (I want under 40%)")

        def down(method, params):
            raise RuntimeError("rpc down")
        self.patch(solana_dex, "_rpc", down)
        ok, why, _ = self.rc.check("solana", "MINT", self._info())
        self.assertFalse(ok)
        self.assertEqual(why, "I could not read who holds it")

    def test_base_relies_on_the_pair_checks_alone(self):
        ok, why, _ = self.rc.check("base", "0xabc", self._info())
        self.assertTrue(ok)

    def test_the_buy_gate_refuses_a_ruggable_token_in_plain_words(self):
        state.set_kv("symbol:solana:RUGGY", "ruggy")
        tid = state.add_ticket(asset_id="solana:RUGGY", venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=10.0, ts=time.time())
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.marketdata, "last_info",
                   lambda a, max_age=60: dict(self._info(liq=9000), change_m5=0.0))
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "blocked")
        self.assertTrue(any("I didn't buy ruggy: only $9,000 of liquidity" in m
                            for m in self.said))
        self.assertEqual(journal.query("SELECT status FROM tickets WHERE ticket_id=?",
                                       (tid,))[0]["status"], "blocked:rug_risk")

    def test_research_never_sees_a_ruggable_candidate(self):
        from tradebot.agent import runner
        from tradebot import signals
        self.patch(signals, "collect_all", lambda *a, **k: {})
        self.patch(signals, "candidates", lambda *a, **k: [
            {"asset_id": "solana:THIN", "score": 5}, {"asset_id": "solana:DEEP", "score": 4}])
        self.patch(signals, "features", lambda a: {})
        self.patch(config, "PAID_PROMO_SOURCES", [])
        infos = {"THIN": dict(self._info(liq=6000), price=1.0, volume_h24=1, base_symbol="T"),
                 "DEEP": dict(self._info(), price=1.0, volume_h24=1, base_symbol="D")}
        self.patch(runner.marketdata, "dexscreener_token", lambda c, a: infos[a])
        from tradebot import shorts
        self.patch(shorts, "candidates", lambda *a, **k: [])
        self.patch(runner.marketdata, "coinbase_movers", lambda: [])
        self.patch(runner.marketdata, "ohlcv_dex", lambda *a, **k: [])
        got = [c["address"] for c in runner.gather() if c.get("address")]
        self.assertEqual(got, ["DEEP"])


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
        self.assertIn("Slippage : ~3.8% per trade (paid 2.0% over the price buying, "
                      "got 1.8% under it selling)", txt)


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

    def _run(self, asset, start, samples, action="BUY_NOW", near_end=True):
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 12 * 3600)
        fid = journal.log_forecast({"asset_id": asset, "action": action, "p30": 0.5})
        calibration.open_tracking(fid, asset, action, start)
        if near_end:
            # the call was made just under six hours ago: the samples land at
            # the end of the window, where a 'flat' result is actually observed
            with journal._lock:
                journal.conn().execute("UPDATE forecast_tracking SET start_ts=start_ts-? "
                                       "WHERE forecast_id=?",
                                       (calibration.config.P30_WINDOW_SEC - 120, fid))
                journal.conn().commit()
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
        self.assertAlmostEqual(o["sim_return"], -0.19)   # -15% and the 4% round trip
        txt = calibration.scorecard(1)
        self.assertIn("stopped 100%", txt)
        self.assertIn("$-1.90 each after costs", txt)

    def test_the_scorecard_buckets_the_sim_by_stated_p30(self):
        self.patch(calibration.config, "BUY_P30_MIN", 0.35)
        self._run("solana:B1", 1.0, [1.31])                    # p30 0.5 in _run
        txt = calibration.scorecard(1)
        self.assertIn("By the p30 the model stated (buy bar is 0.35):", txt)
        self.assertIn("p30~0.5 n=1: won 100%, stopped 0% -> $+2.60 per $10", txt)

    def test_the_target_printing_first_is_a_win(self):
        o = self._run("solana:SIM2", 1.0, [1.31, 0.8])
        self.assertEqual(o["sim_result"], "target")
        self.assertAlmostEqual(o["sim_return"], 0.26)
        fb = calibration.feedback()["BUY_NOW"]
        self.assertAlmostEqual(fb["target_before_stop_share"], 1.0)
        self.assertAlmostEqual(fb["stopped_first_share"], 0.0)
        self.assertAlmostEqual(fb["sim_pnl_per_10usd"], 2.6)

    def test_neither_is_the_move_at_the_end_of_the_window(self):
        o = self._run("solana:SIM3", 1.0, [1.05, 1.10])
        self.assertEqual(o["sim_result"], "flat")
        self.assertAlmostEqual(o["sim_return"], 0.06)

    def test_an_unwatched_end_of_window_is_not_a_flat_result(self):
        o = self._run("solana:GAP", 1.0, [1.16], near_end=False)   # one print at +0, then silence
        self.assertIsNone(o["sim_result"])
        self.assertIsNone(o["sim_return"])

    def test_a_print_after_the_horizon_is_not_part_of_the_record(self):
        fid = journal.log_forecast({"asset_id": "solana:LATE", "action": "PASS", "p30": 0.1})
        calibration.open_tracking(fid, "solana:LATE", "PASS", 1.0)
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:LATE": 1.0}, True))
        calibration.tick()
        with journal._lock:                                  # tracker was down for a day
            journal.conn().execute("UPDATE forecast_tracking SET start_ts=start_ts-86400")
            journal.conn().commit()
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:LATE": 3.0}, True))
        calibration.tick()                                   # 3x, 24h later: not counted
        o = journal.query("SELECT * FROM outcomes WHERE forecast_id=?", (fid,))[0]
        self.assertEqual(o["hit_2x"], 0)
        self.assertAlmostEqual(o["max_multiple"], 1.0)

    def test_the_grid_plays_every_stop_target_pair_on_the_same_prints(self):
        self.patch(calibration.config, "SIM_STOPS", [0.10, 0.15, 0.20, 0.25])
        self.patch(calibration.config, "SIM_TARGETS", [0.15, 0.20, 0.30, 0.50])
        # -12%, then +16%, then +31%: -10% stops out before +15%; -15% never prints
        o = self._run("solana:GRID", 1.0, [0.88, 1.16, 1.31])
        g = json.loads(o["sim_grid"])
        self.assertEqual(g["10/15"][0], "stop")
        self.assertAlmostEqual(g["10/15"][1], -0.14)          # -10% and the 4% round trip
        self.assertEqual(g["15/15"][0], "target")
        self.assertAlmostEqual(g["15/15"][1], 0.11)
        self.assertEqual(g["15/30"][0], "target")
        self.assertAlmostEqual(g["15/30"][1], 0.26)
        self.assertEqual(g["25/50"][0], "flat")                # neither: the window's last print
        self.assertAlmostEqual(g["25/50"][1], 0.27)
        txt = calibration.scorecard(1)
        self.assertIn("$ per $10 by stop/target, every token call (n=1-1):", txt)
        self.assertIn("token calls with p30 >= 0.25", txt)
        row15 = [ln for ln in txt.splitlines() if ln.strip().startswith("-15% ")][0]
        self.assertIn("$+1.10", row15)                          # 15/15
        self.assertIn("$+2.60", row15)                          # 15/30

    def test_a_row_observed_before_the_grid_existed_gets_no_grid(self):
        fid = journal.log_forecast({"asset_id": "solana:PREGRID", "action": "PASS", "p30": 0.2})
        calibration.open_tracking(fid, "solana:PREGRID", "PASS", 1.0)
        with journal._lock:      # the old tracker saw -16% at hour one; no crosses column then
            journal.conn().execute("UPDATE forecast_tracking SET max_6h=1.0, min_6h=0.84, "
                                   "samples=2, stop_ts=start_ts+3600, start_ts=start_ts-? "
                                   "WHERE forecast_id=?", (calibration.config.P30_WINDOW_SEC - 120, fid))
            journal.conn().commit()
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:PREGRID": 1.05}, True))
        calibration.tick()
        self.assertIsNone(journal.query("SELECT crosses FROM forecast_tracking WHERE forecast_id=?",
                                        (fid,))[0]["crosses"])
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        o = journal.query("SELECT * FROM outcomes WHERE forecast_id=?", (fid,))[0]
        self.assertEqual(o["sim_result"], "stop")           # the legacy sim still knows
        self.assertIsNone(o["sim_grid"])                     # the grid does not pretend to

    def test_a_level_added_later_is_not_scored_on_rows_that_never_watched_it(self):
        self.patch(calibration.config, "SIM_STOPS", [0.10])
        self.patch(calibration.config, "SIM_TARGETS", [0.15])
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 12 * 3600)
        fid = journal.log_forecast({"asset_id": "solana:WIDEN", "action": "PASS", "p30": 0.2})
        calibration.open_tracking(fid, "solana:WIDEN", "PASS", 1.0)
        with journal._lock:
            journal.conn().execute("UPDATE forecast_tracking SET start_ts=start_ts-? WHERE forecast_id=?",
                                   (calibration.config.P30_WINDOW_SEC - 120, fid))
            journal.conn().commit()
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:WIDEN": 0.75}, True))
        calibration.tick()                                   # -25% printed under the narrow list
        self.patch(calibration.config, "SIM_STOPS", [0.10, 0.15, 0.20, 0.25])   # operator widens
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:WIDEN": 1.05}, True))
        calibration.tick()
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        g = json.loads(journal.query("SELECT sim_grid FROM outcomes WHERE forecast_id=?",
                                     (fid,))[0]["sim_grid"])
        self.assertEqual(sorted(g), ["10/15"])               # only the pair it watched
        self.assertEqual(g["10/15"][0], "stop")

    def test_levels_keep_lossless_labels(self):
        self.patch(calibration.config, "SIM_STOPS", [0.12, 0.125])
        self.patch(calibration.config, "SIM_TARGETS", [0.15])
        o = self._run("solana:HALF", 1.0, [0.878, 1.16])     # -12.2%: past 12, not past 12.5
        g = json.loads(o["sim_grid"])
        self.assertEqual(g["12/15"][0], "stop")
        self.assertEqual(g["12.5/15"][0], "target")
        self.assertIn("-12.5%", calibration.scorecard(1))

    def test_the_perp_grid_is_mirrored_and_kept_apart(self):
        self.patch(calibration.config, "HL_TARGET", 0.08)
        self.patch(calibration.config, "HL_STOP_PCT", 0.04)
        self.patch(calibration.config, "HL_FEE_RATE", 0.00045)
        self.patch(calibration.config, "SIM_STOPS_PERP", [0.02, 0.04, 0.06, 0.08])
        self.patch(calibration.config, "SIM_TARGETS_PERP", [0.04, 0.06, 0.08, 0.12])
        o = self._run("perp:PGRID", 100.0, [105.0, 91.0], action="SHORT_NOW")
        g = json.loads(o["sim_grid"])
        self.assertEqual(g["4/8"][0], "stop")                  # +5% printed before -9%
        self.assertAlmostEqual(g["4/8"][1], -0.0409)
        self.assertEqual(g["6/8"][0], "target")
        self.assertAlmostEqual(g["6/8"][1], 0.0791)
        txt = calibration.scorecard(1)
        self.assertIn("perp calls (short) (n=1-1):", txt)
        self.assertNotIn("every token call", txt)
        block = txt[txt.index("perp calls (short)"):]
        self.assertIn("-4%", block.splitlines()[1])          # a short's target: price DOWN
        self.assertTrue(block.splitlines()[2].strip().startswith("+2%"))   # its stop: price UP

    def test_a_short_thesis_is_mirrored(self):
        self.patch(calibration.config, "HL_TARGET", 0.08)
        self.patch(calibration.config, "HL_STOP_PCT", 0.04)
        self.patch(calibration.config, "HL_FEE_RATE", 0.00045)
        o = self._run("perp:SIMX", 100.0, [105.0, 90.0], action="SHORT_NOW")
        self.assertEqual(o["sim_result"], "stop")
        self.assertAlmostEqual(o["sim_return"], -0.0409)   # perp fees, not the DEX 4%
        o = self._run("perp:SIMY", 100.0, [91.0], action="SHORT_NOW")
        self.assertEqual(o["sim_result"], "target")
        self.assertAlmostEqual(o["sim_return"], 0.0791)
        self.assertIn("SHORT_NOW/perp", calibration.scorecard(1))

    def test_a_row_the_new_tracker_never_saw_in_window_has_no_sim(self):
        fid = journal.log_forecast({"asset_id": "solana:LEGACY", "action": "BUY_NOW", "p30": 0.5})
        calibration.open_tracking(fid, "solana:LEGACY", "BUY_NOW", 1.0)
        with journal._lock:      # what the old tracker left: sampled, +40%, no new columns
            journal.conn().execute("UPDATE forecast_tracking SET max_6h=1.4, samples=20, "
                                   "start_ts=start_ts-7*3600 WHERE forecast_id=?", (fid,))
            journal.conn().commit()
        self.patch(calibration.marketdata, "marks", lambda a: ({"solana:LEGACY": 1.2}, True))
        calibration.tick()
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        o = journal.query("SELECT * FROM outcomes WHERE forecast_id=?", (fid,))[0]
        self.assertEqual(o["hit_30"], 1)
        self.assertIsNone(o["sim_result"])
        self.assertIsNone(o["sim_return"])
        self.assertNotIn("sim_pnl_per_10usd", calibration.feedback().get("BUY_NOW", {}))

    def test_a_perp_pass_is_not_averaged_with_token_passes(self):
        self.patch(calibration.config, "HL_TARGET", 0.08)
        self.patch(calibration.config, "HL_STOP_PCT", 0.04)
        self._run("perp:PP", 100.0, [91.0], action="PASS")     # fell 9%: a short win
        self._run("solana:TP", 1.0, [0.91], action="PASS")    # fell 9%: a long loss
        fb = calibration.feedback()
        self.assertAlmostEqual(fb["PASS/perp"]["target_before_stop_share"], 1.0)
        self.assertAlmostEqual(fb["PASS"]["target_before_stop_share"], 0.0)


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

    def test_a_failed_lookup_is_not_remembered_as_the_name(self):
        from tradebot import alerts, marketdata
        alerts._symbol_cache.pop("solana:FLAKY456", None)
        answers = iter([RuntimeError("502"), {"base_symbol": "FLK"}])

        def lookup(c, a):
            v = next(answers)
            if isinstance(v, Exception):
                raise v
            return v
        self.patch(marketdata, "dexscreener_token", lookup)
        self.assertEqual(alerts.symbol("solana:FLAKY456"), "FLAKY4")   # this time
        self.assertIsNone(state.get_kv("symbol:solana:FLAKY456"))
        self.assertEqual(alerts.symbol("solana:FLAKY456"), "FLK")      # next time
        self.assertEqual(state.get_kv("symbol:solana:FLAKY456"), "FLK")


class MigrationRace(Base):
    def test_a_column_the_other_process_just_added_is_not_a_crash(self):
        from tradebot import journal as _j
        orig = _j.query
        self.patch(_j, "query", lambda sql, args=(): [] if sql.startswith("PRAGMA") else orig(sql, args))
        state._migrate()          # every ALTER now hits an existing column


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
