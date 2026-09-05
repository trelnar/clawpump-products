# Signals — where the bot looks for hype

Discovery used to be two paid-promotion feeds plus a slice of Coinbase movers, so the
research layer only saw a token once someone had paid to show it. It now runs a **signal
layer** (`bot/tradebot/signals/`) that records *events* from many sources, and hands the
model, per asset, the shape of attention rather than raw counts:

| Feature | Meaning |
|---|---|
| `mentions_1h` / `mentions_6h` | weighted events in the last hour / last six |
| `accel` | last-hour rate ÷ the prior six hours' rate — the thing a 2x looks like from the front |
| `breadth` | distinct sources naming the asset — one source is a paid post, five is a move |
| `kinds` | which hard events fired: `launch`, `graduation`, `new_pool`, `holder_growth`, `call`… |
| `first_seen_min` | minutes since the asset first appeared anywhere |

Ranking is `accel × breadth^1.5 × (2 if a hard event fired)`, with one rule on top: an
asset only one source has named is capped below anything two independent sources agree
on, however loud that one source is. That is the property paid promotion cannot buy. The
top `SIGNAL_CANDIDATES` (20) go to research each cycle, on top of Coinbase movers. Paid promotion is **off**
(`PAID_PROMO_SOURCES=0`); set it to `1` to add those feeds back as one more source.

All message text is data. Nothing in this layer interprets it; only contract addresses
and `$TICKER`s are extracted and stored.

## Sources

| Source | What it catches | Needs | Default |
|---|---|---|---|
| `gecko` | GeckoTerminal trending + new pools on Solana and Base | nothing | on |
| `pumpfun` | pump.fun launches, king-of-the-hill, **graduations** (the canonical Solana memecoin event) | nothing | on |
| `clanker` | new token deploys on Base via Clanker | nothing | on |
| `reddit` | new posts in `REDDIT_SUBS` (CryptoMoonShots, solana, memecoins, SolanaMemeCoins, base), upvote-weighted | `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` (free; Reddit 403s datacenter IPs without them) | on, fails until keyed |
| `farcaster` | cast search for addresses/tickers, via Neynar | `NEYNAR_API_KEY` (free tier) | off until keyed |
| `birdeye` | holder-count growth on the top 5 rising Solana assets | `BIRDEYE_API_KEY` (free tier) | off until keyed |
| `telegram` | new messages in `TG_CHANNELS`, as `call` events — separate daemon | `TG_API_ID`, `TG_API_HASH`, `TG_CHANNELS`, one-time login | off until set |

Every source is isolated: one failing logs `signal_source_fail` and the rest run. Each
gets `SIGNAL_SOURCE_BUDGET_SEC` (45s) of wall clock per pass and stops between requests
once it is spent, so a hanging host costs one request, not the research cycle.
`SIGNAL_SOURCES` (comma list) chooses which are attempted at all. Base addresses are
lowercased everywhere an id is formed; one spelling per asset.

### Not built, and why

- **X / Twitter** — the API is $200/mo for read access at useful volume. Scraping breaks
  weekly and gets the account banned. The cheapest honest route is LunarCrush ($30/mo,
  aggregates X + Reddit + YouTube sentiment per asset); add it as one more source module
  when the budget says yes.
- **Discord** — there is no public search. Monitoring means a user token in specific
  servers, which violates Discord's ToS and gets the account banned. Telegram carries the
  same calls earlier, legally.
- **TikTok** — no API for search; the signal there lags Telegram/X by hours to days anyway.

## Setup on the VPS

All keys go into `/etc/tradebot/agent.env` (the research layer's file — none of these
touch trading credentials). Edit with `nano` so nothing prints to the screen:

```
sudo nano /etc/tradebot/agent.env
```

Add whichever lines you have:

```
REDDIT_CLIENT_ID=...      # https://www.reddit.com/prefs/apps -> create app -> type "script"
REDDIT_CLIENT_SECRET=...  #   redirect uri can be http://localhost; id is under the app name
NEYNAR_API_KEY=...        # https://neynar.com  (free)
BIRDEYE_API_KEY=...       # https://bds.birdeye.so  (free "Standard" tier)
TG_API_ID=...             # https://my.telegram.org -> API development tools
TG_API_HASH=...
TG_CHANNELS=channel1,channel2,...   # public @usernames, without the @
```

Then `systemctl restart tradebot-agent`.

### Telegram monitor (one-time)

Telegram bots cannot read channels they don't admin, so this uses a **user** session.
Use a throwaway Telegram account, not the one that owns the bot: the session file is a
full login. The login prompts for that account's phone number, the code Telegram sends,
and its 2FA password if set — they are typed into your terminal and never echoed, but
don't screenshot that step.

```
sudo -u agent /opt/tradebot/venv/bin/python /opt/tradebot/scripts/tg_login.py
sudo systemctl enable --now tradebot-tgmon
sudo systemctl status tradebot-tgmon
```

The service is `User=agent` with the trading secrets marked inaccessible. If it is
started before the login, it logs `tgmon_not_logged_in` once and exits with code 3,
which the unit does not restart on; run the login, then `systemctl start` it again. Good channels
to start with are the large public call channels for Solana and Base memecoins; breadth
across several independent ones is the signal, so prefer 5–10 channels over one.

## Checking it

```
/opt/tradebot/venv/bin/python /opt/tradebot/scripts/signals_probe.py
```

Runs each enabled source once and prints new-event counts and the current rising list.
**Run this after deploying** — the sources were written against documented response
shapes without live access; a source that returns zero on a busy day means the parser
no longer matches and needs its fixture updated.

In Telegram: `SIGNALS` shows per-source health (last run, failures) and the top rising
assets; `SIGNALS <asset>` shows one asset's features. Journal events: `signal_collect`,
`signal_source_fail`, `tgmon_start`, `tgmon_listening`, `tgmon_channel_fail`.

Events are kept `SIGNAL_RETENTION_DAYS` (3) and deduped on
`(source, asset, kind, ref)`, so re-reading a feed never double-counts.
