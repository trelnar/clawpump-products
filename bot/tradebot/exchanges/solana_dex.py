"""execution venue: Solana DEX via Jupiter aggregator. Exit-safety = a real
round-trip quote at intended size. Keys never leave this process."""
import base64
import json

import requests

from .. import config, journal

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "https://quote-api.jup.ag/v6"
LAMPORTS = 1_000_000_000


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
               [address(), {"mint": USDC_MINT}, {"encoding": "jsonParsed"}])
    total = 0.0
    for acct in res["value"]:
        total += float(acct["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return total


def token_balance(mint):
    res = _rpc("getTokenAccountsByOwner",
               [address(), {"mint": mint}, {"encoding": "jsonParsed"}])
    amt, dec = 0, 0
    for acct in res["value"]:
        ta = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
        amt += int(ta["amount"])
        dec = int(ta["decimals"])
    return amt, dec


def quote(input_mint, output_mint, amount_raw, slippage_bps):
    r = requests.get(f"{JUP}/quote", params={
        "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
        "slippageBps": slippage_bps, "restrictIntermediateTokens": "true"}, timeout=15)
    r.raise_for_status()
    return r.json()


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


def swap(input_mint, output_mint, amount_raw, slippage_bps):
    """Quote -> signed VersionedTransaction -> send. Confirm by signature."""
    from solders.transaction import VersionedTransaction
    kp = _keypair()
    q = quote(input_mint, output_mint, amount_raw, slippage_bps)
    r = requests.post(f"{JUP}/swap", json={
        "quoteResponse": q, "userPublicKey": str(kp.pubkey()),
        "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto"}, timeout=20)
    r.raise_for_status()
    tx_b64 = r.json()["swapTransaction"]
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = VersionedTransaction(tx.message, [kp])
    sig = _rpc("sendTransaction",
               [base64.b64encode(bytes(signed)).decode(), {"encoding": "base64",
                                                           "skipPreflight": False}])
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
