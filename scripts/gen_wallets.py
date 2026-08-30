#!/usr/bin/env python3
"""Generate the bot's two fresh keypairs ON THE VPS. Never reuse a personal
wallet. Prints addresses only; keys go to /etc/tradebot at mode 0600."""
import json
import os
import secrets
import sys

SOL_PATH = "/etc/tradebot/solana_wallet.json"
EVM_PATH = "/etc/tradebot/evm_wallet.key"


def main():
    os.umask(0o077)
    from solders.keypair import Keypair
    from eth_account import Account

    if os.path.exists(SOL_PATH):
        kp = Keypair.from_bytes(bytes(json.load(open(SOL_PATH))))
        print(f"solana (existing): {kp.pubkey()}")
    else:
        kp = Keypair()
        with open(SOL_PATH, "w") as f:
            json.dump(list(bytes(kp)), f)
        os.chmod(SOL_PATH, 0o600)
        print(f"solana (NEW):      {kp.pubkey()}")

    if os.path.exists(EVM_PATH):
        acct = Account.from_key(open(EVM_PATH).read().strip())
        print(f"evm    (existing): {acct.address}")
    else:
        key = "0x" + secrets.token_hex(32)
        acct = Account.from_key(key)
        with open(EVM_PATH, "w") as f:
            f.write(key)
        os.chmod(EVM_PATH, 0o600)
        print(f"evm    (NEW):      {acct.address}")

    print("\nFund: USDC (Solana) -> solana address; USDC (Base) + ETH gas -> evm address; SOL gas -> solana address.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
