"""Signal layer: extraction, storage math, each source's parser against a
recorded-shape fixture, and isolation between sources. No network."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import config, journal, signals, state  # noqa: E402
from tradebot.signals import extract, store  # noqa: E402
from tradebot.signals.sources import clanker, gecko, pumpfun, reddit  # noqa: E402

SOL_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_MINT2 = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
EVM = "0x4200000000000000000000000000000000000006"


class Base(unittest.TestCase):
    def setUp(self):
        state.init()
        store.init()
        journal.conn().execute("DELETE FROM signal_events")
        journal.conn().execute("DELETE FROM signal_first_seen")
        journal.conn().execute("DELETE FROM signal_source_runs")
        journal.conn().commit()
        self.patches = []

    def patch(self, mod, name, value):
        self.patches.append((mod, name, getattr(mod, name)))
        setattr(mod, name, value)

    def tearDown(self):
        for mod, name, old in reversed(self.patches):
            setattr(mod, name, old)


class Extract(Base):
    def test_solana_and_evm_addresses(self):
        text = f"new call {SOL_MINT} and on base {EVM} lfg"
        self.assertEqual(extract.addresses(text), [("solana", SOL_MINT), ("base", EVM)])

    def test_words_that_look_like_base58_but_contain_forbidden_chars_are_skipped(self):
        # 'l' and 'O' and '0' are not base58
        self.assertEqual(extract.addresses("Illllllllllllllllllllllllllllllllllll"), [])

    def test_tickers(self):
        self.assertEqual(extract.tickers("buy $PEPE and $wif not $1x"), ["PEPE", "WIF"])

    def test_asset_ids_form(self):
        self.assertEqual(extract.asset_ids(EVM), [f"base:{EVM}"])


class Store(Base):
    def test_record_is_idempotent_on_ref(self):
        a = f"solana:{SOL_MINT}"
        self.assertTrue(store.record("reddit", a, "post", ref="/r/x/1"))
        self.assertFalse(store.record("reddit", a, "post", ref="/r/x/1"))
        self.assertTrue(store.record("reddit", a, "post", ref="/r/x/2"))
        self.assertEqual(store.features(a)["events_6h"], 2)

    def test_first_seen_is_the_earliest_event(self):
        a = f"solana:{SOL_MINT}"
        now = time.time()
        store.record("gecko", a, "trending", ref="p1", ts=now - 1000)
        store.record("reddit", a, "post", ref="r1", ts=now - 10)
        f = store.features(a, now)
        self.assertAlmostEqual(f["first_seen_min"], 1000 / 60, places=1)
        self.assertEqual(f["first_source"], "gecko")

    def test_acceleration_is_last_hour_vs_prior_six(self):
        a = f"solana:{SOL_MINT}"
        now = time.time()
        # 6 mentions spread over the prior six hours -> 1/h baseline
        for i in range(6):
            store.record("tg", a, "mention", ref=f"old{i}", ts=now - 3600 - i * 3000 - 60)
        # 4 in the last hour
        for i in range(4):
            store.record("tg", a, "mention", ref=f"new{i}", ts=now - i * 600)
        self.assertAlmostEqual(store.features(a, now)["accel"], 4.0, places=1)

    def test_a_fresh_asset_has_a_baseline_floor(self):
        a = f"solana:{SOL_MINT}"
        store.record("tg", a, "mention", ref="only")
        self.assertEqual(store.features(a)["accel"], 2.0)   # 1 / max(0, 0.5)

    def test_breadth_counts_distinct_sources(self):
        a = f"solana:{SOL_MINT}"
        for i in range(5):
            store.record("reddit", a, "post", ref=f"r{i}")
        store.record("gecko", a, "trending", ref="g")
        self.assertEqual(store.features(a)["breadth"], 2)

    def test_rising_ranks_broad_accelerating_hard_events_first(self):
        loud = f"solana:{SOL_MINT}"        # one source, many mentions
        broad = f"solana:{SOL_MINT2}"      # two sources + a graduation
        for i in range(8):
            store.record("reddit", loud, "post", ref=f"l{i}")
        store.record("reddit", broad, "post", ref="b1")
        store.record("pumpfun", broad, "graduation", ref="b2")
        top = store.rising(limit=2)
        self.assertEqual(top[0]["asset_id"], broad)
        self.assertIn("graduation", top[0]["kinds"])

    def test_prune_drops_old_events(self):
        a = f"solana:{SOL_MINT}"
        store.record("x", a, "mention", ref="old", ts=time.time() - 10 * 86400)
        store.record("x", a, "mention", ref="new")
        store.prune(max_age_days=3)
        self.assertEqual(store.features(a)["events_6h"], 1)


class GeckoParser(Base):
    FIXTURE = {"data": [{
        "attributes": {"name": "USDT / SOL", "address": "POOL1", "reserve_in_usd": "62376.49",
                       "volume_usd": {"h1": "1200.5", "h24": "90000"}},
        "relationships": {"base_token": {"data": {"id": f"solana_{SOL_MINT}"}}},
    }, {
        "attributes": {"name": "DUST / SOL", "address": "POOL2", "reserve_in_usd": "120"},
        "relationships": {"base_token": {"data": {"id": f"solana_{SOL_MINT2}"}}},
    }]}

    def test_records_pools_above_liquidity_floor_only(self):
        self.patch(gecko, "_get", lambda path: self.FIXTURE)
        self.patch(config, "SIGNAL_SOURCES", ["gecko"])
        n = gecko.collect()
        self.assertEqual(n, 4)      # 2 lists x 2 chains, the one liquid pool each
        f = store.features(f"solana:{SOL_MINT}")
        self.assertIn("trending", f["kinds"]); self.assertIn("new_pool", f["kinds"])
        self.assertEqual(store.features(f"solana:{SOL_MINT2}")["events_6h"], 0)

    def test_asset_id_is_split_on_the_first_underscore_only(self):
        pool = {"relationships": {"base_token": {"data": {"id": "base_0xab_cd"}}}}
        self.assertEqual(gecko._asset_of("base", pool), "base:0xab_cd")


class RedditParser(Base):
    def test_posts_with_addresses_become_weighted_events(self):
        now = time.time()
        fx = {"data": {"children": [
            {"data": {"title": "gem", "selftext": f"CA {SOL_MINT}", "ups": 100,
                      "created_utc": now - 60, "permalink": "/r/x/a"}},
            {"data": {"title": "old", "selftext": f"CA {SOL_MINT2}", "ups": 5,
                      "created_utc": now - 10 * 3600, "permalink": "/r/x/b"}},
            {"data": {"title": "no address here", "selftext": "moon", "ups": 900,
                      "created_utc": now, "permalink": "/r/x/c"}},
        ]}}
        self.patch(reddit, "_get", lambda sub: fx)
        self.patch(config, "REDDIT_SUBS", ["x"])
        self.assertEqual(reddit.collect(), 1)
        self.assertAlmostEqual(store.features(f"solana:{SOL_MINT}")["mentions_1h"], 4.0)  # 1 + min(100/25,3)
        self.assertEqual(store.features(f"solana:{SOL_MINT2}")["events_6h"], 0)


class PumpfunParser(Base):
    def test_graduation_and_fresh_launch(self):
        now = time.time()
        coins = [
            {"mint": SOL_MINT, "symbol": "GRAD", "complete": True,
             "created_timestamp": (now - 5 * 3600) * 1000, "usd_market_cap": 80000},
            {"mint": SOL_MINT2, "symbol": "NEW", "complete": False,
             "created_timestamp": (now - 600) * 1000, "usd_market_cap": 20000,
             "king_of_the_hill_timestamp": (now - 100) * 1000},
            {"mint": "tiny", "complete": False,
             "created_timestamp": (now - 600) * 1000, "usd_market_cap": 500},
        ]
        self.patch(pumpfun, "_get", lambda path, params: coins)
        n = pumpfun.collect()
        self.assertGreaterEqual(n, 3)
        self.assertIn("graduation", store.features(f"solana:{SOL_MINT}")["kinds"])
        k = store.features(f"solana:{SOL_MINT2}")["kinds"]
        self.assertIn("launch", k); self.assertIn("trending", k)
        self.assertEqual(store.features("solana:tiny")["events_6h"], 0)

    def test_a_dict_wrapped_response_is_also_understood(self):
        self.patch(pumpfun, "_get", lambda path, params: {"coins": []})
        self.assertEqual(pumpfun.collect(), 0)


class ClankerParser(Base):
    def test_recent_launch_recorded(self):
        import datetime as dt
        recent = dt.datetime.now(dt.timezone.utc).isoformat()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        payload = {"data": [
            {"contract_address": EVM.upper(), "symbol": "A", "name": "a", "created_at": recent},
            {"contract_address": "0x" + "1" * 40, "symbol": "B", "created_at": old},
        ]}

        class R:
            def raise_for_status(self): pass
            def json(self): return payload
        self.patch(clanker.requests, "get", lambda *a, **k: R())
        self.assertEqual(clanker.collect(), 1)
        self.assertIn("launch", store.features(f"base:{EVM}")["kinds"])


class Isolation(Base):
    def test_a_raising_source_does_not_stop_the_others(self):
        class Bad:
            NAME, KEYLESS = "bad", True
            enabled = staticmethod(lambda: True)
            collect = staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        class Good:
            NAME, KEYLESS = "good", True
            enabled = staticmethod(lambda: True)
            collect = staticmethod(lambda: 3)
        self.patch(signals, "SOURCES", [Bad, Good])
        out = signals.collect_all()
        self.assertEqual(out, {"bad": None, "good": 3})
        health = {r["source"]: r for r in store.source_health()}
        self.assertEqual(health["bad"]["last_ok"], 0)
        self.assertIn("boom", health["bad"]["last_error"])
        self.assertEqual(health["good"]["last_count"], 3)

    def test_sources_without_keys_are_off(self):
        self.patch(config, "NEYNAR_API_KEY", None)
        self.patch(config, "BIRDEYE_API_KEY", None)
        names = [s.NAME for s in signals.enabled_sources()]
        self.assertNotIn("farcaster", names); self.assertNotIn("birdeye", names)


class Discovery(Base):
    def test_gather_leads_with_rising_signals_and_skips_paid_promo_by_default(self):
        from tradebot.agent import runner
        a = f"solana:{SOL_MINT}"
        store.record("pumpfun", a, "graduation", ref="g")
        store.record("reddit", a, "post", ref="r")
        self.patch(signals, "collect_all", lambda: {})
        self.patch(runner.marketdata, "dexscreener_token", lambda c, ad: {
            "price": 1.0, "liquidity_usd": 60000.0, "volume_h24": 1e5, "base_symbol": "T",
            "created_ms": 0, "pair_address": "P"})
        self.patch(runner.marketdata, "ohlcv_dex", lambda *a, **k: [])
        self.patch(runner.marketdata, "coinbase_movers", lambda: [])
        promo_called = []
        self.patch(runner.marketdata, "dexscreener_trending",
                   lambda: promo_called.append(1) or [])
        self.patch(config, "PAID_PROMO_SOURCES", False)
        out = runner.gather()
        self.assertEqual(out[0]["address"], SOL_MINT)
        self.assertEqual(out[0]["source"], "signals")
        self.assertEqual(out[0]["signals"]["breadth"], 2)
        self.assertIn("graduation", out[0]["signals"]["kinds"])
        self.assertEqual(promo_called, [])


if __name__ == "__main__":
    unittest.main()
