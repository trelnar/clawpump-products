# Trading Bot Setup

One-time setup, in order. Skills referenced live in `.agents/skills/`.

## 1. Telegram bot (~5 min)

- [ ] Message **@BotFather** → `/newbot` → pick a name and username → copy the **bot token**
- [ ] Open your new bot's chat and press **Start**
- [ ] Message **@userinfobot** → note your numeric **user ID**
- [ ] Keep token + user ID for the VPS secrets file (`approval-gate`, `vps-ops`)

## 2. VPS (~30 min)

- [ ] Provider: Hetzner / DigitalOcean / Vultr; ~2 vCPU / 4 GB; US region
- [ ] Ubuntu 24.04 LTS, SSH key added at creation
- [ ] Harden per `vps-ops`: non-root user, ufw (SSH only), fail2ban, unattended upgrades, NTP
- [ ] Note the static IP for exchange key allowlists
- [ ] healthchecks.io: create one check, note the ping URL, set its own Telegram/email notification (dead-man's switch)

## 3. Coinbase (~15 min)

- [ ] Create a dedicated **portfolio** (e.g. "Bot") in Advanced Trade
- [ ] Move the bot's trading capital into it
- [ ] API key scoped to that portfolio: **View + Trade only — no Transfer** — IP-allowlisted to the VPS
- [ ] Save key + secret for the secrets file

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
