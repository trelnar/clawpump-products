# Agent Audit #001 — ClawPump Copy-Trade Leaderboard (Phoenix Perps)

**Audit conducted:** July 3, 2026 · **Published:** July 22, 2026
**Result: 11 traders audited · 0 qualified · $0 allocated**

---

## Why this exists

Copy-trading leaderboards sell rankings, not risk. Before allocating any capital to ClawPump's copy-trade feature, every ranked trader was audited against pre-committed criteria using live on-chain data. The leaderboard was treated as name-discovery only — never as evidence.

This is a timestamped record of that audit. The criteria were written **before** looking at any trader. No verdict was negotiated after the fact.

## Pre-committed criteria

A trader had to clear **all** of the following to qualify:

1. **Wallet age 60+ days** with an adequate trade sample size
2. **Real per-position leverage** consistent with sane risk (not the blended figure the board displays)
3. **No adding to losing positions** (martingale behavior = instant veto)
4. **Open P&L vs. realized P&L** — no large trapped losses propping up a "win rate"
5. **Account actually active** — live balance consistent with the ranking
6. **Mechanically copyable** — trade cadence compatible with a ~90-second copy-execution delay

## Findings (as of July 3, 2026)

| Rank | Trader | Board claimed | On-chain reality | Verdict |
|---|---|---|---|---|
| #1 | Top Phoenix Trader (EibQ…89Yj) | $100.2K 30-day P&L · 65% win rate | Gave back ~70% of peak P&L since Jun 23 · 51% realized win rate · 10 concurrent shorts, −$59.5K open · actively adding to a 15x losing SOL short (open P&L moved −$59K → −$74K within 2 hours during the audit) | **VETO — martingale** |
| #2 | Phoenix Sniper (BCrF…pxn7) | $69.3K 30-day P&L · 78% win rate | Wallet 24 days old · ~12x average leverage · −$66.5K trapped in open positions · one 15x SOL short held 19 days at −18% | **VETO — leverage + loss-holding** |
| #3 | Phoenix Whale | Ranked #3 | $4.97M book, healthy (−0.5%) — but ~360 trades/day HFT style, uncopyable at a 90-second copy cadence | **VETO — mechanism** |
| #5 | Diamond Hands | Ranked #5 | $109K live, one long at +$26.7K open — but only ~33 retrievable trades; fails sample-size rule | **VETO — sample size** |
| #8 | Patient Bear | Ranked #8 | $98.6K, single small SOL short, no retrievable trade history | **VETO — insufficient history** |
| #4, #6, #7, #9, #10, #11 | (six accounts) | Ranked top-11 | Live balances between $0.01 and $45.90 — accounts emptied | **VETO — abandoned** |

## Systemic findings

- **The leaderboard was frozen.** Rankings reflected a June 10 data sync. Six of eleven ranked accounts had been effectively emptied by audit time — the board was ranking ghosts.
- **The top two were in the same trade.** Board #1 and #2 were both trapped in leveraged SOL shorts during the same squeeze, deteriorating in real time as the audit ran.
- **Advertised win rates diverged from realized win rates** once trapped open losses were separated from closed P&L.
- **The platform's own trade-history API returned zero rows** on a wallet with 15,000+ fills — behavioral auditing required manual on-chain review of live position data.

## Method

Leaderboard rankings were used only to generate the candidate list. Each wallet was then reviewed live via on-chain perp position data (perp.so) and platform account-state tooling. Every verdict is a mechanical PASS/VETO against the criteria above — no price predictions, no discretion, no "vibes."

## Disclaimer

These are point-in-time observations of public, pseudonymous on-chain data as of July 3, 2026. Positions, balances, and behavior change. Nothing here is financial advice, a recommendation, or an accusation of wrongdoing — verdicts describe fit against copy-trading criteria only.

---

*Audit series: evidence-based reviews of trading agents and automated strategies. Criteria pre-committed, receipts included, verdicts mechanical.*
