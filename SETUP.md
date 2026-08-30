# Trading Bot Setup

One-time setup, in order. Skills referenced live in `.agents/skills/`.

**Server IP (for API allowlists):** `107.191.39.195`

## 1. Telegram bot (~5 min)

- [x] Telegram user ID: `6674587758`
- [ ] Message **@BotFather** → `/newbot` → pick a name and username → copy the **bot token**
- [ ] Open your new bot's chat and press **Start**
- [ ] Message **@userinfobot** → note your numeric **user ID**
- [ ] Keep token + user ID for the VPS secrets file (`approval-gate`, `vps-ops`)

## 2. VPS (~30 min)

- [x] Vultr `vhp-2c-4gb-amd`, New York (NJ), Ubuntu 24.04 LTS
- [x] Hardened at first boot via the `harden` startup script (`scripts/harden.sh`) — verified `/etc/tradebot/.harden-ok`
- [x] Static IP noted: `107.191.39.195`
- [ ] Add your Mac SSH key (console → `/home/bot/.ssh/authorized_keys`), then SSH in as `bot`
- [ ] healthchecks.io: create one check, note the ping URL, set its own Telegram/email notification (dead-man's switch)

## 3. Coinbase (~15 min)

- [x] Dedicated portfolio created: **HypeBot**
- [x] API key `HypeBot-API` — View + Trade only, no Transfer/Receive, Ed25519, IP-allowlisted to the VPS; saved to password manager
- [ ] Fund the portfolio (25% of total trading capital)

## 4. Equities API (start now — slowest item)

- [ ] E*TRADE: developer.etrade.com → accept API agreement → request production key (days–weeks; tokens need daily manual renewal)
- [ ] In parallel: open an **Alpaca** account (instant keys, built for automation) and fund it as the primary automated equities venue

## 5. Hot wallets (~15 min, on the VPS)

Two keypairs cover every chain: one Solana, one EVM. The EVM key yields the same address on Base, BNB Chain, Arbitrum, and Ethereum.

- [ ] Generate a **fresh** Solana keypair on the VPS (`solana-keygen new`) — never reuse a personal wallet
- [ ] Generate a **fresh** EVM keypair on the VPS — never reuse a personal wallet
- [ ] Both keyfiles at mode `0600` in `/etc/tradebot/`
- [ ] Fund per-chain allocations, held as USDC where available (`capital-allocation`)
- [ ] Fund each chain's native gas float: SOL; ETH on Base/Arbitrum/Ethereum; BNB on BNB Chain
- [ ] Confirm an RPC endpoint and router for each enabled chain (`execution` chain registry)

## 6. Capital

- [ ] Decide the total
- [ ] Split per the `capital-allocation` table; fund each venue
- [ ] Then run the `go-live` Phase 0 checklist
