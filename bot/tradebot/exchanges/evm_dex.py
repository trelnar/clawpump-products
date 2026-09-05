"""execution venue: Base (EVM) via the KyberSwap aggregator (keyless API).
Same exit-safety contract as Solana. One EVM key serves every EVM chain."""
import requests

from .. import config, journal

CHAIN = "base"
CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
KYBER = "https://aggregator-api.kyberswap.com/base/api/v1"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


def _account():
    from eth_account import Account
    with open(config.EVM_KEYFILE) as f:
        return Account.from_key(f.read().strip())


def address():
    return _account().address


def _w3():
    from web3 import Web3
    return Web3(Web3.HTTPProvider(config.BASE_RPC))


def eth_balance():
    w3 = _w3()
    return w3.eth.get_balance(address()) / 1e18


ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


def allowance(token, owner, spender):
    w3 = _w3()
    c = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
    return c.functions.allowance(w3.to_checksum_address(owner),
                                 w3.to_checksum_address(spender)).call()


def token_balance(token):
    w3 = _w3()
    c = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
    return c.functions.balanceOf(address()).call(), c.functions.decimals().call()


def usdc_balance():
    raw, dec = token_balance(USDC)
    return raw / 10 ** dec


def route(token_in, token_out, amount_raw):
    r = requests.get(f"{KYBER}/routes", params={
        "tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount_raw)}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"kyber route: {j}")
    return j["data"]


def exit_safety(token, notional_usd, max_tax=None):
    max_tax = max_tax if max_tax is not None else config.EXIT_SAFETY_MAX_TAX
    usdc_raw = int(notional_usd * 1_000_000)
    measured = {}
    try:
        buy = route(USDC, token, usdc_raw)
        out_amt = int(buy["routeSummary"]["amountOut"])
        measured["buy_out"] = out_amt
        sell = route(token, USDC, out_amt)
        back = int(sell["routeSummary"]["amountOut"])
        measured["sell_back_usdc"] = back / 1e6
        loss = 1 - back / usdc_raw
        measured["roundtrip_loss"] = round(loss, 4)
        ceiling = 2 * 0.03 + max_tax
        ok = loss <= ceiling
        reason = None if ok else f"roundtrip_loss {loss:.1%} > {ceiling:.1%}"
    except Exception as e:
        ok, reason = False, f"route_failed: {e}"
    journal.log_exit_check(f"base:{token}", token, "PASS" if ok else "FAIL", reason, measured)
    return ok, reason, measured


def _ensure_allowance(w3, acct, token, spender, amount, nonce):
    """Approve only when needed, then PROVE the approval took before moving on.

    The first Base round trip died with TransferHelper: TRANSFER_FROM_FAILED --
    the router could not pull the USDC, i.e. no effective allowance at the
    moment the swap was estimated. The old code sent an approve and trusted
    the receipt without reading its status or re-reading the allowance, so a
    reverted approve or a lagging node produced a swap failure with no
    explanation. Every step now logs what it saw."""
    import time as _t
    have = allowance(token, acct.address, spender)
    journal.log_event("evm_allowance", detail={"spender": spender, "have": have, "need": amount})
    if have >= amount:
        return
    c = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
    tx = c.functions.approve(spender, amount).build_transaction({
        "from": acct.address, "nonce": nonce, "chainId": CHAIN_ID,
        "gasPrice": w3.eth.gas_price})
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    # to_hex, not .hex(): under web3 7 / hexbytes 1 the latter drops the 0x
    # prefix, and a receipt lookup on a bare hash is how a landed swap turns
    # into "timeout" and an orphaned holding.
    h = w3.to_hex(w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction))
    rec = w3.eth.wait_for_transaction_receipt(h, timeout=90)
    journal.log_event("evm_approve", detail={"tx": h, "status": rec["status"],
                                             "block": rec["blockNumber"]})
    if rec["status"] != 1:
        raise RuntimeError(f"approve reverted: {h}")
    for _ in range(10):                      # the node must SEE it before we lean on it
        if allowance(token, acct.address, spender) >= amount:
            return
        _t.sleep(1)
    raise RuntimeError(f"approve mined ({h}) but allowance still below {amount}")


def swap(token_in, token_out, amount_raw, slippage_bps):
    """Build via Kyber, approve if needed, sign locally, send, return tx hash."""
    w3 = _w3()
    acct = _account()
    rt = route(token_in, token_out, amount_raw)
    rb = requests.post(f"{KYBER}/route/build", json={
        "routeSummary": rt["routeSummary"], "sender": acct.address,
        "recipient": acct.address, "slippageTolerance": slippage_bps}, timeout=20)
    rb.raise_for_status()
    data = rb.json()["data"]
    router = w3.to_checksum_address(data["routerAddress"])
    # Kyber names the contract we are about to approve and call. Without an
    # allowlist, a compromised aggregator response points both at an address
    # of its choosing and we sign the approval for it.
    if config.EVM_ROUTER_ALLOWLIST and router.lower() not in {
            a.lower() for a in config.EVM_ROUTER_ALLOWLIST}:
        raise RuntimeError(f"router not allowlisted: {router}")

    nonce = w3.eth.get_transaction_count(acct.address)
    if token_in != NATIVE:
        _ensure_allowance(w3, acct, token_in, router, int(amount_raw), nonce)
        nonce = w3.eth.get_transaction_count(acct.address)

    tx = {"from": acct.address, "to": router, "data": data["data"],
          "value": int(amount_raw) if token_in == NATIVE else 0,
          "nonce": nonce, "chainId": CHAIN_ID,
          "gasPrice": w3.eth.gas_price}
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    if config.SIMULATE_BEFORE_SEND:
        w3.eth.call(tx)   # reverts here rather than costing gas on-chain
    signed = acct.sign_transaction(tx)
    h = w3.to_hex(w3.eth.send_raw_transaction(signed.raw_transaction))
    journal.log_order(client_oid=h, venue="base",
                      asset_id=f"base:{token_out if token_in == USDC else token_in}",
                      side="buy" if token_in == USDC else "sell",
                      notional_usd=None, limit_price=None, status="sent", detail=None)
    return h


def confirm(tx_hash):
    w3 = _w3()
    try:
        rec = w3.eth.get_transaction_receipt(tx_hash)
        return "confirmed" if rec and rec["status"] == 1 else "failed"
    except Exception:
        return "unknown"
