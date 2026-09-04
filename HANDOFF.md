# HypeBot — Handoff

**Status as of 2026-09-02: LIVE in NORMAL, ~$880 of real money, zero positions, zero trades ever.**
A second audit (`AUDIT-FIRSTFILL.md`, 13 agents over the newly written code) found 10 more
defects on the trade path, 2 critical. All 10 are fixed in the repo. **STOP the bot and deploy
before it can trade.** Do the three manual round trips in §3b before trusting it unattended.

*Superseded status line:*
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

### 3b. Before the first trade

Everything from the first audit's C1-C3 is fixed and deployed. The second audit
(`AUDIT-FIRSTFILL.md`) found 10 more on the same path; all are fixed in the repo and
**not yet deployed**. Two mattered most:

- **Coinbase orders were polled by an id the API cannot query.** `list_orders` has no
  `client_order_id` filter — verified against the SDK's own source, coinbase-advanced-py
  1.8.4, `rest/orders.py:1446`. It fell into `**kwargs`, and the code read `orders[0]` of an
  unfiltered account-wide list without ever checking whose order it was. The second Coinbase
  order onward would have been booked against a previous order's fill, while the real order
  stayed resting and filled untracked. *Fixed:* the exchange `order_id` is taken from the
  placement response and polled with `get_order`; a rejected placement raises instead of
  entering the fill loop.
- **A confirmed Solana buy could book as "NOT BOUGHT."** The post-swap balance was read at
  the RPC's default `finalized` commitment, ~13s behind the `confirmed` the code waits for —
  so the read returned the *pre*-swap balance, `qty` came out 0, and the money-already-spent
  fill was reported to the owner as a failure with the tokens orphaned. *Fixed:* reads at
  `confirmed`, retries, and past the point of broadcast a fill is **never** reported as
  failed — it books from the quote and freezes instead.

The rest: partial exits deleting whole positions, profit-plan legs re-arming on a model plan
tweak (now keyed by price level, so a level fires once per position), `REVOKE` uppercasing
asset ids so it silently matched nothing, an `ADD` dropping its raised stop, a negative
model-supplied `sell_fraction` deleting a live position without selling it, and the
`GAS_EXITS_FLOOR` that was configured but never enforced.

**Then do this before trusting it unattended** — the one thing no code review can settle:
`execute_sell` has never sold anything, anywhere. Every safety mechanism in the system ends
in that function: the invalidation stop, the profit plan, the liquidity-drain exit, agent
`SELL_NOW`, and your own `FLATTEN`. Do one manual round trip per venue at the smallest size
— buy, check the books match the venue, sell, check the cash comes back — before letting the
agent file a ticket that reaches `execute_buy`. Gate 4's exit-safety is a *quote*, and quotes
are systematically optimistic for exactly the tokens this bot hunts.

### 3c. High — themed clusters (72 findings, details in AUDIT.md)

| Theme | The problem |
|---|---|
| ~~**Exits don't work**~~ | *Fixed.* `entry_liquidity_usd` is recorded at entry, reviving the liquidity-drain exit. The forecast schema gained `HOLD`/`ADD`/`SELL_NOW` (per the strategy skill's own action list) plus a `profit_plan` the fast path executes mechanically — one leg per tick, fractions of the remainder, each leg once. Held positions now reach the model with entry, mark, multiple, age, invalidation and standing plan, so reassessment is actually possible; `submit()` routes each action and `SELL_NOW` carries a partial fraction. Still open: no time stop, and none of it has run against a real position. |
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

- **Position reconciliation against venue holdings — the structural gap.** Every orphan bug
  found in both audits fails the same direction: real assets exist that the database does not
  know about. `reconcile_cash` only reconciles cash; nothing reads a token balance and asks
  "should this be a position?" That single mechanism would have turned five separate findings
  into a logged $5 anomaly. Highest-value remaining change.
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
`REPORT`, `SCORE [days]`, `GAPS [days]`, `WHY <asset>`, `YES <code>` / `NO <code>`.

- `SCORE` — what forecasts predicted vs what happened, split by the action taken.
  Empty until the first forecasts resolve (72h horizon).
- `GAPS` — why it is not trading: PASS reasons, and the evidence the model says it
  lacked. This is what separates discipline from blindness.

**Commissioning test — do this before trusting it unattended:**
```
cd /opt/tradebot && python3 -m scripts.roundtrip coinbase   # then solana, then base
```
One real buy and sell per venue at $5, on a liquid asset you can always exit. It prints
the books against what the venue actually holds at each step. `execute_sell` has never
sold anything anywhere; this is the only thing that resolves that.

**Sizing economics:** `python3 scripts/breakeven.py --capital 1000` — at $1,000 in phase 2,
API cost is 64% of the total cost of trading and the break-even hit rate is ~42%. That
number, not a percentage anyone picked, is the argument for more capital or fewer cycles.

**On the VPS** (`ssh -i ~/.ssh/tradebot_ed25519 root@107.191.39.195`):
```
systemctl status tradebot-core tradebot-agent
cd /opt/tradebot && git pull && systemctl restart tradebot-core tradebot-agent
bash /opt/tradebot/scripts/spend.sh
sqlite3 /var/lib/tradebot/tradebot.db "SELECT datetime(ts,'unixepoch'), kind, substr(detail,1,120) FROM events ORDER BY ts DESC LIMIT 20;"
```

**Tests**: `cd bot && python3 -m unittest discover -s tests` — no network, no credentials,
runs anywhere. 78 tests; 8 skip where the Coinbase SDK is absent, so run it on the VPS too.
`test_contracts.py` checks the code against the *installed SDK* rather than against mocks —
that is the class of check that would have caught the 2026-09-02 Coinbase critical, which
33 green mock-based tests missed. Add a case for every fix that touches the order path.

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
The exits cluster is closed too; what it now needs is a live position to prove it.
