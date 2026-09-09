"""The ratchet on synthetic price paths: the four worked examples from the
design review, plus the live-mode hooks. Ticks are 30s apart; each entry in
a path is (minute, price, buys5, sells5) and holds until the next entry."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import config, execution, journal, ratchet, state  # noqa: E402

T0 = 1_800_000_000.0        # a fixed "entry" instant


class Base(unittest.TestCase):
    def setUp(self):
        state.init()
        for p in state.positions():
            state.close_position(p["asset_id"])
        with journal._lock:
            journal.conn().execute("DELETE FROM price_bars")
            journal.conn().execute("DELETE FROM ratchet_track")
            journal.conn().execute("DELETE FROM events WHERE kind LIKE 'ratchet%'")
            journal.conn().commit()
        self.patches = []
        self.patch(config, "RATCHET_MODE", "shadow")
        self.sold = []
        self.patch(execution, "execute_sell",
                   lambda a, r, f=1.0: self.sold.append((a, r, f)) or "filled")

    def patch(self, mod, name, value):
        self.patches.append((mod, name, getattr(mod, name, None)))
        setattr(mod, name, value)

    def tearDown(self):
        for mod, name, old in reversed(self.patches):
            setattr(mod, name, old)

    def position(self, asset="solana:RatchetMint", entry=100.0, qty=10.0):
        state.upsert_position(asset, "solana", "solana", qty, entry * qty, invalidation=60.0)
        with journal._lock:                      # pin entry_ts to T0
            journal.conn().execute("UPDATE positions SET entry_ts=? WHERE asset_id=?", (T0, asset))
            journal.conn().commit()
        ratchet.clear(asset)
        return state.get_position(asset)

    def drive(self, p, path, until_minute=None):
        """Drive on_tick along a path. Returns list of (minute, fired)."""
        fired = []
        last = until_minute if until_minute is not None else path[-1][0] + 1
        i = 0
        t = path[0][0] * 60.0
        while t < last * 60:
            while i + 1 < len(path) and path[i + 1][0] * 60 <= t:
                i += 1
            _m, price, b, s = path[i]
            q = {"price": price, "fresh": True, "ts": T0 + t, "liquidity_usd": 20000,
                 "buys_m5": b, "sells_m5": s}
            p = state.get_position(p["asset_id"]) or p
            r = ratchet.on_tick(p, q, T0 + t)
            if r:
                fired.append((t / 60, r))
            t += 30
        return fired

    def st(self, asset="solana:RatchetMint"):
        return ratchet.load(asset)


class Arming(Base):
    def test_sniper_prints_inside_one_minute_do_not_arm(self):
        """Example C: 128/133/129 prints then a dump to 95, all within 60s."""
        p = self.position()
        path = [(0, 100, 5, 2), (10, 128, 6, 1), (10.25, 133, 6, 1), (10.5, 102, 2, 8),
                (11, 95, 2, 8), (12, 96, 3, 3)]
        self.assertEqual(self.drive(p, path, until_minute=20), [])
        self.assertFalse(self.st()["armed"])
        self.assertLess(self.st()["hwm"], 120)          # closes never held +20%

    def test_three_held_closes_arm_and_raise_nothing_in_shadow(self):
        p = self.position()
        path = [(0, 100, 5, 2), (60, 121, 12, 4), (61, 122, 12, 4), (62, 120, 12, 4), (63, 121, 12, 4)]
        self.drive(p, path, until_minute=66)
        st = self.st()
        self.assertTrue(st["armed"])
        self.assertGreaterEqual(st["floor"], 103.0)
        self.assertAlmostEqual(st["budget_qty"], 7.5)      # 75% of 10
        self.assertEqual(self.sold, [])
        self.assertEqual(state.get_position(p["asset_id"])["invalidation_price"], 60.0)

    def test_whipsaw_chop_does_not_arm_and_a_pullback_after_arm_does_not_sell(self):
        """Example B: 95-115 for 3h, then holds 120, pulls back to 108."""
        p = self.position()
        chop = [(m, 95 + (m % 7) * 3, 6, 5) for m in range(0, 180, 5)]
        path = chop + [(180, 121, 10, 4), (181, 122, 10, 4), (182, 121, 10, 4), (183, 121, 10, 4),
                       (240, 109, 8, 6), (241, 108, 8, 6), (242, 110, 9, 5)]
        fired = self.drive(p, path, until_minute=245)
        st = self.st()
        self.assertTrue(st["armed"])
        self.assertEqual([f for f in fired], [])
        self.assertAlmostEqual(st["floor"], 103.0)      # the chop's sigma keeps the floor at breakeven

    def test_low_volatility_grind_lifts_the_floor_above_breakeven(self):
        p = self.position()
        grind = [(m, 100 + m * 0.5, 9, 4) for m in range(0, 80)]     # +0.5%/min, steady
        self.drive(p, grind, until_minute=80)
        st = self.st()
        self.assertTrue(st["armed"])
        self.assertGreater(st["floor"], 110)            # trail follows the peak up

    def test_a_feed_gap_breaks_the_dwell(self):
        p = self.position()
        path = [(0, 100, 5, 2), (11, 125, 8, 3)]
        self.drive(p, path, until_minute=12)            # one close at 125
        later = [(40, 126, 8, 3), (41, 127, 8, 3)]
        self.drive(p, later, until_minute=43)           # two more after a 28-min gap
        self.assertFalse(self.st()["armed"])


class Triggers(Base):
    def test_spike_and_dump_takes_on_the_turn(self):
        """Example D: closes 120/138/145, peak 150, 142 with the last 5 min down."""
        p = self.position()
        path = [(0, 100, 8, 3), (5, 120, 14, 3), (6, 138, 16, 3), (7, 145, 16, 4),
                (8, 145, 12, 6), (11, 150, 10, 8), (13, 142, 6, 12)]
        fired = self.drive(p, path, until_minute=14)
        self.assertTrue(fired and fired[-1][1] == "take", fired)
        row = journal.query("SELECT detail FROM events WHERE kind='ratchet_would_sell'")[-1]
        d = json.loads(row["detail"])
        self.assertGreaterEqual(d["price"], 142)         # on the turn, not on the way down
        self.assertLessEqual(d["price"], 150)
        rows = journal.query("SELECT * FROM ratchet_track")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trigger"], "take")
        self.assertAlmostEqual(rows[0]["fraction"], 0.75)
        self.assertTrue(self.st()["done"])
        self.assertEqual(self.sold, [])                 # shadow: no order

    def test_floor_breach_needs_two_fresh_ticks(self):
        p = self.position()
        path = [(0, 100, 8, 3), (60, 130, 12, 4), (61, 131, 12, 4), (62, 130, 12, 4), (63, 131, 12, 4),
                (90, 160, 12, 4), (91, 162, 12, 4), (92, 160, 12, 4)]
        self.drive(p, path, until_minute=95)
        st = self.st()
        self.assertTrue(st["armed"])
        floor = st["floor"]
        self.assertGreater(floor, 103)
        p = state.get_position(p["asset_id"])
        below = {"price": floor * 0.98, "fresh": True, "ts": 0, "buys_m5": 5, "sells_m5": 5}
        self.assertIsNone(ratchet.on_tick(p, below, T0 + 95 * 60))
        self.assertEqual(ratchet.on_tick(p, below, T0 + 95 * 60 + 30), "floor")

    def test_stall_sells_when_cold_and_a_fresh_hold_suppresses_it(self):
        p = self.position()
        arm = [(0, 100, 8, 3), (30, 125, 12, 4), (31, 126, 12, 4), (32, 125, 12, 4), (33, 125, 12, 4)]
        self.drive(p, arm, until_minute=35)
        self.assertTrue(self.st()["armed"])
        # no new peak, sellers > buyers, 40 min later (allow = clamp(10*age_h,20,120) ~ 20 min)
        cold = [(35, 124, 3, 9)]
        fired = self.drive(p, cold, until_minute=75)
        self.assertIn("stall", [f[1] for f in fired])
        # again, with a fresh HOLD at p2x 0.4: suppressed
        p2 = self.position(asset="solana:HoldMint")
        self.drive(p2, arm, until_minute=35)
        journal.log_forecast({"asset_id": "solana:HoldMint", "action": "HOLD", "p2x": 0.4,
                              "ts": T0 + 50 * 60})
        from tradebot import signals
        self.patch(signals, "features", lambda a: {"accel": 1.0})
        fired = self.drive(p2, cold, until_minute=75)
        self.assertNotIn("stall", [f[1] for f in fired])
        # the same HOLD, stale (older than 35 min at the check) -> no suppression
        p3 = self.position(asset="solana:StaleHold")
        self.drive(p3, arm, until_minute=35)
        journal.log_forecast({"asset_id": "solana:StaleHold", "action": "HOLD", "p2x": 0.4,
                              "ts": T0 - 3600})
        fired = self.drive(p3, cold, until_minute=75)
        self.assertIn("stall", [f[1] for f in fired])


class LiveMode(Base):
    def test_live_arm_raises_stop_and_floor_breach_sells_the_share(self):
        self.patch(config, "RATCHET_MODE", "live")
        p = self.position()
        path = [(0, 100, 8, 3), (60, 130, 12, 4), (61, 131, 12, 4), (62, 130, 12, 4), (63, 131, 12, 4)]
        self.drive(p, path, until_minute=66)
        p = state.get_position(p["asset_id"])
        self.assertAlmostEqual(p["invalidation_price"], 103.0)
        self.assertTrue(ratchet.is_armed(p["asset_id"]))
        below = {"price": 100.5, "fresh": True, "ts": 0, "buys_m5": 5, "sells_m5": 5}
        ratchet.on_tick(p, below, T0 + 70 * 60)
        self.assertEqual(ratchet.on_tick(p, below, T0 + 70 * 60 + 30), "floor")
        self.assertEqual(len(self.sold), 1)
        self.assertAlmostEqual(self.sold[0][2], 0.75)
        self.assertIn("ratchet floor", self.sold[0][1])

    def test_winner_close_pauses_reentry_and_the_gate_honours_it(self):
        self.patch(config, "RATCHET_MODE", "live")
        p = self.position()
        path = [(0, 100, 8, 3), (60, 130, 12, 4), (61, 131, 12, 4), (62, 130, 12, 4), (63, 131, 12, 4)]
        self.drive(p, path, until_minute=66)
        ratchet.on_close(p["asset_id"], 1.25)
        self.assertIsNotNone(ratchet.reentry_paused(p["asset_id"]))
        self.assertIsNone(ratchet.load(p["asset_id"]))
        from tradebot import approval
        self.patch(execution, "_run_gates", lambda t, v, f: 1.0)
        asked = []
        self.patch(approval, "request_buy_approval", lambda t, pr, f: asked.append(t))
        tid = state.add_ticket(asset_id=p["asset_id"], venue="solana", chain="solana",
                               action="BUY_NOW", notional_usd=10.0)
        t = [x for x in state.tickets("new") if x["ticket_id"] == tid][0]
        self.assertEqual(execution.process_ticket(t, 100.0, True), "blocked")
        self.assertEqual(asked, [])

    def test_loser_close_does_not_pause(self):
        self.patch(config, "RATCHET_MODE", "live")
        p = self.position()
        ratchet.on_close(p["asset_id"], -0.5)
        self.assertIsNone(ratchet.reentry_paused(p["asset_id"]))


class LifePnl(Base):
    def test_close_after_a_partial_does_not_double_count(self):
        """Plan leg +$0.08, then close at -$0.07: a +$0.01 life is a win."""
        from tradebot import marketdata
        from tradebot.exchanges import coinbase
        coinbase._products["LIFE-USDC"] = {"quote_increment": "0.01", "base_increment": "0.00000001"}
        self.addCleanup(coinbase._products.clear)
        state.upsert_position("cex:LIFE-USDC", "coinbase", None, 2.0, 4.0)
        state.whitelist_add("cex:LIFE-USDC", "coinbase")
        state.set_cash("coinbase", 100.0)
        self.patch(marketdata, "price", lambda a: 2.0)
        self.patch(coinbase, "market_sell", lambda p, q: ("srv-l", {}))
        fills = iter([("1", "2.08", "0", "2.08"), ("1", "1.93", "0", "1.93")])

        def status(o):
            size, avg, fee, val = next(fills)
            return {"status": "FILLED", "filled_size": size, "average_filled_price": avg,
                    "total_fees": fee, "filled_value": val, "order_id": "srv-l"}
        self.patch(coinbase, "order_status", status)
        for mod, name, old in self.patches:      # this test needs the real execute_sell
            if name == "execute_sell":
                setattr(mod, name, old)
        real_sell = execution.execute_sell
        self.assertEqual(real_sell("cex:LIFE-USDC", "leg", 0.5), "filled")     # +0.08
        self.assertEqual(real_sell("cex:LIFE-USDC", "close", 1.0), "filled")   # -0.07
        self.assertTrue(state.is_whitelisted("cex:LIFE-USDC"))                # net +0.01: not a loss
        self.assertIsNone(state.stopped_out_recently("cex:LIFE-USDC"))


class Report(Base):
    def test_report_scores_resolved_counterfactuals(self):
        now = time.time()
        with journal._lock:
            c = journal.conn()
            # sold at 1.22x, ran to 1.30x after; actual life realised 1.10x
            c.execute("INSERT INTO ratchet_track (asset_id, entry_ts, entry_price, armed_ts, trigger, "
                      "shadow_price, shadow_ts, fraction, hwm, floor, sigma15, max_after, last_ts, resolved) "
                      "VALUES ('solana:R1', ?, 100, ?, 'floor', 122, ?, 0.75, 130, 120, 0.1, 130, ?, 1)",
                      (now - 80 * 3600, now - 79 * 3600, now - 78 * 3600, now))
            # sold early at 1.10x, then 2.5x within 72h: the tail case
            c.execute("INSERT INTO ratchet_track (asset_id, entry_ts, entry_price, armed_ts, trigger, "
                      "shadow_price, shadow_ts, fraction, hwm, floor, sigma15, max_after, last_ts, resolved) "
                      "VALUES ('solana:R2', ?, 100, ?, 'stall', 110, ?, 0.75, 125, 108, 0.1, 250, ?, 1)",
                      (now - 80 * 3600, now - 79 * 3600, now - 78 * 3600, now))
            c.commit()
        for a, mult in (("solana:R1", 1.10), ("solana:R2", 2.0)):
            journal.log_event("exit_pnl", a, {"pnl": (mult - 1) * 10, "proceeds": mult * 10,
                                              "cost": 10.0, "share": 1.0, "reason": "t"})
        txt = ratchet.report_text(30)
        self.assertIn("would-sell 2", txt)
        self.assertIn("gate1", txt)
        self.assertIn("1/2", txt)                 # one early sell that then 2x'd
        self.assertIn("gate2 fail", txt)

    def test_bars_prune(self):
        with journal._lock:
            journal.conn().execute("INSERT INTO price_bars VALUES ('x', 1, 1.0, 0, 0, 0)")
            journal.conn().commit()
        ratchet.prune_bars()
        self.assertEqual(journal.query("SELECT COUNT(*) n FROM price_bars")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
