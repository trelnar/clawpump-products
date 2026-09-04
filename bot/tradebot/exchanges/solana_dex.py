"""execution venue: Solana DEX via Jupiter aggregator. Exit-safety = a real
round-trip quote at intended size. Keys never leave this process."""
import base64
import json

import requests

from .. import config, journal

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS = 1_000_000_000

_jup_base = None


def jupiter(path, method="GET", **kw):
    """Call Jupiter, resolving which base URL currently works.

    The hardcoded quote-api.jup.ag stopped resolving and took every Solana swap
    with it -- a DNS failure, not an API error, so nothing in the code could
    have adapted. Bases are tried in order and the winner is remembered for the
    life of the process; a base that later dies simply falls through to the
    next one on the following call."""
    global _jup_base
    order = ([_jup_base] if _jup_base else []) + \
            [b for b in config.JUPITER_BASES if b != _jup_base]
    last = None
    for base in order:
        try:
            fn = requests.post if method == "POST" else requests.get
            r = fn(f"{base}{path}", timeout=kw.pop("timeout", 20), **kw)
            r.raise_for_status()
            if base != _jup_base:
                journal.log_event("jupiter_base", detail=base)
                _jup_base = base
            return r.json()
        except Exception as e:
            last = e
            kw.setdefault("timeout", 20)
    raise RuntimeError(f"Jupiter unreachable on {order}: {last}")


def _keypair():
    from solders.keypair import Keypair
    with open(config.SOLANA_KEYFILE) as f:
        return Keypair.from_bytes(bytes(json.load(f)))


def address():
    return str(_keypair().pubkey())


def _rpc(method, params):
    r = requests.post(config.SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1,
                                               "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"rpc {method}: {j['error']}")
    return j["result"]


def sol_balance():
    return _rpc("getBalance", [address()])["value"] / LAMPORTS


def usdc_balance():
    res = _rpc("getTokenAccountsByOwner",
               [address(), {"mint": USDC_MINT},
                {"encoding": "jsonParsed", "commitment": "confirmed"}])
    total = 0.0
    for acct in res["value"]:
        total += float(acct["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return total


def token_balance(mint):
    # "confirmed", not the RPC default of "finalized": a swap we have just
    # confirmed is ~13s from finalization, and reading at finalized returns the
    # PRE-swap balance -- which the caller cannot distinguish from a zero fill.
    res = _rpc("getTokenAccountsByOwner",
               [address(), {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"}])
    amt, dec = 0, 0
    for acct in res["value"]:
        ta = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
        amt += int(ta["amount"])
        dec = int(ta["decimals"])
    return amt, dec


TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def all_token_balances():
    """Every non-zero SPL balance the wallet holds. Reconciliation needs to ask
    'what do we actually own' -- not merely 'is what we think we own still
    there' -- because the failures worth catching are holdings the database
    does not know about at all."""
    res = _rpc("getTokenAccountsByOwner",
               [address(), {"programId": TOKEN_PROGRAM},
                {"encoding": "jsonParsed", "commitment": "confirmed"}])
    out = {}
    for acct in res.get("value") or []:
        info = acct["account"]["data"]["parsed"]["info"]
        amt = info.get("tokenAmount") or {}
        if int(amt.get("amount") or 0) > 0:
            out[info["mint"]] = float(amt.get("uiAmount") or 0)
    return out


def token_decimals(mint):
    """Authoritative decimals from the mint account. token_balance() reports 0
    when no associated account exists yet, so raw->whole conversion cannot rely
    on it for a first buy."""
    res = _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    v = res.get("value")
    if not v:
        raise RuntimeError(f"mint account not found: {mint}")
    return int(v["data"]["parsed"]["info"]["decimals"])


def quote(input_mint, output_mint, amount_raw, slippage_bps):
    return jupiter("/quote", params={
        "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
        "slippageBps": slippage_bps, "restrictIntermediateTokens": "true"})


def exit_safety(mint, notional_usd, max_tax=None):
    """execution gate 4: buy quote + immediate sell quote of the resulting
    size. Fails only on cannot-sell, tax over cap, or insufficient exit depth.
    Never rejects for age/holders/hype."""
    max_tax = max_tax if max_tax is not None else config.EXIT_SAFETY_MAX_TAX
    usdc_raw = int(notional_usd * 1_000_000)
    measured = {}
    try:
        buy = quote(USDC_MINT, mint, usdc_raw, 300)
        out_amount = int(buy["outAmount"])
        measured["buy_out"] = out_amount
        sell = quote(mint, USDC_MINT, out_amount, 300)
        back = int(sell["outAmount"])
        measured["sell_back_usdc"] = back / 1e6
        roundtrip_loss = 1 - back / usdc_raw
        measured["roundtrip_loss"] = round(roundtrip_loss, 4)
        # round-trip loss bundles spread+impact+any transfer tax; cap it
        ceiling = 2 * 0.03 + max_tax  # both-ways slippage tier cap + tax cap
        ok = roundtrip_loss <= ceiling
        reason = None if ok else f"roundtrip_loss {roundtrip_loss:.1%} > {ceiling:.1%}"
    except Exception as e:
        ok, reason = False, f"quote_failed: {e}"
    journal.log_exit_check(f"solana:{mint}", mint, "PASS" if ok else "FAIL", reason, measured)
    return ok, reason, measured


def simulate(signed_tx_b64):
    """Dry-run before broadcast. Jupiter builds the transaction and we sign it
    blind otherwise -- a compromised or hijacked aggregator response would be
    signed just as readily as a swap. A simulation that errors is a refusal."""
    res = _rpc("simulateTransaction",
               [signed_tx_b64, {"encoding": "base64", "commitment": "confirmed",
                                "replaceRecentBlockhash": True}])
    val = res.get("value") or {}
    if val.get("err"):
        raise RuntimeError(f"simulation failed: {val['err']} "
                           f"{(val.get('logs') or [])[-3:]}")
    return val


def swap(input_mint, output_mint, amount_raw, slippage_bps):
    """Quote -> signed VersionedTransaction -> simulate -> send."""
    from solders.transaction import VersionedTransaction
    kp = _keypair()
    q = quote(input_mint, output_mint, amount_raw, slippage_bps)
    tx_b64 = jupiter("/swap", method="POST", json={
        "quoteResponse": q, "userPublicKey": str(kp.pubkey()),
        "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto"})["swapTransaction"]
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = VersionedTransaction(tx.message, [kp])
    raw_b64 = base64.b64encode(bytes(signed)).decode()
    if config.SIMULATE_BEFORE_SEND:
        simulate(raw_b64)   # raises, and the caller never broadcasts
    sig = _rpc("sendTransaction",
               [raw_b64, {"encoding": "base64", "skipPreflight": False}])
    journal.log_order(client_oid=sig, venue="solana",
                      asset_id=f"solana:{output_mint if input_mint == USDC_MINT else input_mint}",
                      side="buy" if input_mint == USDC_MINT else "sell",
                      notional_usd=None, limit_price=None, status="sent", detail=None)
    return sig, q


def confirm(signature):
    """Query-before-retry: a dropped tx can still land later."""
    res = _rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
    st = res["value"][0]
    if st is None:
        return "unknown"
    if st.get("err"):
        return "failed"
    return st.get("confirmationStatus") or "processed"
