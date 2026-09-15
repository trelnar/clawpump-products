"""Regressions for the external validation of 2026-09-15 (ten findings).

Each test drives the real coordinating path with the venue mocked, the way
the validation's reproductions did; the green happy-path mocks had not
covered these sequences.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

from tradebot import config, execution, journal, shorts, state  # noqa: E402
from tradebot.exchanges import coinbase, evm_dex, hyperliquid as hl  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        state.init()
        for p in state.positions():
            state.close_position(p["asset_id"])
        state.set_mode("NORMAL", reason="test")
        self.patches = []
        self.said = []
        self.patch(execution.alerts, "ops", self.said.append)
        self.patch(config, "SETTLE_READ_SLEEP_SEC", 0)
        self.patch(config, "SETTLE_READ_TRIES", 2)
        with journal._lock:            # other modules' timed-out buys are theirs, not ours
            journal.conn().execute("DELETE FROM orders WHERE status='unresolved'")
            journal.conn().commit()

    def patch(self, mod, name, value):
        self.patches.append((mod, name, getattr(mod, name, None)))
        setattr(mod, name, value)

    def tearDown(self):
        for mod, name, old in reversed(self.patches):
            setattr(mod, name, old)


# --- 1. a lost Base broadcast response must not send twice ---------------------------
class FakeEth:
    def __init__(self, fail):
        self.fail, self.sent, self.nonce, self.gas_price = fail, [], 0, 1

    def get_transaction_count(self, a):
        self.nonce += 1
        return self.nonce

    def estimate_gas(self, tx):
        return 100000

    def send_raw_transaction(self, raw):
        self.sent.append(raw)
        raise self.fail


class FakeW3:
    def __init__(self, eth):
        self.eth = eth

    def to_checksum_address(self, a):
        return a

    def to_hex(self, b):
        return "0x" + bytes(b).hex()

    def keccak(self, b):
        return hashlib.sha3_256(bytes(b)).digest()


class FakeAcct:
    address = "0x" + "ab" * 20

    def sign_transaction(self, tx):
        return types.SimpleNamespace(raw_transaction=json.dumps(tx, sort_keys=True).encode())


class BaseSendAmbiguity(Base):
    def _swap(self, fail):
        eth = FakeEth(fail)
        self.patch(evm_dex, "_w3", lambda: FakeW3(eth))
        self.patch(evm_dex, "_account", lambda: FakeAcct())
        self.patch(evm_dex, "route", lambda *a: {"routeSummary": {}})
        self.patch(evm_dex, "_ensure_allowance", lambda *a, **k: None)
        resp = types.SimpleNamespace(raise_for_status=lambda: None,
                                     json=lambda: {"data": {"routerAddress": "0x" + "cd" * 20,
                                                            "data": "0x"}})
        self.patch(evm_dex.requests, "post", lambda *a, **k: resp)
        self.patch(config, "EVM_ROUTER_ALLOWLIST", [])
        self.patch(config, "SIMULATE_BEFORE_SEND", False)
        return eth, lambda: evm_dex.swap(evm_dex.USDC, "0x" + "ef" * 20, 1_000_000, 300)

    def test_a_dropped_connection_returns_the_hash_and_sends_once(self):
        eth, swap = self._swap(ConnectionError("connection reset"))
        h = swap()
        self.assertTrue(h.startswith("0x") and len(h) == 66)
        self.assertEqual(len(eth.sent), 1)          # not rebuilt with a fresh nonce

    def test_a_429_is_still_retried(self):
        eth, swap = self._swap(RuntimeError("429 Too Many Requests"))
        with self.assertRaises(RuntimeError):
            swap()
        self.assertGreaterEqual(len(eth.sent), 1)


# --- 2. a confirmed buy is never 'not bought' ------------------------------------------
class ConfirmedBuyIsBooked(Base):
    def _base_buy(self, confirm, balance_after, exact=None, tag="a"):
        asset = "base:0x" + "ef" * 20
        self.patch(execution.time, "sleep", lambda s: None)
        self.patch(config, "FILL_TIMEOUT_EVM_SEC", 0.5)
        state.set_kv(f"symbol:{asset}", "wxyz")
        state.set_cash("base", 100.0)
        self.patch(execution.marketdata, "dexscreener_token", lambda c, a: None)
        reads = iter([0, *balance_after])
        self.patch(evm_dex, "token_balance", lambda t: (next(reads, balance_after[-1]), 6))
        self.patch(evm_dex, "swap", lambda *a, **k: "0xhash" + tag)
        self.patch(evm_dex, "confirm", confirm)
        self.patch(evm_dex, "tx_token_delta",
                   lambda h, t, owner=None, decimals=None: exact)
        t = {"ticket_id": 4242, "asset_id": asset, "venue": "base", "chain": "base",
             "notional_usd": 10.0, "ts": time.time(), "invalidation_price": None}
        return asset, execution.execute_buy(t, 0.002)

    def test_balance_reads_that_never_rise_fall_back_to_the_receipt(self):
        asset, res = self._base_buy(lambda h: "confirmed", [0, 0, 0], exact=5000.0)
        self.assertEqual(res, "filled")
        self.assertAlmostEqual(state.get_position(asset)["qty"], 5000.0)
        self.assertEqual(state.get_mode(), "NORMAL")

    def test_a_dead_confirmation_endpoint_is_not_a_failed_swap(self):
        def down(h):
            raise RuntimeError("rpc down")
        asset, res = self._base_buy(down, [5_000_000_000, 5_000_000_000], exact=None)
        self.assertEqual(res, "filled")                   # landed by balance
        self.assertAlmostEqual(state.get_position(asset)["qty"], 5000.0)

    def test_an_unknown_outcome_is_remembered_and_adopted_when_it_lands(self):
        def down(h):
            raise RuntimeError("rpc down")
        asset, res = self._base_buy(down, [0, 0, 0], exact=None, tag="u")
        self.assertEqual(res, "failed")
        self.assertIsNone(state.get_position(asset))
        rows = journal.query("SELECT * FROM orders WHERE client_oid='0xhashu' AND status='unresolved'")
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("could not confirm" in m for m in self.said))
        # a later pass: the chain has it
        self.patch(evm_dex, "confirm", lambda h: "confirmed")
        self.patch(evm_dex, "tx_token_delta", lambda h, t, owner=None, decimals=None: 5000.0)
        execution.resolve_unresolved_orders()
        p = state.get_position(asset)
        self.assertAlmostEqual(p["qty"], 5000.0)
        self.assertAlmostEqual(p["invalidation_price"], 0.002 * 0.85)
        self.assertAlmostEqual(state.cash()["base"], 90.0)
        self.assertEqual(journal.query("SELECT status FROM orders WHERE client_oid='0xhashu'")[0]["status"],
                         "resolved")
        self.assertTrue(any("did go through" in m for m in self.said))


# --- 3. STOP during a later gate ---------------------------------------------------------
class StopDuringGate(Base):
    def test_a_stop_tapped_during_the_quote_balance_gate_wins(self):
        asset = "cex:STP2-USDC"
        state.whitelist_add(asset, "coinbase")
        self.patch(execution.marketdata, "price", lambda a: 1.0)
        self.patch(execution.risk, "check_buy", lambda *a, **k: None)

        def quote_balance(q):
            state.set_mode("USER_STOP", reason="operator")   # the tap lands here
            return 1000.0
        self.patch(coinbase, "quote_balance", quote_balance)
        placed = []
        self.patch(coinbase, "limit_buy", lambda *a: placed.append(a) or ("srv", {}))
        t = {"ticket_id": 4343, "asset_id": asset, "venue": "coinbase", "chain": None,
             "notional_usd": 10.0, "ts": time.time(), "invalidation_price": None}
        self.assertEqual(execution.process_ticket(t, 1000.0, True), "blocked")
        self.assertEqual(placed, [])
        self.addCleanup(state.set_mode, "NORMAL", "cleanup")


# --- 4. decisions on holdings are never dropped by the entry budget ----------------------
class HeldDecisionsAlwaysProcessed(Base):
    def test_a_sell_after_six_passes_still_becomes_a_ticket(self):
        from tradebot.agent import runner
        self.patch(runner.marketdata, "marks", lambda a: ({}, True))
        self.patch(runner.marketdata, "price", lambda a: 1.0)
        self.patch(state, "total_value", lambda m: 100.0)
        state.upsert_position("solana:HELD7", "solana", "solana", 100.0, 10.0)
        cands = [{"asset_id": f"solana:P{i}", "action": "PASS", "p30": 0.1} for i in range(6)]
        cands.append({"asset_id": "solana:HELD7", "action": "SELL_NOW", "p30": 0.1,
                      "sell_fraction": 1.0})
        runner.submit(cands)
        sells = [t for t in state.tickets("new") if t["asset_id"] == "solana:HELD7"
                 and t["action"] == "SELL_NOW"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(journal.query("SELECT COUNT(*) n FROM forecasts WHERE asset_id='solana:HELD7'")[0]["n"], 1)


# --- 5/6/9. the short leg ---------------------------------------------------------------
class ShortLeg(Base):
    def setUp(self):
        super().setUp()
        with journal._lock:
            journal.conn().execute("DELETE FROM shorts")
            journal.conn().commit()
        self.patch(config, "SHORTS_ENABLED", True)
        self.patch(shorts.alerts, "ops", self.said.append)
        state.set_kv("symbol:perp:ETH", "ETH")
        self.patch(hl, "positions", lambda: {"ETH": {"size": -1.0, "entry": 2600.0, "upnl": 0}})
        self.patch(hl, "round_size", lambda coin, sz, up=False: round(sz, 4))
        shorts._write({"asset_id": "perp:ETH", "coin": "ETH", "qty": 1.0, "entry_price": 2600.0,
                       "notional_usd": 2600.0, "entry_ts": time.time(), "stop_price": 2704.0,
                       "ratchet": json.dumps(shorts._new_ratchet()), "lwm": 2600.0,
                       "last_alert_ts": 0})

    def test_a_partial_fill_of_a_full_cover_keeps_the_rest_on_the_books(self):
        self.patch(hl, "close_short", lambda coin, sz=None: (0.4, 2500.0, 7))
        self.assertEqual(shorts.cover("perp:ETH", 1.0, "stop 2704 hit at 2710"), "filled")
        r = shorts.get("perp:ETH")
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["qty"], 0.6)

    def test_a_dust_remainder_closes_the_row(self):
        self.patch(hl, "close_short", lambda coin, sz=None: (0.9999, 2500.0, 7))
        shorts.cover("perp:ETH", 1.0, "stop")
        self.assertIsNone(shorts.get("perp:ETH"))

    def test_an_approved_short_does_not_open_while_shorts_are_off(self):
        with journal._lock:
            journal.conn().execute("DELETE FROM shorts")
            journal.conn().commit()
        self.patch(config, "SHORTS_ENABLED", False)
        opened = []
        self.patch(hl, "open_short", lambda *a: opened.append(a) or (0.01, 2600.0, 1))
        tid = state.add_ticket(asset_id="perp:ETH", venue="hyperliquid", chain=None,
                               action="SHORT_NOW", notional_usd=15.0, ts=time.time(),
                               status="approved")
        t = [x for x in state.tickets("approved") if x["ticket_id"] == tid][0]
        self.assertEqual(shorts.execute(t, approved=True), "blocked")
        self.assertEqual(opened, [])
        self.assertIsNone(shorts.get("perp:ETH"))

    def test_size_rounding_is_idempotent(self):
        self.patch(hl, "sz_decimals", lambda coin: 4)
        for mod, name, old in self.patches:
            if name == "round_size":
                setattr(mod, name, old)
        self.assertAlmostEqual(hl.round_size("ETH", 0.0043), 0.0043)
        self.assertAlmostEqual(hl.round_size("ETH", hl.round_size("ETH", 0.0043)), 0.0043)
        self.assertAlmostEqual(hl.round_size("ETH", 0.00435), 0.0043)
        self.assertAlmostEqual(hl.round_size("ETH", 0.00431, up=True), 0.0044)
        self.assertAlmostEqual(hl.round_size("ETH", 0.0043, up=True), 0.0043)


# --- 7. wallet concentration, not token accounts -------------------------------------------
class RugOwners(Base):
    def test_one_deployer_across_nineteen_accounts_is_one_holder(self):
        from tradebot import rugcheck
        from tradebot.exchanges import solana_dex
        amounts = [450] + [25] * 19            # pool 45%, dev 47.5% across 19 accounts
        owners = ["POOL"] + ["DEV"] * 19

        def rpc(method, params):
            if method == "getTokenLargestAccounts":
                return {"value": [{"address": f"A{i}", "amount": str(a)} for i, a in enumerate(amounts)]}
            if method == "getTokenSupply":
                return {"value": {"amount": "1000"}}
            return {"value": [{"data": {"parsed": {"info": {"owner": o}}}} for o in owners]}
        self.patch(solana_dex, "_rpc", rpc)
        self.patch(config, "RUG_MAX_TOP10_SHARE", 0.40)
        share, m = rugcheck.top_holders_share("MINT", "POOL")
        self.assertAlmostEqual(share, 0.475)
        self.assertEqual(m["wallets"], 1)


if __name__ == "__main__":
    unittest.main()
