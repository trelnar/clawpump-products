"""The short leg against a fake Hyperliquid: open, stop, mirrored ratchet,
cover accounting, approval/AUTO routing, discovery shape, calibration on the
low. No network; the SDK is never imported."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import alerts, config, journal, shorts, state  # noqa: E402
from tradebot.exchanges import hyperliquid as hl  # noqa: E402


class FakeHL:
    def __init__(self):
        self.mid_px = {"ETH": 2600.0, "WIF": 2.0}
        self.opened, self.closed = [], []
        self.value = 100.0

    def mids(self):
        return dict(self.mid_px)

    def mid(self, coin):
        return self.mid_px.get(coin)

    def account_value(self):
        return self.value

    def round_size(self, coin, sz):
        return round(sz, 4)

    def open_short(self, coin, notional):
        px = self.mid_px[coin]
        sz = round(notional / px, 4)
        self.opened.append((coin, sz, px))
        return sz, px, 1

    def close_short(self, coin, sz=None):
        px = self.mid_px[coin]
        self.closed.append((coin, sz, px))
        return (sz if sz is not None else self._open_sz(coin)), px, 2

    def _open_sz(self, coin):
        r = shorts.get(f"perp:{coin}")
        return r["qty"] if r else 0.0

    def positions(self):
        # mirror the book unless a test overrides self.live
        if getattr(self, "live", None) is not None:
            return self.live
        return {r["coin"]: {"size": -r["qty"], "entry": r["entry_price"], "upnl": 0.0}
                for r in shorts.open_positions()}

    def contexts(self):
        return {"ETH": {"mark": 2600.0, "prev_day": 2200.0, "volume_24h": 5e8, "funding": 0.0001,
                        "open_interest": 1e8, "sz_decimals": 4},
                "DUD": {"mark": 1.0, "prev_day": 0.99, "volume_24h": 5e6, "funding": 0, "open_interest": 1, "sz_decimals": 1},
                "THIN": {"mark": 3.0, "prev_day": 2.0, "volume_24h": 1000, "funding": 0, "open_interest": 1, "sz_decimals": 1}}

    def candles(self, coin, interval="1h", hours=8):
        base = 2200.0
        return [{"t": i, "o": base, "h": base * 1.2, "l": base, "c": base + i * 60, "v": 1} for i in range(8)]


class Base(unittest.TestCase):
    def setUp(self):
        state.init()
        with journal._lock:
            journal.conn().execute("DELETE FROM shorts")
            journal.conn().execute("DELETE FROM events WHERE kind IN ('exit_pnl','short_stop','short_ratchet_armed')")
            journal.conn().commit()
        self.patches = []
        self.fake = FakeHL()
        for name in ("mids", "mid", "account_value", "round_size", "open_short", "close_short",
                     "contexts", "candles", "positions"):
            self.patch(hl, name, getattr(self.fake, name))
        self.patch(config, "SHORTS_ENABLED", True)
        state.set_mode("NORMAL", reason="test")     # a fresh DB cold-starts SELL_ONLY
        for coin in ("ETH", "WIF"):                 # earlier tests leave cooldowns/approvals behind
            state.set_kv(f"stopout:perp:{coin}", "")
            state.set_kv(f"ratchet_exit:perp:{coin}", "")
            state.whitelist_revoke(f"perp:{coin}")
        with journal._lock:
            journal.conn().execute("DELETE FROM pending_approvals WHERE asset_id LIKE 'perp:%'")
            journal.conn().commit()
        self.out = []
        self.patch(alerts, "_send_fn", lambda body, buttons=None: self.out.append(body) or True)
        state.set_auto_approve(0)

    def patch(self, mod, name, value):
        self.patches.append((mod, name, getattr(mod, name, None)))
        setattr(mod, name, value)

    def tearDown(self):
        for mod, name, old in reversed(self.patches):
            setattr(mod, name, old)

    def ticket(self, coin="ETH"):
        tid = state.add_ticket(asset_id=f"perp:{coin}", venue="hyperliquid", chain=None,
                               action="SHORT_NOW", notional_usd=10.0, detail=f"{coin} fading")
        return [t for t in state.tickets("new") if t["ticket_id"] == tid][0]


class OpenAndCover(Base):
    def test_auto_mode_opens_and_books_a_short(self):
        state.set_auto_approve(1)
        self.assertEqual(shorts.process_ticket(self.ticket()), "filled")
        r = shorts.get("perp:ETH")
        self.assertAlmostEqual(r["entry_price"], 2600.0)
        self.assertAlmostEqual(r["stop_price"], 2600 * 1.04)
        self.assertIn("I shorted ETH for $", self.out[-1])
        self.assertIn("stop at 2704", self.out[-1])

    def test_without_auto_it_asks_and_yes_queues_it(self):
        from tradebot import approval
        t = self.ticket()
        self.assertEqual(shorts.process_ticket(t), "awaiting_approval")
        self.assertIsNone(shorts.get("perp:ETH"))
        code = journal.query("SELECT code FROM pending_approvals WHERE kind='short' ORDER BY ts DESC LIMIT 1")[0]["code"]
        queued = []
        c = approval.Commands(lambda p: queued.append(p["ticket_id"]), lambda: None,
                              lambda: "", lambda a: "", lambda a: "")
        c.handle(f"YES {code}")
        self.assertEqual(queued, [t["ticket_id"]])
        self.assertTrue(state.is_whitelisted("perp:ETH"))

    def test_margin_short_is_blocked_with_a_plain_message(self):
        state.set_auto_approve(1)
        self.fake.value = 5.0
        self.assertEqual(shorts.process_ticket(self.ticket()), "blocked")
        self.assertIn("Not shorting ETH", self.out[-1])

    def test_stop_covers_everything_at_a_loss(self):
        state.set_auto_approve(1)
        shorts.process_ticket(self.ticket())
        self.fake.mid_px["ETH"] = 2600 * 1.05
        shorts.monitor()
        self.assertIsNone(shorts.get("perp:ETH"))
        self.assertIn("for a loss of $", self.out[-1])
        self.assertIn("because it hit the stop", self.out[-1])
        e = journal.query("SELECT detail FROM events WHERE kind='exit_pnl' ORDER BY ts DESC LIMIT 1")[0]
        self.assertLess(json.loads(e["detail"])["pnl"], 0)
        self.assertIsNotNone(state.stopped_out_recently("perp:ETH"))

    def test_mirrored_ratchet_arms_on_the_way_down_and_covers_on_the_bounce(self):
        state.set_auto_approve(1)
        shorts.process_ticket(self.ticket())
        t0 = time.time()
        # three 1-min closes at -6%, then a bounce
        for i, px in enumerate([2600 * 0.94, 2600 * 0.94, 2600 * 0.94, 2600 * 0.94]):
            self.fake.mid_px["ETH"] = px
            shorts.monitor(t0 + i * 60)
        r = shorts.get("perp:ETH")
        rs = json.loads(r["ratchet"])
        self.assertTrue(rs["armed"])
        self.assertLess(r["stop_price"], 2600)           # stop moved inside profit
        self.fake.mid_px["ETH"] = 2600 * 0.94 * 1.04      # +4% bounce off the low, above a 3% ceiling
        shorts.monitor(t0 + 5 * 60)
        shorts.monitor(t0 + 5 * 60 + 10)
        self.assertEqual(len(self.fake.closed), 1)
        self.assertRegex(self.out[-1], r"I covered 7\d% of my ETH short for a gain of \$")
        self.assertIsNotNone(shorts.get("perp:ETH"))      # 25% still on

    def test_max_hold_covers(self):
        state.set_auto_approve(1)
        shorts.process_ticket(self.ticket())
        shorts.monitor(time.time() + config.HL_MAX_HOLD_SEC + 1)
        self.assertIsNone(shorts.get("perp:ETH"))
        self.assertIn("thesis window ran out", self.out[-1])


class RatchetAndBook(Base):
    def _open(self):
        state.set_auto_approve(1)
        shorts.process_ticket(self.ticket())

    def _arm(self, t0):
        for i in range(4):
            self.fake.mid_px["ETH"] = 2600 * 0.94
            shorts.monitor(t0 + i * 60)
        r = shorts.get("perp:ETH")
        self.assertTrue(json.loads(r["ratchet"])["armed"])
        return r

    def test_ceiling_sits_between_the_low_and_the_stop_and_a_further_fall_does_not_cover(self):
        self._open()
        t0 = time.time()
        r = self._arm(t0)
        rs = json.loads(r["ratchet"])
        low = r["lwm"]
        self.assertGreater(rs["ceiling"], low)
        self.assertLessEqual(rs["ceiling"], r["stop_price"])
        self.assertLess(r["stop_price"], 2600)
        for i in range(5, 12):                       # keeps falling: no cover
            self.fake.mid_px["ETH"] = 2600 * (0.94 - 0.005 * i)
            shorts.monitor(t0 + i * 60)
        self.assertEqual(self.fake.closed, [])
        r = shorts.get("perp:ETH")
        self.assertLess(r["lwm"], low)               # low water mark followed it down

    def test_cover_failure_keeps_the_ratchet_budget(self):
        self._open()
        t0 = time.time()
        self._arm(t0)

        def boom(coin, sz=None):
            raise RuntimeError("IoC not filled")
        self.patch(hl, "close_short", boom)
        self.fake.mid_px["ETH"] = 2600 * 0.94 * 1.05
        shorts.monitor(t0 + 5 * 60)
        shorts.monitor(t0 + 5 * 60 + 10)
        rs = json.loads(shorts.get("perp:ETH")["ratchet"])
        self.assertGreater(rs["budget"], 0)
        self.assertFalse(rs["done"])
        self.assertIn("COVER FAILED", self.out[-1])

    def test_partial_cover_arithmetic_and_no_double_count_at_close(self):
        self._open()
        r0 = shorts.get("perp:ETH")
        self.fake.mid_px["ETH"] = 2600 * 0.95
        self.assertEqual(shorts.cover("perp:ETH", 0.5, "ratchet floor at x"), "filled")
        r1 = shorts.get("perp:ETH")
        self.assertAlmostEqual(r1["qty"], r0["qty"] - round(r0["qty"] * 0.5, 4), places=6)
        self.assertAlmostEqual(r1["notional_usd"], r1["qty"] * r0["entry_price"], places=6)
        self.fake.mid_px["ETH"] = 2600 * 1.01          # give a little back at the end
        shorts.cover("perp:ETH", 1.0, "stop x hit")
        self.assertIsNone(shorts.get("perp:ETH"))
        rows = journal.query("SELECT detail FROM events WHERE kind='exit_pnl' AND asset_id='perp:ETH'")
        total = sum(json.loads(x["detail"])["pnl"] for x in rows)
        self.assertGreater(total, 0)                    # +5% on half beats -1% on half
        self.assertTrue(state.is_whitelisted("perp:ETH"))   # a net win is not a loss

    def test_row_without_an_exchange_position_is_dropped_once(self):
        self._open()
        self.fake.live = {}                              # closed in the UI
        self.fake.mid_px["ETH"] = 2600 * 1.10            # stop would fire
        shorts.monitor()
        self.assertIsNone(shorts.get("perp:ETH"))
        self.assertEqual(self.fake.closed, [])
        self.assertIn("no longer on Hyperliquid", self.out[-1])
        n = len(self.out)
        shorts.monitor()
        self.assertEqual(len(self.out), n)               # nothing more to say

    def test_reconcile_adopts_an_exchange_short_with_no_row(self):
        self.fake.live = {"WIF": {"size": -5.0, "entry": 2.0, "upnl": 0.0}}
        shorts._last_recon[0] = 0
        shorts.reconcile(time.time())
        r = shorts.get("perp:WIF")
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["stop_price"], 2.0 * 1.04)
        self.assertIn("adopted it", self.out[-1])

    def test_approved_ticket_older_than_the_age_gate_still_opens(self):
        t = self.ticket()
        with journal._lock:
            journal.conn().execute("UPDATE tickets SET ts=ts-1500 WHERE ticket_id=?", (t["ticket_id"],))
            journal.conn().commit()
        t = dict(t, ts=t["ts"] - 1500)
        self.assertEqual(shorts.execute(t, approved=True), "filled")
        self.assertEqual(shorts.execute(dict(t, ticket_id=999), approved=True), "blocked")  # already short
        self.assertIn("approved but not opened", self.out[-1])

    def test_second_question_on_the_same_asset_is_not_asked(self):
        self.assertEqual(shorts.process_ticket(self.ticket()), "awaiting_approval")
        self.assertEqual(shorts.process_ticket(self.ticket()), "blocked")


class Discovery(Base):
    def test_candidates_are_pumpers_with_volume(self):
        c = shorts.candidates()
        self.assertEqual([x["coin"] for x in c], ["ETH"])
        self.assertEqual(c[0]["side"], "short")
        self.assertAlmostEqual(c[0]["chg24"], 2600 / 2200 - 1, places=3)

    def test_submit_turns_a_confident_short_into_a_ticket_and_a_timid_one_into_pass(self):
        from tradebot import marketdata
        from tradebot.agent import runner
        self.patch(marketdata, "marks", lambda a: ({}, True))
        self.patch(state, "total_value", lambda m: 100.0)
        n = runner.submit([{"asset_id": "perp:WIF", "action": "SHORT_NOW", "p30": 0.6, "p2x": 0,
                            "confidence": 0.5, "what": "t"},
                           {"asset_id": "perp:ETH", "action": "SHORT_NOW", "p30": 0.1, "p2x": 0,
                            "confidence": 0.5, "what": "t"},
                           {"asset_id": "perp:SOL", "action": "BUY_NOW", "p30": 0.9, "p2x": 0,
                            "confidence": 0.5, "what": "keeps pumping"}])
        self.assertEqual(n, 1)
        acts = {t["asset_id"]: t["action"] for t in state.tickets("new")}
        self.assertEqual(acts.get("perp:WIF"), "SHORT_NOW")
        self.assertNotIn("perp:ETH", acts)
        self.assertNotIn("perp:SOL", acts)          # a bullish perp call is never shorted
        t = [x for x in state.tickets("new") if x["asset_id"] == "perp:WIF"][0]
        self.assertAlmostEqual(t["notional_usd"], config.HL_ORDER_USD)


class CalibrationLow(Base):
    def test_short_forecast_scores_on_the_low(self):
        from tradebot import calibration
        with journal._lock:
            journal.conn().execute("DELETE FROM outcomes")
            journal.conn().execute("DELETE FROM forecast_tracking")
            journal.conn().execute("DELETE FROM ratchet_track")
            journal.conn().commit()
        fid = journal.log_forecast({"asset_id": "perp:ETH", "action": "SHORT_NOW", "p30": 0.5})
        calibration.open_tracking(fid, "perp:ETH", "SHORT_NOW", 100.0)
        self.patch(calibration.marketdata, "marks", lambda a: ({"perp:ETH": 90.0}, True))
        calibration.tick()
        self.patch(calibration.config, "TRACK_WINDOW_SEC", 0)
        calibration.tick()
        o = journal.query("SELECT * FROM outcomes WHERE forecast_id=?", (fid,))[0]
        self.assertEqual(o["hit_30"], 1)         # -10% inside 6h >= the 8% target
