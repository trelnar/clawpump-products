# HypeBot — Handoff

**Status as of 2026-09-01: LIVE, holding ~$895 of real money. Not yet redeployed.**
An audit found 3 critical defects that fire on the first trade. **C1-C3 are fixed in the
repo but the VPS is still running the old code** — `git pull && systemctl restart` on the
VPS is the next physical action. Until then, keep it on `STOP`.
Full inventory of all 106 findings: `AUDIT.md`.

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
  *C1 defeated these; fixed in the repo, not yet on the VPS.*
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

### 3b. Critical — FIXED in the repo, not yet deployed

Fixed on branch `claude/trading-bot-skills-sfqmfo`, covered by `bot/tests/test_fills.py`
(20 tests, `cd bot && python3 -m unittest discover -s tests`). **Deploy before trading.**

- **C1. Solana positions were booked in raw token units.** `execution.py` stored Jupiter's
  raw `outAmount`; marks are USD per whole token, so a $5 fill reported ~$5M of portfolio
  value and silently slackened every percentage limit.
  *Fixed:* quantity now comes from the token-balance delta divided by the mint's
  authoritative decimals (`solana_dex.token_decimals`). Base does the same via the ERC-20
  `decimals()` call. A `_sanity_qty` check requires `qty * price` to land within 10x of the
  dollars actually spent; when it doesn't, the fill is still booked (an invisible position is
  worse) and the bot drops to `RECON_FREEZE` with an ops alert rather than trading off a
  portfolio value it distrusts.
- **C2. Buys were booked as filled with no confirmation** on all three venues. A rejected
  Coinbase limit order or an unlanded swap became a phantom position.
  *Fixed:* Coinbase orders are polled to a terminal state and booked from `filled_size` /
  `average_filled_price` / `total_fees` — partial fills book what filled and the remainder is
  cancelled; zero fill books nothing. Base swaps wait for a receipt (`confirm()` returning
  `unknown` no longer counts as success). Solana waits for the signature. Sells got the same
  treatment: proceeds are a measured USDC balance delta, and an unconfirmed exit leaves the
  position on the books with a `SELL FAILED` alert instead of silently closing it.
- **C3. The Telegram listener was an unsupervised daemon thread** with unguarded
  `cq['from']['id']` access. One unusual update killed it permanently; trading continued with
  the kill switch dead and the heartbeat still green.
  *Fixed:* every update dispatches inside its own guard, `sender` is read defensively, the
  offset persists to `kv` so a crash doesn't redeliver forever, and `Poller.healthy()` exposes
  liveness. `core.supervise_telegram` checks it every 30s, respawns a dead thread, and drops to
  `SELL_ONLY` after 5 minutes without a successful poll — exits and monitoring keep running —
  restoring `NORMAL` only for a halt it set itself.

### 3c. High — themed clusters (72 findings, details in AUDIT.md)

| Theme | The problem |
|---|---|
| **Exits don't work** | No take-profit path; the forecast schema has no SELL/HOLD/ADD action; `entry_liquidity_usd` is never written so the liquidity-drain exit is dead. Only a model-supplied invalidation price can ever close a position. |
| ~~**Gate bypass**~~ | *Fixed.* An approved buy ran `execute_buy` directly, skipping gates 1-4 — so a tapped `YES` executed even under `USER_STOP`. Approvals now go through `execution.execute_approved`, which re-runs halt/staleness/risk/exit-safety at the moment of the tap, and `YES` on a buy is refused outright when mode isn't `NORMAL` (without consuming the code — `RESUME`, then the same `YES` works). |
| **Fill integrity** | *Mostly closed by C1/C2:* fills, proceeds, and Coinbase fees now come from the venue. Still open: fees are folded into cost basis but not recorded per-fill in the journal. |
| **Concurrency** | Approved buys execute on the Telegram poller thread, blocking STOP/FLATTEN for 90+ seconds. Two threads place orders with no mutex. |
| **Recovery** | No backups of journal or wallet keys. Cold-start recovery unimplemented. `RESUME` now also clears `RECON_FREEZE` (C1's sanity guard can land there), but `SELL_ONLY` still has no user-facing exit besides the Telegram watchdog's own recovery. |
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

**Tests**: `cd bot && python3 -m unittest discover -s tests` — no network, no credentials,
runs anywhere. Add a case here for every fix that touches the order path.

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
violated, those were the highest-value fixes: §3b C1-C3 and the §3c gate bypass are closed.
The exits cluster is next — the bot can now buy correctly and still barely sell.
