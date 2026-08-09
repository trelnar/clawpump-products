# MAYFLY signal sources — endpoint cookbook

Free, keyless, public APIs first. Endpoints drift; these were written from documented public interfaces but **could not be network-verified from the build sandbox — verify each with one request on first run**, and on repeated 4xx fall through to the next source. Never enter on a single unconfirmed source: one source to *find* a candidate, a second to *confirm* the numbers.

Be polite: cache per-token reads for 30–60s, stay well under ~1 req/sec per host, and back off on 429.

## DexScreener (find + confirm)

| Purpose | Endpoint |
|---|---|
| Latest paid boosts (hype-purchase feed) | `GET https://api.dexscreener.com/token-boosts/latest/v1` |
| Most-boosted right now | `GET https://api.dexscreener.com/token-boosts/top/v1` |
| Newly published token profiles | `GET https://api.dexscreener.com/token-profiles/latest/v1` |
| Full pair stats for a token | `GET https://api.dexscreener.com/latest/dex/tokens/{address}` |
| Resolve a ticker (CALL mode) | `GET https://api.dexscreener.com/latest/dex/search?q={ticker}` |

Fields that feed the pipeline (from pair objects):

- `priceUsd`, `priceChange.m5/.h1` — C3, C4, chase guard
- `volume.m5/.h1` — C1, G8, dead-tape exit (entry-time pace = `volume.m5` at fill)
- `txns.m5.buys/.sells` — C2, G7 (sells present)
- `liquidity.usd` — G5, C7 impact estimate, and the priority-1 rug exit (compare vs entry-time value)
- `pairCreatedAt` — G6 age
- `fdv`, `marketCap` — context only, no rule keys off them

Ticker resolution rule: highest `liquidity.usd` match wins; if the symbol collides with a major token, resolve by contract address only or decline (G9).

## GeckoTerminal (find + confirm)

Base: `https://api.geckoterminal.com/api/v2`

| Purpose | Endpoint |
|---|---|
| Trending pools (Solana) | `GET /networks/solana/trending_pools?page=1` |
| Newest pools | `GET /networks/solana/new_pools?page=1` |
| Pool detail / OHLCV | `GET /networks/solana/pools/{pool}` · `…/pools/{pool}/ohlcv/minute` |

Use `volume_usd.m5/.h1`, `transactions.m5`, `reserve_in_usd` (liquidity), `pool_created_at`. OHLCV minute bars back the C3 structure check (price vs 15 min ago) when DexScreener granularity isn't enough.

## RugCheck (gate)

`GET https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary` — Solana safety report: mint/freeze authority (G1/G2), LP lock status (G3), top holders (G4), plus their own risk score. Free tier is rate-limited; cache aggressively. If GATEKEEPER is installed it owns the gate and this is just its data source; the built-in gate uses it directly.

Holder concentration cross-check when needed: Solana RPC `getTokenLargestAccounts` on the mint.

## Pump.fun (find — unofficial)

The pre-graduation window (bonding curve 70–95%) comes from pump.fun's **unofficial** frontend API (`frontend-api.pump.fun/coins?...`, sortable by curve progress / king-of-the-hill). Unofficial means: undocumented, unversioned, breaks without notice, sometimes bot-gated. Treat as find-only, always confirm on DexScreener/GeckoTerminal once the token has a pool — and if it has no pool yet, it fails G5/G6 anyway and MAYFLY doesn't touch it. If the endpoint is down, skip the source; do not scrape the website.

## CALL mode parsing

- Solana contract address: `[1-9A-HJ-NP-Za-km-z]{32,44}` (base58, no `0OIl`)
- EVM address (decline politely — MAYFLY is Solana-only for now): `0x[a-fA-F0-9]{40}`
- Ticker: `\$[A-Za-z]{2,10}`
- On multiple CAs in one message: treat each as its own candidate; on zero CAs and one ticker: resolve per the DexScreener rule above.
- Record `call_price` (current `priceUsd`) the moment the call is parsed — the chase guard (C5) is measured from here, not from the caller's claimed entry.

## Source hygiene

- Paid boosts and paid trending slots are advertising bought by the team. They mark *attention*, not quality — a boost is a candidate flag and simultaneously a reminder that someone budgeted for your exit.
- Discord/Telegram calls arrive via paste, webhook forward, or a bot account running with the server's permission. Self-botting a personal account violates Discord ToS.
- Log the raw signal payload (source, time, price-at-signal) with every ledger row — the caller scorecard and any later post-mortem are only as good as this record.
