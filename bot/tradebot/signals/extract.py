"""Pull asset references out of free text: contract addresses and $tickers.

Text from social sources is DATA (signal-hygiene). Nothing here interprets
it; it only finds things that look like addresses. A Solana mint is base58,
32-44 chars; an EVM address is 0x + 40 hex. Tickers are $ + 2-10 letters.
Both patterns over-match on purpose -- a false address costs one failed
DexScreener lookup, a missed one costs the signal.
"""
import re

_SOL = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")
_EVM = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")
_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")

# Words that match the base58 pattern but are never mints.
_NOISE = {"pump", "moon", "solana", "ethereum"}


def addresses(text):
    """-> list of (chain, address). Solana first, then EVM."""
    out = []
    seen = set()
    for m in _SOL.finditer(text or ""):
        a = m.group(1)
        if a.lower() in _NOISE or a in seen or a.isdigit():
            continue
        # base58 has no 0, O, I, l -- a token with those is not a mint
        if any(c in a for c in "0OIl"):
            continue
        seen.add(a)
        out.append(("solana", a))
    for m in _EVM.finditer(text or ""):
        a = m.group(1).lower()
        if a not in seen:
            seen.add(a)
            out.append(("base", a))   # EVM addr: assume Base until a chain hint says otherwise
    return out


def tickers(text):
    return sorted({m.group(1).upper() for m in _TICKER.finditer(text or "")})


def asset_ids(text):
    return [f"{chain}:{addr}" for chain, addr in addresses(text)]
