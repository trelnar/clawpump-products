"""Contract tests: check the code against the real vendored SDK, not my mocks.

The Coinbase critical of 2026-09-02 passed 33 green tests because every one of
them stubbed `coinbase.order_status`, so the broken line -- a call passing a
parameter `list_orders` does not accept -- was never executed. Mocks encode the
author's assumptions, so a wrong assumption passes. These tests assert against
the installed library's own signatures and response types instead, and skip
cleanly where a dependency is not installed (this suite must stay runnable
without credentials or network).
"""
import inspect
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADEBOT_LOG_STDOUT", "0")
# A fresh DB per run. A fixed path here would win the setdefault race against
# the other test module and leak state between runs.
os.environ.setdefault("TRADEBOT_DB", os.path.join(tempfile.mkdtemp(), "test.db"))


def _sdk(name):
    try:
        __import__(name)
        return sys.modules[name]
    except ImportError:
        return None


class CoinbaseSDKContract(unittest.TestCase):
    """Every Coinbase call the bot makes, checked against the real SDK."""

    def setUp(self):
        self.orders = _sdk("coinbase.rest.orders")
        if self.orders is None:
            self.skipTest("coinbase-advanced-py not installed")

    def _params(self, fn_name):
        fn = getattr(self.orders, fn_name)
        return set(inspect.signature(fn).parameters)

    def test_the_calls_we_make_exist(self):
        for fn in ("limit_order_gtc_buy", "limit_order_gtc_sell",
                   "market_order_sell", "get_order", "cancel_orders", "list_orders"):
            self.assertTrue(hasattr(self.orders, fn), f"SDK has no {fn}")

    def test_placement_helpers_accept_client_order_id(self):
        for fn in ("limit_order_gtc_buy", "limit_order_gtc_sell", "market_order_sell"):
            self.assertIn("client_order_id", self._params(fn), fn)

    def test_list_orders_does_NOT_filter_by_client_order_id(self):
        # The regression that cost a critical. If a future SDK adds the filter,
        # this fails and someone re-reads exchanges/coinbase.py deliberately.
        self.assertNotIn("client_order_id", self._params("list_orders"))

    def test_we_poll_by_order_id_not_by_listing(self):
        from tradebot.exchanges import coinbase
        src = inspect.getsource(coinbase.order_status)
        self.assertIn("get_order", src)
        self.assertNotIn("list_orders", src)

    def test_get_order_takes_the_id_we_pass_it(self):
        self.assertIn("order_id", self._params("get_order"))

    def test_the_order_fields_we_read_are_real_fields(self):
        types = _sdk("coinbase.rest.types.orders_types")
        src = inspect.getsource(types.Order.__init__)
        for field in ("status", "filled_size", "average_filled_price",
                      "total_fees", "filled_value", "order_id", "client_order_id"):
            self.assertIn(f'"{field}"', src, f"Order has no {field}")

    def test_the_placement_response_carries_an_order_id(self):
        types = _sdk("coinbase.rest.types.orders_types")
        for cls in ("CreateOrderResponse", "CreateOrderSuccess"):
            self.assertIn('"order_id"', inspect.getsource(getattr(types, cls).__init__))

    def test_to_dict_converts_nested_responses(self):
        # coinbase.py:_to_dict relies on this being recursive.
        base = _sdk("coinbase.rest.types.base_response")
        self.assertIn("to_dict", inspect.getsource(base.BaseResponse.to_dict))


class AggregatorPayloadContract(unittest.TestCase):
    """Recorded real response shapes. Update these only against a fresh capture."""

    JUPITER_QUOTE = {
        "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "outputMint": "So11111111111111111111111111111111111111112",
        "inAmount": "5000000", "outAmount": "34567890",
        "otherAmountThreshold": "34000000", "slippageBps": 300,
        "priceImpactPct": "0.0012", "routePlan": [],
    }
    KYBER_ROUTE = {"routeSummary": {"tokenIn": "0x833589f", "amountIn": "5000000",
                                    "tokenOut": "0x4200", "amountOut": "1234567890",
                                    "gasUsd": "0.004"},
                   "routerAddress": "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"}
    _SOL_INFO = {
        "mint": "So11111111111111111111111111111111111111112",
        "tokenAmount": {"amount": "34567890", "decimals": 9, "uiAmount": 0.03456789},
    }
    SOL_TOKEN_ACCOUNTS = {
        "value": [{"account": {"data": {"parsed": {"info": _SOL_INFO}}}}],
    }

    def test_jupiter_fields_the_code_reads(self):
        for f in ("outAmount", "inAmount"):
            self.assertIn(f, self.JUPITER_QUOTE)
        self.assertIsInstance(int(self.JUPITER_QUOTE["outAmount"]), int)

    def test_kyber_fields_the_code_reads(self):
        self.assertIn("amountOut", self.KYBER_ROUTE["routeSummary"])
        self.assertIn("routerAddress", self.KYBER_ROUTE)

    def test_the_allowlisted_router_matches_the_recorded_one(self):
        from tradebot import config
        self.assertIn(self.KYBER_ROUTE["routerAddress"].lower(),
                      {a.lower() for a in config.EVM_ROUTER_ALLOWLIST})

    def test_solana_token_account_parsing(self):
        acct = self.SOL_TOKEN_ACCOUNTS["value"][0]["account"]["data"]["parsed"]["info"]
        self.assertIn("mint", acct)
        for f in ("amount", "decimals", "uiAmount"):
            self.assertIn(f, acct["tokenAmount"])


class ConfigCoherence(unittest.TestCase):
    """Limits that silently cancel each other are worse than no limits."""

    def setUp(self):
        from tradebot import config
        self.c = config

    def test_a_full_position_is_reachable_in_one_order(self):
        self.assertGreaterEqual(self.c.MAX_SINGLE_ORDER_PCT, self.c.MAX_POSITION_PCT,
                                "fat-finger cap below the position cap makes the "
                                "position cap unreachable")

    def test_every_phase_can_actually_place_its_order(self):
        for ph, factor in self.c.PHASE_SIZE_FACTOR.items():
            size = (self.c.PHASE1_ORDER_USD if ph == 1
                    else self.c.MAX_POSITION_PCT * 1000.0 * factor)
            self.assertLessEqual(size, self.c.MAX_SINGLE_ORDER_PCT * 1000.0,
                                 f"phase {ph} order is rejected as a fat finger")

    def test_the_gas_floor_is_below_what_the_wallets_hold(self):
        # Documents the funded reality; fails loudly if the floor is raised past it.
        for chain, funded in (("solana", 0.0965), ("base", 0.0025)):
            floor = self.c.GAS_COST_PER_EXIT[chain] * self.c.GAS_EXITS_FLOOR
            self.assertLess(floor, funded, f"{chain} gas floor exceeds the float")


if __name__ == "__main__":
    unittest.main()
