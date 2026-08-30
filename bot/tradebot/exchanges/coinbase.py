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
                             api_secret=config.COINBASE_API_SECRET)
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


def limit_buy(product_id, notional_usd, limit_price):
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    size = f"{notional_usd / limit_price:.8f}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="buy", notional_usd=notional_usd, limit_price=limit_price,
                      status="submitted", detail=None)
    r = _to_dict(client().limit_order_gtc_buy(client_order_id=oid, product_id=product_id,
                                              base_size=size, limit_price=f"{limit_price:.8g}"))
    return oid, r


def limit_sell(product_id, qty, limit_price):
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="sell", notional_usd=qty * limit_price, limit_price=limit_price,
                      status="submitted", detail=None)
    r = _to_dict(client().limit_order_gtc_sell(client_order_id=oid, product_id=product_id,
                                               base_size=f"{qty:.8f}",
                                               limit_price=f"{limit_price:.8g}"))
    return oid, r


def market_sell(product_id, qty):
    """Risk-off escalation only: a worse fill beats an unfilled exit."""
    oid = f"tb-{uuid.uuid4().hex[:20]}"
    journal.log_order(client_oid=oid, venue="coinbase", asset_id=f"cex:{product_id}",
                      side="sell", notional_usd=None, limit_price=None,
                      status="submitted_market", detail=None)
    r = _to_dict(client().market_order_sell(client_order_id=oid, product_id=product_id,
                                            base_size=f"{qty:.8f}"))
    return oid, r


def order_status(oid):
    """Query-before-retry idempotency: never blind-resubmit."""
    try:
        r = _to_dict(client().list_orders(client_order_id=oid))
        orders = r.get("orders") or []
        return orders[0] if orders else None
    except Exception as e:
        journal.log_event("order_status_fail", detail=f"{oid}: {e}")
        return None


def cancel(order_id):
    return _to_dict(client().cancel_orders(order_ids=[order_id]))
