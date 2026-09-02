"""execution venue: Coinbase Advanced Trade via the official SDK. Trade-only
CDP key scoped to the HypeBot portfolio; withdrawal disabled; IP-allowlisted."""
import uuid

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


def usdc_balance():
    accts = _to_dict(client().get_accounts(limit=250))
    total = 0.0
    for a in accts.get("accounts", []):
        if a.get("currency") in ("USDC", "USD"):
            total += float((a.get("available_balance") or {}).get("value") or 0)
    return total


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
    size = f"{notional_usd / limit_price:.8f}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="buy", notional_usd=notional_usd, limit_price=limit_price,
                      status="submitted", detail=None)
    r = _to_dict(client().limit_order_gtc_buy(client_order_id=oid, product_id=product_id,
                                              base_size=size, limit_price=f"{limit_price:.8g}"))
    return _placed(oid, r)


def limit_sell(product_id, qty, limit_price):
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="sell", notional_usd=qty * limit_price, limit_price=limit_price,
                      status="submitted", detail=None)
    r = _to_dict(client().limit_order_gtc_sell(client_order_id=oid, product_id=product_id,
                                               base_size=f"{qty:.8f}",
                                               limit_price=f"{limit_price:.8g}"))
    return _placed(oid, r)


def market_sell(product_id, qty):
    """Risk-off escalation only: a worse fill beats an unfilled exit."""
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="sell", notional_usd=None, limit_price=None,
                      status="submitted_market", detail=None)
    r = _to_dict(client().market_order_sell(client_order_id=oid, product_id=product_id,
                                            base_size=f"{qty:.8f}"))
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
