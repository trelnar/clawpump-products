#!/usr/bin/env python3
"""Move USDC from the bot's wallet on ARBITRUM into Hyperliquid.

Hyperliquid is funded through its Arbitrum bridge: a plain USDC transfer to
the bridge contract from an address credits that same address on
Hyperliquid within a minute. So: withdraw USDC from Coinbase to the bot's
EVM address on the Arbitrum network (plus ~$2 of ETH on Arbitrum for gas),
then run this as the bot user:

    sudo -u bot /opt/tradebot/venv/bin/python /opt/tradebot/scripts/hl_deposit.py 50

It sends the given amount (default: everything above $1). Minimum 5 USDC.
"""
import os
import sys
import time

sys.path.insert(0, "/opt/tradebot/bot")
from tradebot import config  # noqa: E402

ARB_RPC = os.environ.get("ARB_RPC", "https://arb1.arbitrum.io/rpc")
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
BRIDGE = "0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7"
ERC20 = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
          "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
         {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
          "inputs": [{"name": "to", "type": "address"}, {"name": "v", "type": "uint256"}],
          "outputs": [{"type": "bool"}]}]


def main():
    from eth_account import Account
    from web3 import Web3
    with open(config.EVM_KEYFILE) as f:
        acct = Account.from_key(f.read().strip())
    w3 = Web3(Web3.HTTPProvider(ARB_RPC, request_kwargs={"timeout": 30}))
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ARB), abi=ERC20)
    bal = usdc.functions.balanceOf(acct.address).call() / 1e6
    eth = w3.eth.get_balance(acct.address) / 1e18
    print(f"address {acct.address}\nArbitrum USDC {bal:.2f}, ETH {eth:.5f}")
    amount = float(sys.argv[1]) if len(sys.argv) > 1 else max(0.0, bal - 1.0)
    if amount < 5:
        print("need at least 5 USDC to deposit; nothing sent")
        return 1
    if amount > bal:
        print(f"only {bal:.2f} available; nothing sent")
        return 1
    if eth < 0.0002:
        print("no ETH on Arbitrum for gas; send ~$2 of ETH (Arbitrum network) first")
        return 1
    fn = usdc.functions.transfer(Web3.to_checksum_address(BRIDGE), int(amount * 1e6))
    base = {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
            "maxFeePerGas": w3.eth.gas_price * 2, "maxPriorityFeePerGas": 0, "chainId": 42161}
    # Arbitrum folds L1 calldata cost into gas units; a fixed number runs out
    gas = int(fn.estimate_gas({"from": acct.address}) * 1.3)
    tx = fn.build_transaction({**base, "gas": gas})
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print("sent", w3.to_hex(h), "-- waiting for the receipt")
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    print("status", rc.status, "(1 = ok). Hyperliquid credits the same address within about a minute.")
    time.sleep(5)
    return 0 if rc.status == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
