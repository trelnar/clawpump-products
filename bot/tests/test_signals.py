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

    def test_evm_addresses_are_lowercased(self):
        mixed = "0xAbCdEf" + "1" * 34
        self.assertEqual(extract.asset_ids(mixed), [f"base:{mixed.lower()}"])

    def test_pool_ids_in_chart_urls_are_not_assets(self):
        pair = "8" * 40
        text = (f"chart https://dexscreener.com/solana/{pair} for {SOL_MINT} "
                f"and https://www.geckoterminal.com/solana/pools/{SOL_MINT2}")
        self.assertEqual(extract.addresses(text), [("solana", SOL_MINT)])
        # a pump.fun link carries the mint itself, so it counts
        self.assertEqual(extract.addresses(f"https://pump.fun/{SOL_MINT2}"),
                         [("solana", SOL_MINT2)])


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
        # the NEWER event lands first: gecko reports at poll time, reddit
        # backdates to created_utc. First-seen must still be the earliest.
        store.record("reddit", a, "post", ref="r1", ts=now - 10)
        store.record("gecko", a, "trending", ref="p1", ts=now - 1000)
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

    def test_one_source_never_outranks_sustained_multi_source_calls(self):
        """A brand-new single-source asset used to score accel x1 x hard = 10
        against 3.0 for an asset three channels had called hourly for 7h."""
        now = time.time()
        fresh = f"solana:{SOL_MINT}"
        called = f"solana:{SOL_MINT2}"
        store.record("pumpfun", fresh, "launch", ref="f1", ts=now - 60)
        store.record("pumpfun", fresh, "trending", ref="koth:f1", ts=now - 60)
        for h in range(7):
            for ch in ("tg:a", "tg:b", "tg:c"):
                store.record(ch, called, "call", ref=f"{ch}/{h}", ts=now - h * 3600 - 30)
        top = store.rising(limit=2, now=now)
        self.assertEqual(top[0]["asset_id"], called)
        self.assertLessEqual(top[1]["score"], store.SINGLE_SOURCE_CAP_HARD)
        loud = f"solana:{'3' * 32}"
        store.record("reddit", loud, "post", ref="big", weight=4.0, ts=now - 60)
        self.assertLessEqual(store.features(loud, now) and
                             store.score_of(store.features(loud, now)), store.SINGLE_SOURCE_CAP)

    def test_prune_drops_old_events(self):
        a = f"solana:{SOL_MINT}"
        store.record("x", a, "mention", ref="old", ts=time.time() - 10 * 86400)
        store.record("x", a, "mention", ref="new")
        gone = f"solana:{SOL_MINT2}"
        store.record("x", gone, "mention", ref="old2", ts=time.time() - 10 * 86400)
        self.assertEqual(store.event_count(a), 2)
        store.prune(max_age_days=3)
        self.assertEqual(store.event_count(a), 1)
        self.assertEqual(store.event_count(gone), 0)
        self.assertIsNone(store.features(gone)["first_seen_min"])   # first_seen pruned too
        self.assertIsNotNone(store.features(a)["first_seen_min"])


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


class AssetIdSpelling(Base):
    def test_norm_asset_lowercases_base_only(self):
        from tradebot import state
        self.assertEqual(state.norm_asset("base:0xABC"), "base:0xabc")
        self.assertEqual(state.norm_asset(f"solana:{SOL_MINT}"), f"solana:{SOL_MINT}")
        self.assertEqual(state.norm_asset("cex:BTC-USDC"), "cex:BTC-USDC")
        self.assertIsNone(state.norm_asset(None))

    def test_migration_rewrites_checksum_ids(self):
        from tradebot import state
        mixed = "base:0xAbCdEf" + "1" * 34
        state.upsert_position(mixed, "base", "base", 1.0, 5.0)
        state.whitelist_add(mixed, "base")
        state._migrate()
        self.assertIsNone(state.get_position(mixed))
        self.assertIsNotNone(state.get_position(mixed.lower()))
        self.assertTrue(state.is_whitelisted(mixed.lower()))
        state.close_position(mixed.lower())

    def test_discovery_dedupes_across_case(self):
        from tradebot import config, marketdata, signals, state
        from tradebot.agent import runner
        mixed = "0xAbCdEf" + "1" * 34
        self.patch(signals, "collect_all", lambda: {})
        self.patch(signals, "candidates", lambda: [
            {"asset_id": f"base:{mixed}", "score": 5.0},
            {"asset_id": f"base:{mixed.lower()}", "score": 4.0}])
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
        self.assertEqual(looked, [mixed.lower()])
        self.assertEqual(len(out), 1)

    def test_one_lookup_failure_does_not_abort_discovery(self):
        from tradebot import config, marketdata, signals
        from tradebot.agent import runner
        self.patch(signals, "collect_all", lambda: {})
        self.patch(signals, "candidates", lambda: [
            {"asset_id": f"solana:{SOL_MINT}", "score": 5.0},
            {"asset_id": f"solana:{SOL_MINT2}", "score": 4.0}])
        self.patch(signals, "features", lambda a: {})

        def dex(chain, addr):
            if addr == SOL_MINT:
                raise RuntimeError("429 Too Many Requests")
            return {"price": 1.0, "liquidity_usd": 1e6, "volume_h24": 1e5,
                    "base_symbol": "X", "created_ms": 0, "pair_address": "p"}
        self.patch(marketdata, "dexscreener_token", dex)
        self.patch(marketdata, "ohlcv_dex", lambda *a, **k: [])
        self.patch(marketdata, "coinbase_movers", lambda: [])
        self.patch(config, "PAID_PROMO_SOURCES", False)
        out = runner.gather()
        self.assertEqual([c["address"] for c in out], [SOL_MINT2])


class Budget(Base):
    def test_sources_stop_at_the_deadline(self):
        from tradebot.signals import budget, sources
        calls = []

        def get(sub):
            calls.append(sub)
            budget.arm(-1)          # the first request uses up the budget
            return {"data": {"children": []}}
        self.patch(sources.reddit, "_get", get)
        self.patch(sources.reddit, "_last", [time.time() - 100])
        budget.arm(60)
        try:
            sources.reddit.collect()
        finally:
            budget.clear()
        self.assertEqual(len(calls), 1)

    def test_signals_text_before_any_signal(self):
        from tradebot import core
        self.assertIn("no signals recorded", core.signals_text("solana:nothing"))


class TgMon(Base):
    def test_not_logged_in_exits_with_the_no_restart_code(self):
        try:
            import telethon  # noqa: F401
        except ImportError:
            self.skipTest("telethon not installed")
        import asyncio
        from tradebot import config
        from tradebot.signals import tgmon

        class FakeClient:
            def __init__(self, *a, **k): self.disconnected = False
            async def connect(self): pass
            async def is_user_authorized(self): return False
            async def disconnect(self): self.disconnected = True
        import telethon as _t
        self.patch(_t, "TelegramClient", FakeClient)
        self.patch(config, "TG_API_ID", "1"); self.patch(config, "TG_API_HASH", "h")
        self.assertEqual(asyncio.run(tgmon._run()), tgmon.EXIT_NOT_LOGGED_IN)
