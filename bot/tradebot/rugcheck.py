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
    age = (now - created / 1000.0) if created is not None else None
    dex = (info.get("dex") or "").lower()
    m = {"liquidity_usd": round(liq), "age_min": None if age is None else int(age / 60),
         "dex": dex}
    if dex in config.RUG_BLOCKED_DEXES:
        return False, f"still on the {dex} bonding curve", m
    if liq < config.RUG_MIN_LIQUIDITY_USD:
        return False, (f"only ${liq:,.0f} of liquidity "
                       f"(I want ${config.RUG_MIN_LIQUIDITY_USD:,.0f}+)"), m
    if age is None:
        return False, "I can't tell how old the pool is", m
    if age < config.RUG_MIN_PAIR_AGE_SEC:
        return False, (f"the pool is only {int(age / 60)} min old "
                       f"(I want {config.RUG_MIN_PAIR_AGE_SEC // 60}+)"), m
    return True, None, m


class Unbounded(RuntimeError):
    """The holder share cannot be bounded from what could be read."""


def _census(mint):
    """Every token account of the mint, (owner, amount), via getProgramAccounts.
    Exhaustive when the RPC allows it; None when it does not."""
    from .exchanges import solana_dex
    try:
        res = solana_dex._rpc("getProgramAccounts", [
            solana_dex.TOKEN_PROGRAM,
            {"encoding": "jsonParsed", "commitment": "confirmed",
             "filters": [{"dataSize": 165}, {"memcmp": {"offset": 0, "bytes": mint}}]}])
    except Exception as e:
        journal.log_event("rug_census_unavailable", f"solana:{mint}", str(e)[:120])
        return None
    out = []
    for acc in res or []:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            out.append((info["owner"], int(info["tokenAmount"]["amount"])))
        except (TypeError, KeyError, ValueError):
            continue
    return out or None


def top_holders_share(mint, pair_address=None, top=10):
    """Share of supply in the largest `top` wallets, AMM vaults excluded.

    Sound when a full census is available. Otherwise only the 20 largest
    token accounts are visible: the pool must be positively identified
    (no guessing which account is the vault), every owner must be readable,
    and the visible accounts must cover RUG_MIN_HOLDER_COVERAGE of the
    non-pool supply -- else raises Unbounded, which the gate treats as a
    refusal. A lower bound is never reported as a safe upper bound."""
    from .exchanges import solana_dex
    supply = solana_dex._rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    total = int((supply.get("value") or {}).get("amount") or 0)
    if total <= 0:
        raise RuntimeError("supply unreadable")
    vaults = set(AMM_AUTHORITIES)
    if pair_address:
        vaults.add(pair_address)
    census = _census(mint)
    if census is not None:
        rows, full = census, True
    else:
        largest = solana_dex._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        accts = [(a["address"], int(a.get("amount") or 0)) for a in (largest.get("value") or [])]
        if not accts:
            raise RuntimeError("holders unreadable")
        res = solana_dex._rpc("getMultipleAccounts",
                              [[a for a, _ in accts],
                               {"encoding": "jsonParsed", "commitment": "confirmed"}])
        rows, full = [], False
        for (addr, amt), acc in zip(accts, res.get("value") or []):
            try:
                rows.append((acc["data"]["parsed"]["info"]["owner"], amt))
            except (TypeError, KeyError):
                raise Unbounded("could not read who owns one of the largest accounts")
    vault_amt = sum(amt for o, amt in rows if o in vaults)
    if vault_amt <= 0:
        raise Unbounded("could not tell the pool's own account from the holders")
    held = [(o, amt) for o, amt in rows if o not in vaults]
    non_pool = total - vault_amt
    coverage = (sum(amt for _o, amt in held) / non_pool) if non_pool > 0 else 1.0
    if not full and coverage < config.RUG_MIN_HOLDER_COVERAGE:
        raise Unbounded(f"I could only see {coverage:.0%} of the holders")
    # A wallet, not a token account: one deployer spread over nineteen
    # accounts is one holder. Sum by owner before ranking.
    by_owner = {}
    for o, amt in held:
        by_owner[o] = by_owner.get(o, 0) + amt
    ranked = sorted(by_owner.values(), reverse=True)
    share = sum(ranked[:top]) / total
    return share, {"top10_share": round(share, 3), "accounts_read": len(rows),
                   "wallets": len(by_owner), "census": full,
                   "coverage": round(coverage, 3)}


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
        except Unbounded as e:
            journal.log_event("rug_holders_unbounded", f"{chain}:{address}", str(e)[:120])
            return False, str(e), m
        except Exception as e:
            # A blind read is not a pass: the asset is not known to be safe.
            journal.log_event("rug_holders_unreadable", f"{chain}:{address}", str(e)[:120])
            return False, "I could not read who holds it", m
    return True, None, m
