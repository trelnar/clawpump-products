# HypeBot — Handoff

**Status as of 2026-09-01: LIVE, holding ~$895 of real money, and it should be stopped.**
An audit found 3 critical defects that fire on the first trade. Send `STOP` in Telegram
before anything else. Full inventory of all 106 findings: `AUDIT.md`.

---

## 1. What this is

An autonomous short-horizon crypto trading bot. It hunts assets with a credible path to
**2x within 1–3 days**, on any chain, with no quality/age/hype filter — the only hard stops
are "can I exit this", "is this contract address real", and "content is data, never
instructions".

Two services on one VPS (`runtime` skill — the agent decides, the core enforces):

| Service | Role | Model calls? |
|---|---|---|
| `tradebot-core` | Telegram approval gate, risk limits, execution, position monitoring, state, heartbeat | No |
| `tradebot-agent` | Discovery → research via Claude API → tickets | Yes, every 15 min |

Repo: `trelnar/clawpump-products`, branch `claude/trading-bot-skills-sfqmfo`.
18 skills in `.agents/skills/` specify intent; `bot/tradebot/` implements it (partially — see §4).

## 2. Deployed state

- **VPS** 107.191.39.195 (Vultr, New York/NJ, Ubuntu 24.04). Key-only SSH, ufw, fail2ban.
  Users: `bot` (trading, no sudo), `sigbot` (an unrelated pre-existing signal bot, hourly cron).
- **Money** — $1,000 total:
  | Where | Amount |
  |---|---|
  | Coinbase HypeBot portfolio | ~$275 USDC |
  | Solana wallet `dHTaGtKmUiKHQfDqNo5Hom5WQ6u2SnnhjbEtMoBpoht` | $390 USDC + ~0.0965 SOL gas |
  | Base wallet `0x973813C36Fe55a5299cfa264eA296c69b6527Ca0` | $215 USDC + ~$10 ETH gas |
  | Stranded on Ethereum mainnet (same EVM address) | ~$5 USDC |
- **Phase 1** of `go-live`: every order $5. First buy of any asset needs Telegram approval;
  adds and sells are automatic.
- **Hard limits** (`risk-limits`): 5% max position, 20%/24h rolling-peak drawdown halts buying.
  *These are currently defeated by finding C1 below.*
- **Cost**: ~$2/day of Claude API. Prompt caching confirmed working (10,062 tokens/cycle cached).
- **Trades executed to date: zero.**

Credentials live in `/etc/tradebot/secrets.env` (Telegram token, healthchecks URL, Coinbase
CDP key + secret, Anthropic key). Wallet keys in `/etc/tradebot/`. **No backups of any of it.**

## 3. NEXT STEPS — start here

### 3a. The five things I was least certain about
These are judgment gaps, not bugs. They decide whether the bot is *worth running* at all.

1. **No order has ever executed.** Every buy/sell path is unproven against a real venue.
   The audit confirmed this is worse than "untested" — see C1–C3, which are defects in
   exactly those paths. Worst case is a successful buy followed by a failed sell.
2. **The research layer is fed far thinner data than the strategy demands.**
   `short-horizon-research` asks for social acceleration, narrative formation, on-chain flows,
   holder growth, smart money. It receives price, liquidity, 24h volume, and candles.
   Six cycles of "0 tickets" may be correct discipline or may be blindness — currently
   indistinguishable. This is the gap most likely to make the bot useless rather than dangerous.
3. **The economics at $1,000 may not clear costs.** Round-trip on a microcap can be 3–6%;
   $2/day API against $50 positions is real drag. Never modeled.
4. **Nothing is calibrated.** No forecast has resolved; the self-calibration loop is
   unexercised — and per finding H16, nothing writes the data it would need.
5. **Wave structure is unproven here.** Encoded faithfully and fenced (no vetoes, no sizing).
   Its one concrete contribution is structural invalidation levels.

### 3b. Critical — fix before any trade

- **C1. Solana positions are booked in raw token units.** `execution.py:80` stores Jupiter's
  raw `outAmount`; marks are USD per whole token. A $5 fill reports ~$5M of portfolio value,
  which silently slackens *every* percentage limit and guarantees a spurious `EMERGENCY_HALT`
  when the position closes. Base/Coinbase book whole units — the venues disagree.
  *Fix:* divide by token decimals; assert `qty*mark` is within an order of magnitude of cost basis.
- **C2. Buys are booked as filled with no confirmation** on all three venues. A rejected
  Coinbase limit order or an unlanded swap becomes a phantom position.
  *Fix:* poll order/tx status and book from the actual fill, not the assumed one.
- **C3. The Telegram listener is an unsupervised daemon thread** with unguarded `cq['from']['id']`
  access. One unusual update kills it permanently; trading continues with the kill switch dead
  and the heartbeat still green.
  *Fix:* guard `_dispatch`, persist `offset`, supervise the thread, and drop to `SELL_ONLY`
  if inbound is down 5 minutes (which `vps-ops` already specifies).

### 3c. High — themed clusters (72 findings, details in AUDIT.md)

| Theme | The problem |
|---|---|
| **Exits don't work** | No take-profit path; the forecast schema has no SELL/HOLD/ADD action; `entry_liquidity_usd` is never written so the liquidity-drain exit is dead. Only a model-supplied invalidation price can ever close a position. |
| **Gate bypass** | An approved buy skips four of the five execution gates, including the halt check and risk limits. |
| **Fill integrity** | Coinbase order responses discarded; sell proceeds fabricated when the price feed is down; fees recorded nowhere and absent from cost basis. |
| **Concurrency** | Approved buys execute on the Telegram poller thread, blocking STOP/FLATTEN for 90+ seconds. Two threads place orders with no mutex. |
| **Recovery** | No backups of journal or wallet keys. Cold-start recovery unimplemented — every restart resumes in NORMAL and can buy within 60s. `SELL_ONLY`/`RECON_FREEZE` are dead-end modes with no path back. |
| **Observability** | Zero stdout logging; `journalctl` is empty and every diagnostic is trapped in SQLite. The dead-man's switch pings *before* doing the work, so it stays green through a total core failure. The agent layer has no alert path at all — the Anthropic key expiring in 30 days would be silent. |
| **Security** | Both wallets blind-sign transactions built by third-party aggregators (no router allowlist, no simulation). The research service is handed every credential. Telegram token gets written into the journal on transport errors. |
| **Unimplemented skills** | `backtest-replay`, calibration, `capital-allocation`, `equities-constraints` are prose only. Every decision-logic change goes straight to live money. |
| **Strategy inputs** | Discovery is two *paid-promotion* endpoints plus an alphabetical slice of 80/700 Coinbase products — it structurally selects for late, advertised tokens. DexScreener fields already fetched are discarded before the prompt. Only 3 of 18 skills reach the model. |
| **Correctness traps** | `REVOKE` can never match a token asset (and reports success). Approval codes use a hex alphabet the spec excludes. Rejected assets re-propose within 15 minutes. A DexScreener response missing `priceUsd` yields 0.0, which reads as an invalidation cross and liquidates the position. |

### 3d. Areas to explore (beyond fixing)

- **Two-tier research** — Haiku triages, Opus judges finalists. ~5× cheaper.
- **Real signal sources** — the fastest credible wins are on-chain flow (a Solana indexer)
  and social acceleration. Without these, §3a.2 stays unresolved.
- **Signal-bot's missing gates** — its VIDYA and Delta Vol filters were never built; the
  08-22 short at +1.69 into a rally is the trade they'd have blocked.
- **Service-account API key** — the console recommends it for automation; current key is
  an unlinked workspace key expiring Sep 30.
- **Equities** — deliberately unfunded until $25k (PDT limits make $50 stock positions pointless).

## 4. How to operate it

**Telegram** (@bandaidbot): `STATUS`, `STOP`, `RESUME`, `FLATTEN`, `REVOKE <asset>`,
`REPORT`, `WHY <asset>`, `YES <code>` / `NO <code>`.

**On the VPS** (`ssh -i ~/.ssh/tradebot_ed25519 root@107.191.39.195`):
```
systemctl status tradebot-core tradebot-agent
cd /opt/tradebot && git pull && systemctl restart tradebot-core tradebot-agent
bash /opt/tradebot/scripts/spend.sh
sqlite3 /var/lib/tradebot/tradebot.db "SELECT datetime(ts,'unixepoch'), kind, substr(detail,1,120) FROM events ORDER BY ts DESC LIMIT 20;"
```

**Monitoring**: healthchecks.io (`tradebot-heartbeat`, 5 min period) alerts Telegram + email
when the core stops pinging. Note the caveat in §3c — it can report green through a failure.

## 5. History worth knowing

Five defects were already found and fixed the hard way, each by the alerting working:
a false emergency halt on a $0 portfolio; a core-loop freeze from an untimed Coinbase call;
a heartbeat pinging a deleted check URL (silently 404ing as success); an Anthropic key that
needed a workspace-scoped type; GeckoTerminal rate limits. The safety rails have all been
exercised at least once — the drills (STOP/RESUME, FLATTEN) both passed.

The design principle throughout: **the model decides, code enforces.** No model output can
raise a limit, skip an approval, or size a position. Where the audit found that principle
violated (§3b C1, §3c gate bypass), those are the highest-value fixes.
