"""Rug filter: is this token worth holding for six hours at all?

Three of the first five real exits (2026-09-13) were rugs -- -100%, -99% six
minutes after the buy, -71% -- on tokens that had cleared a $5,000 liquidity
bar. A stop-loss is a promise the pool has to keep; a pulled pool keeps none.
This module asks the questions a stop cannot: is there enough liquidity that
one wallet cannot drain it, has the pair lived long enough for the deployer's
first dump to have happened already, is it off the bonding curve, and do the
top wallets hold so much that a single sell zeroes it.

Two layers. `check_pair` needs only the DexScreener pair the bot already reads
and runs before research, so nothing ruggable costs a model call. `check` adds
the on-chain holder read and runs at the buy gate. Both return (ok, reason,
measured); the reason is written for the operator's phone.
"""
import time

from . import config, journal

# Token accounts owned by these are AMM vaults, not holders. The pair's own
# address is added per call (PumpSwap and most CPMMs park the vault under
# the pool). Raydium AMM v4 and CPMM use a shared authority instead.
AMM_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # Raydium AMM v4
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",   # Raydium CPMM
    "GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ",   # Raydium CLMM
}


def check_pair(info, now=None):
    """Liquidity, age and venue, from the pair the bot already fetched."""
    now = now or time.time()
    if not info:
        return False, "no pair data", {}
    liq = float(info.get("liquidity_usd") or 0)
    created = info.get("created_ms")
    age = (now - created / 1000.0) if created else None
    dex = (info.get("dex") or "").lower()
    m = {"liquidity_usd": round(liq), "age_min": None if age is None else int(age / 60),
         "dex": dex}
    if dex in config.RUG_BLOCKED_DEXES:
        return False, f"still on the {dex} bonding curve", m
    if liq < config.RUG_MIN_LIQUIDITY_USD:
        return False, (f"only ${liq:,.0f} of liquidity "
                       f"(I want ${config.RUG_MIN_LIQUIDITY_USD:,.0f}+)"), m
    if age is not None and age < config.RUG_MIN_PAIR_AGE_SEC:
        return False, (f"the pool is only {int(age / 60)} min old "
                       f"(I want {config.RUG_MIN_PAIR_AGE_SEC // 60}+)"), m
    return True, None, m


def top_holders_share(mint, pair_address=None, top=10):
    """Share of supply in the largest `top` wallets, AMM vaults excluded.

    Returns (share, measured). Keyless Solana RPC: the 20 largest token
    accounts, their owners, and the supply. When no vault can be identified
    the single largest account is assumed to be the pool -- on every
    graduated token that is what it is -- and the assumption is recorded."""
    from .exchanges import solana_dex
    largest = solana_dex._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    accts = [(a["address"], int(a.get("amount") or 0)) for a in (largest.get("value") or [])]
    supply = solana_dex._rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    total = int((supply.get("value") or {}).get("amount") or 0)
    if total <= 0 or not accts:
        raise RuntimeError("supply or holders unreadable")
    owners = {}
    if accts:
        res = solana_dex._rpc("getMultipleAccounts",
                              [[a for a, _ in accts],
                               {"encoding": "jsonParsed", "commitment": "confirmed"}])
        for (addr, _amt), acc in zip(accts, res.get("value") or []):
            try:
                owners[addr] = acc["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                owners[addr] = None
    vaults = set(AMM_AUTHORITIES)
    if pair_address:
        vaults.add(pair_address)
    holders = [(a, amt) for a, amt in accts if owners.get(a) not in vaults]
    assumed = False
    if len(holders) == len(accts) and holders:
        holders = holders[1:]          # no vault found: the largest is the pool
        assumed = True
    holders.sort(key=lambda x: -x[1])
    share = sum(amt for _a, amt in holders[:top]) / total
    return share, {"top10_share": round(share, 3), "accounts_read": len(accts),
                   "vaults_excluded": len(accts) - len(holders) - (1 if assumed else 0),
                   "pool_assumed": assumed}


def check(chain, address, info, now=None):
    """The full filter for the buy gate. Solana adds the holder read; Base
    has no keyless holder index and relies on liquidity, age and venue."""
    ok, reason, m = check_pair(info, now)
    if not ok:
        return ok, reason, m
    if chain == "solana" and config.RUG_HOLDER_CHECK:
        try:
            share, hm = top_holders_share(address, (info or {}).get("pair_address"))
            m.update(hm)
            if share > config.RUG_MAX_TOP10_SHARE:
                return False, (f"the top 10 wallets hold {share:.0%} of the supply "
                               f"(I want under {config.RUG_MAX_TOP10_SHARE:.0%})"), m
        except Exception as e:
            # A blind read is not a pass: the asset is not known to be safe.
            journal.log_event("rug_holders_unreadable", f"{chain}:{address}", str(e)[:120])
            return False, "I could not read who holds it", m
    return True, None, m
