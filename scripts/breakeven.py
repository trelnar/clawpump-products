#!/usr/bin/env python3
"""What the strategy has to achieve for the account to make money.

The 5%/20%/10-position limits were chosen, not derived, and the phase ladder
was never checked against the cost of trading. This computes the hit rate a
given position size needs just to break even, so phase-2 sizing is a decision
with a number behind it.

Assumptions are arguments, not hidden constants. Run:
    python3 scripts/breakeven.py --capital 1000
"""
import argparse


def analyse(capital, pct, factor, roundtrip, api_day, trades_day, loss_frac, win_mult):
    size = capital * pct * factor
    cost_per_trade = size * roundtrip
    daily_cost = cost_per_trade * trades_day + api_day
    # A winner returns (win_mult - 1) x size; a loser returns -loss_frac x size.
    win_pnl = size * (win_mult - 1)
    lose_pnl = -size * loss_frac
    # hit * win_pnl + (1-hit) * lose_pnl = daily_cost / trades_day
    per_trade_hurdle = daily_cost / trades_day if trades_day else float("inf")
    denom = win_pnl - lose_pnl
    hit = (per_trade_hurdle - lose_pnl) / denom if denom else float("inf")
    return {"size": size, "cost_per_trade": cost_per_trade, "daily_cost": daily_cost,
            "api_share": api_day / daily_cost if daily_cost else 0,
            "required_hit_rate": hit}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--roundtrip", type=float, default=0.045,
                    help="buy+sell spread, impact and fees as a fraction (microcap 3-6%%)")
    ap.add_argument("--api-day", type=float, default=2.0, help="Claude API $/day")
    ap.add_argument("--trades-day", type=float, default=2.0)
    ap.add_argument("--loss-frac", type=float, default=0.5,
                    help="average loss on a loser, as a fraction of the position")
    ap.add_argument("--win-mult", type=float, default=2.0, help="average winner multiple")
    a = ap.parse_args()

    print(f"capital ${a.capital:,.0f} | round trip {a.roundtrip:.1%} | "
          f"API ${a.api_day:.2f}/day | {a.trades_day:g} trades/day")
    print(f"a winner returns {a.win_mult:g}x, a loser gives back {a.loss_frac:.0%}\n")
    print(f"{'phase':>5} {'size':>9} {'cost/trade':>11} {'cost/day':>9} "
          f"{'API share':>10} {'break-even hit rate':>20}")
    for ph, factor in ((2, 0.25), (3, 0.50), (4, 1.00)):
        r = analyse(a.capital, 0.05, factor, a.roundtrip, a.api_day,
                    a.trades_day, a.loss_frac, a.win_mult)
        hit = r["required_hit_rate"]
        flag = "  <-- impossible" if hit > 1 else ("  <-- demanding" if hit > 0.5 else "")
        print(f"{ph:>5} ${r['size']:>8.2f} ${r['cost_per_trade']:>10.2f} "
              f"${r['daily_cost']:>8.2f} {r['api_share']:>9.0%} {hit:>19.1%}{flag}")
    print("\nRead the API share column: where it dominates, the account is too small "
          "for the research cadence, and the fix is fewer cycles or more capital, "
          "not a better strategy.")


if __name__ == "__main__":
    main()
