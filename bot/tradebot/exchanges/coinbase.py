"""execution venue: Coinbase Advanced Trade via the official SDK. Trade-only
CDP key scoped to the HypeBot portfolio; withdrawal disabled; IP-allowlisted."""
import uuid
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from .. import config, journal

_client = None


def client():
    global _client
    if _client is None:
        from coinbase.rest import RESTClient
        _client = RESTClient(api_key=config.COINBASE_API_KEY,
                             api_secret=config.COINBASE_API_SECRET,
                             timeout=10)  # a hung call must never freeze the core loop
    return _client


def _to_dict(resp):
    return resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)


QUOTE_CURRENCIES = ("USDC", "USD")


def balances():
    """Available balance per currency."""
    accts = _to_dict(client().get_accounts(limit=250))
    out = {}
    for a in accts.get("accounts", []):
        cur = a.get("currency")
        if cur:
            out[cur] = out.get(cur, 0.0) + float(
                (a.get("available_balance") or {}).get("value") or 0)
    return out


def quote_balance(currency):
    """Spendable balance in ONE quote currency.

    USD and USDC are not interchangeable on a given product: a BTC-USD order
    cannot spend USDC, and the venue refuses it with INSUFFICIENT_FUND. The
    first real order died this way -- the books said $275 cash and every dollar
    of it was USDC while the order was quoted in USD."""
    return balances().get(currency, 0.0)


def usdc_balance():
    """Total spendable quote cash, for the portfolio-value denominator."""
    b = balances()
    return sum(b.get(c, 0.0) for c in QUOTE_CURRENCIES)


_products = {}


def product(product_id):
    """Cached product metadata. Every venue quantises price and size to its own
    increments and rejects anything finer -- the first real order was refused
    with INVALID_PRICE_PRECISION for sending BTC-USD three decimals."""
    if product_id not in _products:
        r = _to_dict(client().get_product(product_id=product_id))
        _products[product_id] = r.get("product") or r
    return _products[product_id]


def _quantise(value, increment, rounding):
    """Snap to a venue increment. Decimal, not float: 0.1 + 0.2 arithmetic is
    how an order comes back one satoshi over the tick and gets rejected."""
    try:
        step = Decimal(str(increment))
    except Exception:
        step = Decimal("0")
    d = Decimal(str(value))
    if step <= 0:
        return format(d.normalize(), "f")
    return format((d / step).quantize(Decimal("1"), rounding=rounding) * step, "f")


def fmt_price(product_id, price):
    """Round a limit price UP to the tick: for a buy, up is the marketable side."""
    p = product(product_id)
    return _quantise(price, p.get("quote_increment") or p.get("price_increment") or "0.01",
                     ROUND_UP)


def fmt_size(product_id, size):
    """Round a base size DOWN: never order more than intended."""
    return _quantise(size, product(product_id).get("base_increment") or "0.00000001",
                     ROUND_DOWN)


def check_min_size(product_id, base_size, notional_usd):
    """A sub-minimum order is refused by the venue, and a sub-minimum REMAINDER
    after a partial exit is unsellable by any automated path."""
    p = product(product_id)
    base_min = float(p.get("base_min_size") or 0)
    quote_min = float(p.get("quote_min_size") or 0)
    if base_min and float(base_size) < base_min:
        raise RuntimeError(f"size {base_size} below {product_id} minimum {base_min}")
    if quote_min and notional_usd and notional_usd < quote_min:
        raise RuntimeError(f"${notional_usd:.2f} below {product_id} minimum ${quote_min}")


def best_price(product_id):
    book = _to_dict(client().get_product_book(product_id=product_id, limit=1))
    pb = book.get("pricebook", {})
    bid = float(pb["bids"][0]["price"]) if pb.get("bids") else None
    ask = float(pb["asks"][0]["price"]) if pb.get("asks") else None
    return bid, ask


def _placed(client_oid, resp):
    """Extract the exchange's own order_id from a placement response.

    The client_order_id we generate is NOT queryable on this API: list_orders
    has no client_order_id filter (it lands in **kwargs and is ignored server
    side), so the id returned here is the only handle on the order we placed.
    Raising on a rejected placement keeps a failed order out of the fill poll."""
    if resp.get("success") is False:
        raise RuntimeError(f"order rejected: "
                           f"{resp.get('error_response') or resp.get('failure_reason')}")
    oid = (resp.get("order_id")
           or (resp.get("success_response") or {}).get("order_id"))
    if not oid:
        raise RuntimeError(f"no order_id in placement response: {str(resp)[:200]}")
    # An event, not a second orders row: the journal is append-only and
    # client_oid is UNIQUE. This is what ties our id to the venue's.
    journal.log_event("order_placed", detail={"client_oid": client_oid, "order_id": oid})
    return oid, resp


def limit_buy(product_id, notional_usd, limit_price):
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    price = fmt_price(product_id, limit_price)
    size = fmt_size(product_id, notional_usd / float(price))
    check_min_size(product_id, size, notional_usd)
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="buy", notional_usd=notional_usd, limit_price=float(price),
                      status="submitted", detail=None)
    r = _to_dict(client().limit_order_gtc_buy(client_order_id=oid, product_id=product_id,
                                              base_size=size, limit_price=price))
    return _placed(oid, r)


def limit_sell(product_id, qty, limit_price):
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="sell", notional_usd=qty * limit_price, limit_price=limit_price,
                      status="submitted", detail=None)
    r = _to_dict(client().limit_order_gtc_sell(
        client_order_id=oid, product_id=product_id,
        base_size=fmt_size(product_id, qty),
        limit_price=_quantise(limit_price, product(product_id).get("quote_increment")
                              or "0.01", ROUND_DOWN)))
    return _placed(oid, r)


def market_sell(product_id, qty):
    """Risk-off escalation only: a worse fill beats an unfilled exit."""
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="sell", notional_usd=None, limit_price=None,
                      status="submitted_market", detail=None)
    r = _to_dict(client().market_order_sell(client_order_id=oid, product_id=product_id,
                                            base_size=fmt_size(product_id, qty)))
    return _placed(oid, r)


def order_status(order_id):
    """Exact single-order lookup by the exchange's order_id. Never a list read:
    orders[0] of an account-wide list is somebody else's order."""
    try:
        r = _to_dict(client().get_order(order_id=order_id))
        return r.get("order")
    except Exception as e:
        journal.log_event("order_status_fail", detail=f"{order_id}: {e}")
        return None


def cancel(order_id):
    return _to_dict(client().cancel_orders(order_ids=[order_id]))
