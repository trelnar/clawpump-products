"""backtest.ablation -- grid runner, metrics, statistical guards, markdown report.

Owns the measurement layer of the BTC 4h harness. Reads ONLY the frozen trade
record (strategy.TRADE_COLUMNS); never reimplements indicator or strategy math.

The central question (SPEC §1): the VIDYA ribbon flips AFTER a turn, so the
ribbon+delta gates structurally reject turn signals and admit only
pullback-in-trend signals. Does that filter add expectancy or destroy it?
This module's job is to answer that or -- the more likely and entirely
acceptable outcome -- to prove the sample cannot answer it.

DESIGN RULE: this module REFUSES to print a confident number on a small sample.
See SPEC §6. The refusal wording in _render_not_reported / _render_inconclusive
and the three verdict blocks is reproduced verbatim from SPEC and must not be
paraphrased.

OPEN QUESTIONS (SPEC §7.3 -- ambiguities recorded, literal wording implemented):

 Q1  compute_metrics' key set is frozen by SPEC §3.6 ("EXACTLY these keys"), and
     run_grid's columns are frozen to that key set. My brief also asked for
     trades/month, a buy-and-hold benchmark, and a walk-forward split. Adding
     them to compute_metrics would silently change run_grid's schema and break
     the integrator. They are therefore separate public helpers --
     trades_per_month(), buy_and_hold_benchmark(), walk_forward_split(),
     run_walk_forward() -- and are rendered into the report from those.
     SPEC wins on the frozen schema.

 Q2  profit_factor: SPEC says "sum(wins)/abs(sum(losses))" without naming the
     column. Implemented on return_pct (a P&L measure); expectancy stays in R.
     A reader wanting PF in R-space should recompute. See VERIFY note there.

 Q3  Dead swept parameters are written as NaN in the results frame (e.g.
     delta_threshold on an arm with use_delta=False). NaN is the honest encoding
     of "this parameter did not exist in this cell" and makes
     `results.delta_threshold == 20.0` correctly exclude those rows. The
     StrategyParams object still carries the arm's own default. SPEC does not
     specify; if the integrator expects a number, change _dead() only.

 Q4  SPEC §3.6 asks for the full-grid table "sorted by expectancy_r descending"
     but §6.5 forbids sorting a not_reported cell to the top. A pure expectancy
     sort puts pure noise in row 1. Implemented as (sufficiency_rank asc,
     expectancy_r desc) and the sort key is stated in the table caption.
     Documented deviation, taken deliberately.

 Q5  SPEC §6.3 enumerates three verdicts but does not cover "reportable cells
     exist, yet the A0 baseline itself is not reportable" -- (b) and (c) both
     quote baseline numbers that would not exist. A fourth block, (d), is
     emitted there rather than printing a false statement. Flagged as a SPEC gap.

 Q6  The central-question table (report §3) uses each arm's CANONICAL cell
     (delta 20.0 / quadrant 0.5 / offset 1 -- the user's live rules), NOT its
     best-expectancy cell. Selecting the best cell per arm for a head-to-head is
     precisely the multiple-comparison error this harness exists to avoid.

 Q7  simulate() reports discarded entries via trades.attrs["discarded_entries"].
     run_grid's column set is frozen and cannot carry it, so the per-cell totals
     are logged at WARNING when non-zero rather than tabulated.

NOT VERIFIED: no number produced here means anything until the --verify CSV is
diffed against the user's TradingView chart (SPEC U-0). Every report carries
that disclaimer as its first block.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import sys
import zlib
from typing import Any, Iterator, Sequence

import numpy as np
import pandas as pd

from . import data as _data
from . import strategy as _strategy
from . import two_pole as _two_pole
from . import vidya as _vidya
from .strategy import TRADE_COLUMNS, ExitModel, StrategyParams

__all__ = [
    "MIN_TRADES_REPORTABLE",
    "MIN_TRADES_DIRECTIONAL",
    "WARMUP_BARS",
    "DEFAULT_SWEEPS",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "RANDOM_BASELINE_ARM_ID",
    "RANDOM_BASELINE_SEED",
    "BASELINE_ARM_ID",
    "METRIC_KEYS",
    "RESULT_COLUMNS",
    "Arm",
    "classify_sufficiency",
    "bootstrap_ci",
    "compute_metrics",
    "build_arms",
    "build_exit_grid",
    "run_grid",
    "render_report",
    "trades_per_month",
    "buy_and_hold_benchmark",
    "expected_false_positives",
    "sidak_alpha",
    "walk_forward_split",
    "run_walk_forward",
    "walk_forward_summary",
    "render_walk_forward_section",
]

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants (SPEC §3.6, §3.7, §6)
# --------------------------------------------------------------------------

MIN_TRADES_REPORTABLE: int = 30   # below this, no conclusion may be drawn
MIN_TRADES_DIRECTIONAL: int = 10  # below this, no number is printed at all

# ATR(200) + VIDYA(34) + SMA(25)/two-pole(20) chain: indicators are not
# trustworthy for roughly the first 400 bars (SPEC §3.7). Imported by verify.py.
WARMUP_BARS: int = 400

BOOTSTRAP_RESAMPLES: int = 10_000
BOOTSTRAP_SEED: int = 12345

BASELINE_ARM_ID: str = "A0"
RANDOM_BASELINE_ARM_ID: str = "A7"
RANDOM_BASELINE_SEED: int = 777

# SPEC §5.2. delta_threshold is swept, never a constant: the +/-20% gate was
# chosen against a PER-BAR delta reading while the harness computes the correct
# CUMULATIVE-PER-LEG reading, on which observed values run +27..+179% (SPEC U-1).
DEFAULT_SWEEPS: dict[str, list] = {
    "delta_threshold":     [0.0, 10.0, 20.0, 30.0, 50.0, 80.0],   # U-1
    "quadrant_threshold":  [0.0, 0.25, 0.5, 0.75, 1.0],
    "entry_offset_bars":   [1, 2],                                # U-4
    "max_signal_age_bars": [3],       # widen to [1,2,3,5] only on demand
    "zero_cross_kills":    [True],
}

_SWEEP_KEYS: tuple[str, ...] = (
    "delta_threshold",
    "quadrant_threshold",
    "entry_offset_bars",
    "max_signal_age_bars",
    "zero_cross_kills",
)

# The user's live rules, used for the head-to-head table (see OPEN QUESTION Q6).
CANONICAL_CELL: dict[str, Any] = {
    "delta_threshold": 20.0,
    "quadrant_threshold": 0.5,
    "entry_offset_bars": 1,
    "max_signal_age_bars": 3,
    "zero_cross_kills": True,
}

METRIC_KEYS: tuple[str, ...] = (
    "n_trades", "n_long", "n_short", "n_end_of_data",
    "win_rate", "avg_return_pct", "median_return_pct", "total_return_pct",
    "expectancy_r", "expectancy_r_ci_lo", "expectancy_r_ci_hi",
    "profit_factor", "max_drawdown_pct", "avg_bars_held",
    "avg_mae_pct", "avg_mfe_pct", "pct_stopped", "pct_timed_out",
    "sufficiency",
)

_METRIC_DTYPES: dict[str, str] = {
    "n_trades": "int64", "n_long": "int64", "n_short": "int64",
    "n_end_of_data": "int64", "sufficiency": "object",
}

_CELL_COLUMNS: dict[str, str] = {
    "arm_id": "object", "arm_label": "object", "exit_id": "object",
    "delta_threshold": "float64", "quadrant_threshold": "float64",
    "max_signal_age_bars": "int64", "entry_offset_bars": "int64",
    "zero_cross_kills": "bool",
}

RESULT_COLUMNS: tuple[str, ...] = tuple(_CELL_COLUMNS) + METRIC_KEYS

_SUFFICIENCY_RANK: dict[str, int] = {
    "reportable": 0, "inconclusive": 1, "not_reported": 2,
}

_BARS_PER_DAY_4H: float = 6.0
_DAYS_PER_MONTH: float = 30.4375


# --------------------------------------------------------------------------
# Arms (SPEC §3.6, §5.1)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Arm:
    """One ablation arm: an identifier, a human label, and its StrategyParams.

    arm_id is the stable key used in the results frame and the report; label is
    the §5.1 name. The DOT is always required in every arm -- the arms vary only
    the gates around it.
    """

    arm_id: str
    label: str
    params: StrategyParams


def build_arms() -> list[Arm]:
    """Return the eight ablation arms of SPEC §5.1, in the order listed there.

    A0 all_gates        the user's live rules; the baseline under test
    A1 quadrant_only    the arm that would have taken the Aug 13 long
    A2 quadrant_ribbon  isolates the ribbon's contribution
    A3 quadrant_delta   isolates delta's contribution
    A4 dot_only         every zero-line-gated dot; the rawest signal
    A5 inverted_ribbon  ribbon must OPPOSE the trade -- the direct test of the
                        central question. If A5 beats A2, the gates are
                        rejecting the good trades.
    A6 ribbon_delta_only trend-following filter, no zone requirement
    A7 random_baseline  the null hypothesis. Any arm that does not beat A7 has
                        demonstrated nothing.
    """
    base = StrategyParams()
    return [
        Arm("A0", "all_gates", dataclasses.replace(
            base, use_quadrant=True, use_ribbon=True, use_delta=True)),
        Arm("A1", "quadrant_only", dataclasses.replace(
            base, use_quadrant=True, use_ribbon=False, use_delta=False)),
        Arm("A2", "quadrant_ribbon", dataclasses.replace(
            base, use_quadrant=True, use_ribbon=True, use_delta=False)),
        Arm("A3", "quadrant_delta", dataclasses.replace(
            base, use_quadrant=True, use_ribbon=False, use_delta=True)),
        Arm("A4", "dot_only", dataclasses.replace(
            base, use_quadrant=False, use_ribbon=False, use_delta=False)),
        Arm("A5", "inverted_ribbon", dataclasses.replace(
            base, use_quadrant=True, use_ribbon=True, invert_ribbon=True,
            use_delta=False)),
        Arm("A6", "ribbon_delta_only", dataclasses.replace(
            base, use_quadrant=False, use_ribbon=True, use_delta=True)),
        # A7 carries A0's gate configuration so its sweep cells line up 1:1 with
        # A0's; its entries are replaced wholesale by the random generator.
        Arm("A7", "random_baseline", dataclasses.replace(
            base, use_quadrant=True, use_ribbon=True, use_delta=True)),
    ]


def build_exit_grid() -> list[ExitModel]:
    """Return the fifteen exit models of SPEC §5.3, in the order listed there.

    stop_param is a FRACTION for fixed_pct (0.016 = 1.6%) and an ATR MULTIPLE
    for the atr_* kinds. atr_length=14 throughout -- independent of VIDYA's
    ATR(200).

    The `beyond*` family is the hypothesis under test: the one live trade was
    stopped at 76,233 (the exact low of a sweep) and price then ran through its
    take-profit to 82,272. If stops beyond the signal-bar extreme do not
    outperform fixed_pct, that hypothesis is dead and must be reported as dead.

    `live_bracket` reproduces the one real trade (entry 77,474 / SL 76,235 /
    TP 79,850, R:R 1.89) so the harness can be checked against a known outcome.
    """
    return [
        ExitModel("live_bracket", "fixed_pct", 0.016, "fixed_pct", 0.031, 42, False),
        ExitModel("pct1.6_R2", "fixed_pct", 0.016, "r_multiple", 2.0, 42, False),
        ExitModel("pct3.0_R2", "fixed_pct", 0.030, "r_multiple", 2.0, 42, False),
        ExitModel("atr1.5_R2", "atr_from_entry", 1.5, "r_multiple", 2.0, 42, False),
        ExitModel("atr2.5_R2", "atr_from_entry", 2.5, "r_multiple", 2.0, 42, False),
        ExitModel("beyond0.25_R2", "atr_beyond_extreme", 0.25, "r_multiple", 2.0, 42, False),
        ExitModel("beyond0.5_R1", "atr_beyond_extreme", 0.50, "r_multiple", 1.0, 42, False),
        ExitModel("beyond0.5_R2", "atr_beyond_extreme", 0.50, "r_multiple", 2.0, 42, False),
        ExitModel("beyond0.5_R3", "atr_beyond_extreme", 0.50, "r_multiple", 3.0, 42, False),
        ExitModel("beyond1.0_R2", "atr_beyond_extreme", 1.00, "r_multiple", 2.0, 42, False),
        ExitModel("beyond0.5_trail3", "atr_beyond_extreme", 0.50, "atr_trail", 3.0, 42, False),
        ExitModel("beyond0.5_timeonly", "atr_beyond_extreme", 0.50, "none", 0.0, 42, False),
        ExitModel("beyond0.5_flip", "atr_beyond_extreme", 0.50, "none", 0.0, 42, True),
        ExitModel("beyond0.5_R2_t21", "atr_beyond_extreme", 0.50, "r_multiple", 2.0, 21, False),
        ExitModel("beyond0.5_R2_t84", "atr_beyond_extreme", 0.50, "r_multiple", 2.0, 84, False),
    ]


# --------------------------------------------------------------------------
# Metrics (SPEC §3.6)
# --------------------------------------------------------------------------

def classify_sufficiency(n_trades: int) -> str:
    """Map a trade count to a sufficiency verdict. Derived ONLY from n_trades.

    n >= 30      -> "reportable"
    10 <= n < 30 -> "inconclusive"
    n < 10       -> "not_reported"
    """
    if n_trades >= MIN_TRADES_REPORTABLE:
        return "reportable"
    if n_trades >= MIN_TRADES_DIRECTIONAL:
        return "inconclusive"
    return "not_reported"


def bootstrap_ci(
    values: Sequence[float] | np.ndarray | pd.Series,
    *,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap CI for the MEAN of `values`.

    Returns (lo, hi) as a 2-tuple of float; (nan, nan) when fewer than two
    finite values are present. Non-finite values are dropped before resampling.

    Deterministic: numpy.random.default_rng(seed), 10_000 resamples by default
    (SPEC §3.6). `alpha` is exposed so a caller holding the trades frame can
    request a multiple-comparison-corrected level -- see sidak_alpha(). The
    results frame always stores the uncorrected 95% interval.
    """
    v = np.asarray(values, dtype="float64").ravel()
    v = v[np.isfinite(v)]
    if v.size < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    # Resample indices, not values, so the estimator stays a plain mean.
    idx = rng.integers(0, v.size, size=(n_resamples, v.size))
    means = v[idx].mean(axis=1)
    lo = float(np.percentile(means, 100.0 * (alpha / 2.0)))
    hi = float(np.percentile(means, 100.0 * (1.0 - alpha / 2.0)))
    return (lo, hi)


def _nanmean(s: pd.Series) -> float:
    """Mean over finite values only; nan when there are none."""
    v = pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64")
    v = v[np.isfinite(v)]
    return float(v.mean()) if v.size else float("nan")


def compute_metrics(trades: pd.DataFrame) -> dict:
    """Summarise a trades frame. Input may have zero rows.

    Returns a dict with EXACTLY the keys of METRIC_KEYS, never None; undefined
    values are float("nan"). Key set and types are frozen by SPEC §3.6 --
    run_grid's column schema is derived from them, so do not add keys here
    (see OPEN QUESTION Q1).

      n_trades int, n_long int, n_short int, n_end_of_data int,
      win_rate float (fraction in [0,1]), avg_return_pct float,
      median_return_pct float, total_return_pct float (SUM, never compounded),
      expectancy_r float (mean r_multiple -- THE headline metric),
      expectancy_r_ci_lo/hi float (95% bootstrap, 10_000 resamples, seed 12345),
      profit_factor float (inf if no losses), max_drawdown_pct float (positive),
      avg_bars_held float, avg_mae_pct float, avg_mfe_pct float,
      pct_stopped float, pct_timed_out float, sufficiency str.

    The CI is computed whenever n >= 2; RENDERING must respect sufficiency
    (SPEC §6) -- a number existing in this dict is not permission to print it.
    """
    missing = [c for c in TRADE_COLUMNS if c not in trades.columns]
    if missing:
        raise ValueError(
            f"trades frame is missing required TRADE_COLUMNS: {missing}"
        )

    n = int(len(trades))
    out: dict[str, Any] = {k: float("nan") for k in METRIC_KEYS}
    out["n_trades"] = n
    out["n_long"] = int((trades["side"] == "long").sum())
    out["n_short"] = int((trades["side"] == "short").sum())
    out["n_end_of_data"] = int((trades["exit_reason"] == "end_of_data").sum())
    out["sufficiency"] = classify_sufficiency(n)

    if n == 0:
        # An empty cell is a finding, not an error: it is how "the gates reject
        # everything" shows up in the grid (SPEC §6.5).
        return out

    ret = pd.to_numeric(trades["return_pct"], errors="coerce")
    r = pd.to_numeric(trades["r_multiple"], errors="coerce")

    out["win_rate"] = float((ret > 0).sum()) / n
    out["avg_return_pct"] = _nanmean(ret)
    out["median_return_pct"] = float(ret.median(skipna=True))
    # SUM, not a compounded curve: the harness has no position-sizing model and
    # compounding one manufactures a curve (SPEC §6.5).
    out["total_return_pct"] = float(ret.sum(skipna=True))
    out["expectancy_r"] = _nanmean(r)

    if n >= 2:
        lo, hi = bootstrap_ci(r)
        out["expectancy_r_ci_lo"] = lo
        out["expectancy_r_ci_hi"] = hi

    # VERIFY: profit factor is computed on return_pct (P&L), not r_multiple.
    # SPEC §3.6 says "sum(wins)/abs(sum(losses))" without naming the column.
    # Check this is the convention the user expects before quoting it.
    wins = float(ret[ret > 0].sum())
    losses = float(ret[ret < 0].sum())
    if losses == 0.0:
        out["profit_factor"] = float("inf") if wins > 0 else float("nan")
    else:
        out["profit_factor"] = wins / abs(losses)

    # Max drawdown on the CUMULATIVE SUM of return_pct, reported positive. The
    # leading 0.0 makes an immediate first-trade loss count as drawdown.
    eq = np.concatenate([[0.0], ret.fillna(0.0).to_numpy(dtype="float64").cumsum()])
    out["max_drawdown_pct"] = float(np.maximum.accumulate(eq).__sub__(eq).max())

    out["avg_bars_held"] = _nanmean(trades["bars_held"])
    out["avg_mae_pct"] = _nanmean(trades["mae_pct"])
    out["avg_mfe_pct"] = _nanmean(trades["mfe_pct"])
    out["pct_stopped"] = float((trades["exit_reason"] == "stop").sum()) / n
    out["pct_timed_out"] = float((trades["exit_reason"] == "time").sum()) / n
    return out


# --------------------------------------------------------------------------
# Supplementary metrics (not part of the frozen compute_metrics schema -- Q1)
# --------------------------------------------------------------------------

def trades_per_month(n_trades: int, bars: pd.DataFrame, *, warmup_bars: int | None = None) -> float:
    """Trade frequency over the TRADEABLE span (post-warmup), per 30.4375 days.

    Returns nan when the tradeable span is empty. Frequency matters because a
    cell can only reach MIN_TRADES_REPORTABLE if the signal fires often enough;
    a low rate tells the user that more history -- not a different parameter --
    is the fix.
    """
    warm = WARMUP_BARS if warmup_bars is None else warmup_bars
    usable = bars.iloc[warm:]
    if len(usable) < 2:
        return float("nan")
    days = (usable.index[-1] - usable.index[0]).total_seconds() / 86400.0
    if days <= 0:
        return float("nan")
    return float(n_trades) / (days / _DAYS_PER_MONTH)


def buy_and_hold_benchmark(bars: pd.DataFrame, *, warmup_bars: int | None = None) -> dict:
    """Buy-and-hold over the tradeable (post-warmup) span.

    Returns a dict: start_ts, end_ts, n_bars, days, start_price, end_price,
    return_pct, max_drawdown_pct, ann_return_pct. All floats are nan when the
    span is too short.

    COMPARABILITY WARNING, restated in the report: this is a COMPOUNDED hold
    return, while every strategy cell reports a SUM of per-trade percentages
    (SPEC §6.5 forbids compounding a backtest with no sizing model). The two
    are not on the same scale. B&H is here to answer "did this signal beat
    doing nothing at all", which is a direction question, not a magnitude one.
    """
    warm = WARMUP_BARS if warmup_bars is None else warmup_bars
    usable = bars.iloc[warm:]
    nan = float("nan")
    if len(usable) < 2:
        return {
            "start_ts": None, "end_ts": None, "n_bars": int(len(usable)),
            "days": nan, "start_price": nan, "end_price": nan,
            "return_pct": nan, "max_drawdown_pct": nan, "ann_return_pct": nan,
        }
    close = usable["close"].to_numpy(dtype="float64")
    p0, p1 = float(close[0]), float(close[-1])
    days = (usable.index[-1] - usable.index[0]).total_seconds() / 86400.0
    ret = (p1 / p0 - 1.0) * 100.0
    peak = np.maximum.accumulate(close)
    dd = float(((peak - close) / peak).max() * 100.0)
    ann = ((p1 / p0) ** (365.25 / days) - 1.0) * 100.0 if days > 0 else nan
    return {
        "start_ts": usable.index[0], "end_ts": usable.index[-1],
        "n_bars": int(len(usable)), "days": days,
        "start_price": p0, "end_price": p1,
        "return_pct": ret, "max_drawdown_pct": dd, "ann_return_pct": float(ann),
    }


def sidak_alpha(k: int, alpha: float = 0.05) -> float:
    """Sidak-corrected per-comparison alpha for k independent comparisons.

    1 - (1 - alpha)**(1/k). Returned for callers who hold the trades frame and
    can re-run bootstrap_ci at the corrected level; the results frame always
    stores the uncorrected 95% interval (see OPEN QUESTION Q1/Q3).
    """
    if k <= 1:
        return float(alpha)
    return float(1.0 - (1.0 - alpha) ** (1.0 / k))


def expected_false_positives(k: int, alpha: float = 0.05) -> float:
    """Expected count of spurious 'separations' when scanning k cells at alpha.

    Simply k*alpha. Printed beside any claimed winner so the
    multiple-comparisons problem is visible on the same line as the claim
    (SPEC §6.5).
    """
    return float(max(k, 0)) * float(alpha)


# --------------------------------------------------------------------------
# Grid construction
# --------------------------------------------------------------------------

def _live_sweep_keys(params: StrategyParams) -> tuple[str, ...]:
    """Which swept keys actually change behaviour for these params.

    A sweep key is applied to an arm only where it is live: delta_threshold does
    nothing when use_delta=False, quadrant_threshold does nothing when
    use_quadrant=False. Cells differing only in a dead parameter are collapsed
    so a disabled gate cannot inflate the grid or the multiple-comparison count
    (SPEC §5.2).
    """
    live = []
    for key in _SWEEP_KEYS:
        if key == "delta_threshold" and not params.use_delta:
            continue
        if key == "quadrant_threshold" and not params.use_quadrant:
            continue
        live.append(key)
    return tuple(live)


def _iter_cells(arm: Arm, sweeps: dict[str, list]) -> Iterator[dict[str, Any]]:
    """Yield deduplicated sweep combinations for one arm, in a stable order.

    Dead parameters are pinned to the arm's own default so the emitted
    StrategyParams is well-formed; the results row records them as NaN (Q3).
    """
    live = _live_sweep_keys(arm.params)
    axes = [sweeps.get(k, DEFAULT_SWEEPS[k]) for k in live]
    seen: set[tuple] = set()
    for combo in itertools.product(*axes) if axes else [()]:
        cell = {k: v for k, v in zip(live, combo)}
        key = tuple(cell.get(k, None) for k in _SWEEP_KEYS)
        if key in seen:
            continue
        seen.add(key)
        full = {k: getattr(arm.params, k) for k in _SWEEP_KEYS}
        full.update(cell)
        full["_live"] = live
        yield full


def _dead(value: Any, key: str, live: tuple[str, ...]) -> Any:
    """Report-value for a swept parameter: NaN when the parameter is dead (Q3)."""
    return value if key in live else float("nan")


def _cell_token(arm: Arm, cell: dict[str, Any]) -> str:
    """Stable string identifying a sweep cell, for deterministic RNG seeding."""
    parts = [arm.arm_id] + [f"{k}={cell[k]!r}" for k in _SWEEP_KEYS]
    return "|".join(parts)


def _random_entries(
    bars: pd.DataFrame,
    tp: pd.DataFrame,
    vd: pd.DataFrame,
    *,
    n_entries: int,
    n_long: int,
    entry_offset_bars: int,
    warmup_bars: int,
    token: str,
) -> list[dict]:
    """Build the A7 null-hypothesis entries: random bars, matched to A0.

    Matched in COUNT and LONG/SHORT RATIO to the A0 cell with the same sweep
    parameters, and simulated through the same exit model, so the only
    difference from A0 is that the entry timing carries no information.

    Any arm that does not beat A7 has demonstrated nothing (SPEC §5.1).

    Deterministic: seeded from (RANDOM_BASELINE_SEED, crc32(token)) so the
    result depends on the cell identity, not on iteration order.
    """
    if n_entries <= 0:
        return []
    lo = int(warmup_bars)
    hi = len(bars) - 1 - int(entry_offset_bars)
    if hi <= lo:
        return []
    pool = np.arange(lo, hi + 1, dtype="int64")
    take = int(min(n_entries, pool.size))
    rng = np.random.default_rng([RANDOM_BASELINE_SEED, zlib.crc32(token.encode())])
    picks = np.sort(rng.choice(pool, size=take, replace=False))

    sides = np.array(["long"] * min(n_long, take) + ["short"] * (take - min(n_long, take)))
    rng.shuffle(sides)

    osc = tp["osc"].to_numpy(dtype="float64")
    delta = vd["delta_pct"].to_numpy(dtype="float64")
    trend = vd["trend"].to_numpy()
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    idx = bars.index

    out: list[dict] = []
    for k, i in enumerate(picks):
        i = int(i)
        out.append({
            "signal_ts": idx[i],
            "entry_ts": idx[i + int(entry_offset_bars)],
            "side": str(sides[k]),
            "signal_osc": float(osc[i]),
            "signal_high": float(high[i]),
            "signal_low": float(low[i]),
            "signal_delta_pct": float(delta[i]),
            "signal_trend": int(trend[i]),
        })
    return out


def run_grid(
    bars: pd.DataFrame,
    arms: list[Arm] | None = None,
    exits: list[ExitModel] | None = None,
    *,
    sweeps: dict[str, list] | None = None,
    progress: bool = False,
) -> pd.DataFrame:
    """Cartesian product of arms x exits x sweeps. One row per cell.

    Returns a RangeIndex DataFrame with columns EXACTLY RESULT_COLUMNS:
      arm_id, arm_label, exit_id, delta_threshold, quadrant_threshold,
      max_signal_age_bars, entry_offset_bars, zero_cross_kills,
      then every key of compute_metrics() with the same names and types.

    Cells producing zero trades still get a row (n_trades=0,
    sufficiency="not_reported"). Empty cells are NEVER dropped -- an empty cell
    is a finding: it is how "the gates reject everything" shows up.

    WARMUP: indicators are computed on the FULL frame so their warmup is real,
    then entries whose SIGNAL bar falls in the first WARMUP_BARS bars are
    discarded. Slicing `bars` instead would restart the ATR(200) warmup inside
    the tradeable window and quietly corrupt every gate (SPEC §3.7).

    Deduplication: cells differing only in a parameter that is dead for their
    arm are collapsed (SPEC §5.2), so a disabled gate cannot inflate the
    multiple-comparison count. Dead parameters appear as NaN in the row.

    Deterministic: identical inputs produce byte-identical output.
    """
    _data.validate_bars(bars)
    arms = build_arms() if arms is None else list(arms)
    exits = build_exit_grid() if exits is None else list(exits)
    merged: dict[str, list] = dict(DEFAULT_SWEEPS)
    if sweeps:
        merged.update(sweeps)

    warm = WARMUP_BARS  # module-level lookup so tests may monkeypatch it
    if len(bars) <= warm:
        _log.warning(
            "run_grid: %d bars is at or below WARMUP_BARS=%d -- no cell can "
            "produce a trade. Every cell will be empty. This is a data problem, "
            "not a finding.", len(bars), warm,
        )

    # Indicators depend only on bars, not on any grid axis: compute once.
    tp = _two_pole.compute(bars)
    vd = _vidya.compute(bars)
    first_tradeable = bars.index[warm] if len(bars) > warm else None

    # Entries depend on (arm, sweep) but NOT on the exit model, so they are
    # resolved once per sweep cell and reused across all 15 exits.
    a0_shape: dict[tuple, tuple[int, int]] = {}
    rows: list[dict[str, Any]] = []
    total = 0
    discarded_total = 0

    for arm in arms:
        for cell in _iter_cells(arm, merged):
            live = cell.pop("_live")
            params = dataclasses.replace(
                arm.params,
                delta_threshold=float(cell["delta_threshold"]),
                quadrant_threshold=float(cell["quadrant_threshold"]),
                entry_offset_bars=int(cell["entry_offset_bars"]),
                max_signal_age_bars=int(cell["max_signal_age_bars"]),
                zero_cross_kills=bool(cell["zero_cross_kills"]),
            )
            shape_key = tuple(cell[k] for k in _SWEEP_KEYS)

            if arm.arm_id == RANDOM_BASELINE_ARM_ID:
                n_e, n_l = a0_shape.get(shape_key, (0, 0))
                if shape_key not in a0_shape:
                    _log.warning(
                        "A7 random baseline has no matching %s cell to match "
                        "count/ratio against; emitting zero entries for %s.",
                        BASELINE_ARM_ID, shape_key,
                    )
                entries = _random_entries(
                    bars, tp, vd,
                    n_entries=n_e, n_long=n_l,
                    entry_offset_bars=params.entry_offset_bars,
                    warmup_bars=warm, token=_cell_token(arm, cell),
                )
            else:
                signals = _strategy.build_signals(bars, tp, vd, params)
                entries = _strategy.resolve_entries(bars, signals, params)
                if first_tradeable is None:
                    entries = []
                else:
                    entries = [e for e in entries if e["signal_ts"] >= first_tradeable]
                if arm.arm_id == BASELINE_ARM_ID:
                    n_l = sum(1 for e in entries if e["side"] == "long")
                    a0_shape[shape_key] = (len(entries), n_l)

            for ex in exits:
                trades = _strategy.simulate(bars, entries, ex, params)
                discarded_total += int(trades.attrs.get("discarded_entries", 0) or 0)
                row: dict[str, Any] = {
                    "arm_id": arm.arm_id,
                    "arm_label": arm.label,
                    "exit_id": ex.exit_id,
                    "delta_threshold": _dead(float(cell["delta_threshold"]),
                                             "delta_threshold", live),
                    "quadrant_threshold": _dead(float(cell["quadrant_threshold"]),
                                                "quadrant_threshold", live),
                    "max_signal_age_bars": int(cell["max_signal_age_bars"]),
                    "entry_offset_bars": int(cell["entry_offset_bars"]),
                    "zero_cross_kills": bool(cell["zero_cross_kills"]),
                }
                row.update(compute_metrics(trades))
                rows.append(row)
                total += 1
                if progress and total % 50 == 0:
                    print(f"  ... {total} cells", file=sys.stderr, flush=True)

    if discarded_total:
        # Cannot be tabulated: run_grid's columns are frozen (OPEN QUESTION Q7).
        _log.warning(
            "%d pending entries were discarded across the grid because a "
            "position was already open (allow_pyramiding=False).", discarded_total,
        )

    out = pd.DataFrame(rows, columns=list(RESULT_COLUMNS))
    if out.empty:
        out = pd.DataFrame({c: pd.Series(dtype=_CELL_COLUMNS.get(
            c, _METRIC_DTYPES.get(c, "float64"))) for c in RESULT_COLUMNS})
        return out
    for col, dt in {**_CELL_COLUMNS, **_METRIC_DTYPES}.items():
        out[col] = out[col].astype(dt)
    for col in METRIC_KEYS:
        if col not in _METRIC_DTYPES:
            out[col] = out[col].astype("float64")
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------
# Walk-forward (supplementary -- OPEN QUESTION Q1)
# --------------------------------------------------------------------------

def walk_forward_split(
    bars: pd.DataFrame,
    n_splits: int = 3,
    *,
    warmup_bars: int | None = None,
    min_test_bars: int = 180,
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Anchored walk-forward split into (train, test) BarFrame pairs.

    Both legs are ordinary BarFrames that can be handed straight to run_grid:

      train = bars[0 : split]                     run_grid drops its first 400
      test  = bars[split - warmup : split + w]    run_grid drops its first 400,
                                                  which lands EXACTLY on `split`

    That prefix on the test leg is the trick that keeps run_grid's frozen
    signature usable: the test window carries its own 400 bars of indicator
    history, so ATR(200) is fully warm at the first tradeable bar and no trade
    is generated before `split`. Train windows are anchored (they always start
    at bar 0) and grow with each fold.

    Returns fewer than n_splits pairs -- possibly none -- when the series is too
    short. That is not an error; it is the honest answer for short history.
    """
    _data.validate_bars(bars)
    warm = WARMUP_BARS if warmup_bars is None else warmup_bars
    n = len(bars)
    usable = n - warm
    if usable < (n_splits + 1) * min_test_bars:
        _log.warning(
            "walk_forward_split: %d post-warmup bars cannot support %d folds of "
            ">= %d test bars. Returning fewer folds.", usable, n_splits, min_test_bars,
        )
    folds: list[tuple[pd.DataFrame, pd.DataFrame]] = []
    if usable < 2 * min_test_bars:
        return folds
    width = usable // (n_splits + 1)
    if width < min_test_bars:
        width = min_test_bars
    for k in range(1, n_splits + 1):
        split = warm + width * k
        end = min(split + width, n)
        if split >= n or (end - split) < min_test_bars:
            break
        train = bars.iloc[0:split].copy()
        test = bars.iloc[max(0, split - warm):end].copy()
        if len(train) <= warm or len(test) <= warm:
            continue
        folds.append((train, test))
    return folds


def run_walk_forward(
    bars: pd.DataFrame,
    arms: list[Arm] | None = None,
    exits: list[ExitModel] | None = None,
    *,
    sweeps: dict[str, list] | None = None,
    n_splits: int = 3,
    progress: bool = False,
) -> pd.DataFrame:
    """Run the grid on each walk-forward fold's train and test leg.

    Returns a RangeIndex frame with columns ["fold_id", "leg"] + RESULT_COLUMNS,
    where leg is "train" or "test". Empty (zero-row, correct columns) when the
    series cannot be split.

    This is the anti-overfit measurement: an arm that wins in-sample and
    collapses out-of-sample was fitted, not discovered.
    """
    folds = walk_forward_split(bars, n_splits)
    frames: list[pd.DataFrame] = []
    for fid, (train, test) in enumerate(folds):
        for leg, frame in (("train", train), ("test", test)):
            if progress:
                print(f"walk-forward fold {fid} {leg}: {len(frame)} bars",
                      file=sys.stderr, flush=True)
            res = run_grid(frame, arms, exits, sweeps=sweeps, progress=progress)
            res.insert(0, "leg", leg)
            res.insert(0, "fold_id", fid)
            frames.append(res)
    if not frames:
        empty = run_grid(bars.iloc[:0], arms, exits, sweeps=sweeps) \
            if False else pd.DataFrame(columns=["fold_id", "leg", *RESULT_COLUMNS])
        return empty
    return pd.concat(frames, ignore_index=True)


def walk_forward_summary(wf_results: pd.DataFrame) -> pd.DataFrame:
    """Per fold: pick the best REPORTABLE in-sample cell, look it up out-of-sample.

    Returns columns: fold_id, arm_id, exit_id, delta_threshold,
    quadrant_threshold, entry_offset_bars, is_expectancy_r, is_n, oos_expectancy_r,
    oos_n, oos_sufficiency, held_up (bool -- OOS expectancy still positive AND
    OOS cell reportable).

    Folds where no in-sample cell reaches MIN_TRADES_REPORTABLE are omitted:
    there is nothing to select, and selecting from noise is the error this
    function exists to detect.
    """
    cols = ["fold_id", "arm_id", "exit_id", "delta_threshold", "quadrant_threshold",
            "entry_offset_bars", "is_expectancy_r", "is_n", "oos_expectancy_r",
            "oos_n", "oos_sufficiency", "held_up"]
    if wf_results is None or wf_results.empty:
        return pd.DataFrame(columns=cols)
    key = ["arm_id", "exit_id", "delta_threshold", "quadrant_threshold",
           "entry_offset_bars", "max_signal_age_bars", "zero_cross_kills"]
    rows = []
    for fid, grp in wf_results.groupby("fold_id", sort=True):
        tr = grp[(grp["leg"] == "train") & (grp["sufficiency"] == "reportable")]
        if tr.empty:
            continue
        best = tr.sort_values("expectancy_r", ascending=False).iloc[0]
        te = grp[grp["leg"] == "test"]
        for k in key:
            te = te[te[k].isna() & pd.isna(best[k]) | (te[k] == best[k])] \
                if pd.isna(best[k]) else te[te[k] == best[k]]
        oos = te.iloc[0] if len(te) else None
        rows.append({
            "fold_id": int(fid), "arm_id": best["arm_id"], "exit_id": best["exit_id"],
            "delta_threshold": best["delta_threshold"],
            "quadrant_threshold": best["quadrant_threshold"],
            "entry_offset_bars": int(best["entry_offset_bars"]),
            "is_expectancy_r": float(best["expectancy_r"]), "is_n": int(best["n_trades"]),
            "oos_expectancy_r": float(oos["expectancy_r"]) if oos is not None else float("nan"),
            "oos_n": int(oos["n_trades"]) if oos is not None else 0,
            "oos_sufficiency": str(oos["sufficiency"]) if oos is not None else "not_reported",
            "held_up": bool(oos is not None
                            and oos["sufficiency"] == "reportable"
                            and float(oos["expectancy_r"]) > 0.0),
        })
    return pd.DataFrame(rows, columns=cols)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _f(v: Any, nd: int = 3) -> str:
    """Format a float for markdown; em-dash for nan/None, 'inf' preserved."""
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not np.isfinite(x):
        return "inf" if x > 0 else ("-inf" if x < 0 else "—")
    return f"{x:.{nd}f}"


def _cell_label(row: pd.Series) -> str:
    """Human-readable cell identifier used in every refusal message."""
    bits = [f"{row['arm_id']}/{row['arm_label']}", str(row["exit_id"])]
    if pd.notna(row.get("delta_threshold")):
        bits.append(f"delta>={_f(row['delta_threshold'], 1)}")
    if pd.notna(row.get("quadrant_threshold")):
        bits.append(f"quad={_f(row['quadrant_threshold'], 2)}")
    bits.append(f"off={int(row['entry_offset_bars'])}")
    return " × ".join(bits[:2]) + " (" + ", ".join(bits[2:]) + ")"


def _render_not_reported(row: pd.Series) -> str:
    """SPEC §6.1 -- fewer than 10 trades. Exact wording; do not paraphrase."""
    return (
        f"> **NOT REPORTED — {_cell_label(row)}: {int(row['n_trades'])} trades.** "
        f"Below the 10-trade floor. No win rate, expectancy, or profit factor is "
        f"shown for this cell, because any statistic computed on "
        f"{int(row['n_trades'])} trades would be indistinguishable from noise. "
        f"This cell is evidence about nothing."
    )


def _render_inconclusive(row: pd.Series) -> str:
    """SPEC §6.2 -- 10 to 29 trades. Exact wording; do not paraphrase."""
    return (
        f"> **INCONCLUSIVE — {_cell_label(row)}: {int(row['n_trades'])} trades** "
        f"(30 required for a conclusion). Point estimate: expectancy "
        f"{_f(row['expectancy_r'])}R, 95% CI [{_f(row['expectancy_r_ci_lo'])}, "
        f"{_f(row['expectancy_r_ci_hi'])}]. **This sample cannot distinguish this "
        f"arm from the baseline and must not be used to make a trading decision.** "
        f"The interval is shown so the reader can see how wide it is, not so the "
        f"point estimate can be quoted."
    )


def _match_canonical(results: pd.DataFrame, arm_id: str) -> pd.DataFrame:
    """Rows for `arm_id` at the canonical (live-rules) sweep cell.

    A dead parameter is NaN in the row and matches unconditionally, so an arm
    with the delta gate disabled still resolves to a single canonical cell.
    """
    sub = results[results["arm_id"] == arm_id]
    for key, want in CANONICAL_CELL.items():
        if key not in sub.columns:
            continue
        col = sub[key]
        sub = sub[col.isna() | (col == want)]
    return sub


def _overlaps(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    """True when two intervals overlap, or when either is undefined."""
    if not all(np.isfinite([lo1, hi1, lo2, hi2])):
        return True
    return lo1 <= hi2 and lo2 <= hi1


def _verdict_block(results: pd.DataFrame) -> str:
    """SPEC §6.3 -- exactly one verdict, chosen mechanically.

    (a) no cell reaches 30 trades
    (b) reportable cells exist but the best arm's CI overlaps the baseline's
    (c) separation exists
    (d) SPEC GAP: reportable cells exist but the A0 baseline is not itself
        reportable, so (b)/(c) would quote baseline numbers that do not exist.
        See OPEN QUESTION Q5.
    """
    k = int(len(results))
    rep = results[results["sufficiency"] == "reportable"]
    n_bars = int(results.attrs.get("n_bars", 0))
    days = float(results.attrs.get("days", float("nan")))

    if rep.empty:
        return (
            f"> **NO CONCLUSION IS AVAILABLE FROM THIS RUN.** No arm produced 30 "
            f"or more trades over {n_bars} bars ({_f(days, 1)} days). The central "
            f"question — whether the ribbon and delta gates add or destroy "
            f"expectancy — **is not answered by this data.** The correct next step "
            f"is more history, not a different parameter. Do not act on the "
            f"ranking below; it is sorted noise."
        )

    best = rep.sort_values("expectancy_r", ascending=False).iloc[0]
    base_pool = _match_canonical(rep, BASELINE_ARM_ID)
    if base_pool.empty:
        base_pool = rep[rep["arm_id"] == BASELINE_ARM_ID]
    if base_pool.empty:
        return (
            f"> **NO BASELINE COMPARISON IS AVAILABLE.** Reportable cells exist "
            f"(best: {_cell_label(best)} at {_f(best['expectancy_r'])}R, "
            f"n={int(best['n_trades'])}), but the {BASELINE_ARM_ID} baseline — the "
            f"user's live rules — did not itself reach {MIN_TRADES_REPORTABLE} "
            f"trades in any cell. **There is therefore nothing to compare against, "
            f"and no claim about whether the gates add or destroy expectancy can be "
            f"made.** That the gates admit too few trades to measure is itself the "
            f"most important finding in this run."
        )

    base = base_pool.sort_values("n_trades", ascending=False).iloc[0]
    if _overlaps(best["expectancy_r_ci_lo"], best["expectancy_r_ci_hi"],
                 base["expectancy_r_ci_lo"], base["expectancy_r_ci_hi"]):
        return (
            f"> **NO ARM SEPARATES FROM BASELINE.** The best reportable arm is "
            f"{_cell_label(best)} at {_f(best['expectancy_r'])}R (95% CI "
            f"[{_f(best['expectancy_r_ci_lo'])}, {_f(best['expectancy_r_ci_hi'])}], "
            f"n={int(best['n_trades'])}); the baseline {_cell_label(base)} is "
            f"{_f(base['expectancy_r'])}R (95% CI [{_f(base['expectancy_r_ci_lo'])}, "
            f"{_f(base['expectancy_r_ci_hi'])}], n={int(base['n_trades'])}). **These "
            f"intervals overlap.** On this data the gates are neither shown to add "
            f"expectancy nor shown to destroy it. A difference in point estimates "
            f"without separated intervals is not a finding."
        )

    n_rep = int(len(rep))
    return (
        f"> **SEPARATION FOUND.** {_cell_label(best)} at {_f(best['expectancy_r'])}R "
        f"(95% CI [{_f(best['expectancy_r_ci_lo'])}, {_f(best['expectancy_r_ci_hi'])}], "
        f"n={int(best['n_trades'])}) does not overlap {_cell_label(base)} at "
        f"{_f(base['expectancy_r'])}R (95% CI [{_f(base['expectancy_r_ci_lo'])}, "
        f"{_f(base['expectancy_r_ci_hi'])}], n={int(base['n_trades'])}). **This is a "
        f"single un-corrected comparison drawn from {k} grid cells; at that many "
        f"cells, some separation is expected by chance alone.** Treat this as a "
        f"hypothesis to test forward, not a validated edge.\n>\n"
        f"> Multiple-comparison guard: {n_rep} of {k} cells were eligible to be "
        f"named a winner. At alpha=0.05 that is "
        f"{_f(expected_false_positives(n_rep), 1)} spurious separations expected by "
        f"chance; the Sidak-corrected per-comparison alpha is "
        f"{_f(sidak_alpha(n_rep), 5)}. Re-run bootstrap_ci() at that alpha on the "
        f"winner's trades before believing this."
    )


# SPEC §6.4 -- verbatim, first block of every report. Do not edit.
_DISCLAIMER = (
    "> **PROVISIONAL — NOT VERIFIED AGAINST A LIVE CHART.**\n"
    "> The Two-Pole Oscillator and Volumatic VIDYA in this harness are ports. "
    "They have been tested against synthetic data only. Until the `--verify` CSV "
    "has been diffed against the user's TradingView chart and the four signal-log "
    "bars match within ±0.02 (checklist item U-0), **every number in this report "
    "is provisional and may be measuring a bug rather than a market.**\n"
    "> Additionally, the Delta Volume threshold of ±20% was chosen against a "
    "per-bar reading, while this harness computes the correct cumulative-per-leg "
    "reading (checklist item U-1). Any result quoted at a ±20% threshold is "
    "quoting a gate that is probably close to a no-op."
)


def render_report(
    results: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    top_n: int = 20,
) -> str:
    """Render the markdown report. Returns the report as a string.

    Contains, in order (SPEC §3.6):
      1. the §6.4 provisional-port disclaimer, verbatim, as the first block
      2. data coverage + the buy-and-hold benchmark
      3. THE CENTRAL QUESTION table: A0 beside A1 and A5, per exit model
      4. the full grid, top_n rows, each carrying its sufficiency verdict
      5. the §6.3 statistical-honesty verdict block

    Non-reportable cells are rendered with the exact wording of §6.1/§6.2 and
    their statistics are blanked in the tables. No winner is ever named from a
    non-reportable cell; if filtering to reportable leaves nothing, the report
    says so (§6.3(a)).

    Sort order of the full grid is (sufficiency_rank asc, expectancy_r desc) --
    a deliberate documented deviation from a pure expectancy sort, which would
    put pure noise in row 1. See OPEN QUESTION Q4.
    """
    _data.validate_bars(bars)
    res = results.copy()
    days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400.0 if len(bars) > 1 else float("nan")
    res.attrs["n_bars"] = int(len(bars))
    res.attrs["days"] = days

    out: list[str] = []
    out.append("# BTC 4h ablation — gate and exit grid\n")
    out.append(_DISCLAIMER + "\n")

    # ---- 2. coverage -----------------------------------------------------
    bh = buy_and_hold_benchmark(bars)
    out.append("## 1. Data coverage\n")
    out.append(f"- First bar: `{bars.index[0]}`")
    out.append(f"- Last bar: `{bars.index[-1]}`")
    out.append(f"- Bars: {len(bars)} (4h) — {_f(days, 1)} days")
    out.append(f"- Warmup excluded from trade generation: first {WARMUP_BARS} bars "
               f"(ATR(200) + VIDYA(34) + SMA(25)/two-pole(20) chain)")
    out.append(f"- Tradeable bars: {max(0, len(bars) - WARMUP_BARS)}")
    out.append(f"- Grid cells evaluated: **{len(res)}**\n")
    out.append("### Buy-and-hold benchmark (tradeable span)\n")
    out.append(f"- {_f(bh['start_price'], 2)} → {_f(bh['end_price'], 2)} "
               f"= **{_f(bh['return_pct'], 2)}%** over {_f(bh['days'], 1)} days "
               f"(max DD {_f(bh['max_drawdown_pct'], 2)}%)")
    out.append("- **Not directly comparable to the cells below.** This is a "
               "compounded hold; every cell reports a SUM of per-trade percentages, "
               "because compounding a backtest with no position-sizing model "
               "manufactures a curve (SPEC §6.5). Use it to ask whether the signal "
               "beat doing nothing at all, not by how much.\n")

    # ---- 3. the central question -----------------------------------------
    out.append("## 2. THE CENTRAL QUESTION\n")
    out.append("The VIDYA flips *after* a turn, so the ribbon+delta gates structurally "
               "reject turn signals and admit only pullback-in-trend signals. "
               "**A0** is the user's live rules; **A1** is the dot plus the zone only "
               "(the arm that would have taken the Aug 13 long); **A5** requires the "
               "ribbon to OPPOSE the trade. If A5 beats A2/A0, the gates are "
               "rejecting the good trades.\n")
    out.append("All three arms are shown at their **canonical cell** (the user's live "
               "parameters: delta 20.0, quadrant 0.5, offset 1), *not* at each arm's "
               "best-expectancy cell — picking the best cell per arm for a head-to-head "
               "is exactly the multiple-comparison error this harness exists to avoid.\n")
    out.append("| exit_id | A0 all_gates | A1 quadrant_only | A5 inverted_ribbon |")
    out.append("|---|---|---|---|")
    refusals: list[str] = []
    for ex_id in res["exit_id"].drop_duplicates():
        cells = []
        for arm_id in ("A0", "A1", "A5"):
            sub = _match_canonical(res[res["exit_id"] == ex_id], arm_id)
            if sub.empty:
                cells.append("—")
                continue
            row = sub.iloc[0]
            suff = row["sufficiency"]
            if suff == "not_reported":
                cells.append(f"n={int(row['n_trades'])} · NOT REPORTED")
                refusals.append(_render_not_reported(row))
            elif suff == "inconclusive":
                cells.append(f"{_f(row['expectancy_r'])}R "
                             f"[{_f(row['expectancy_r_ci_lo'])}, {_f(row['expectancy_r_ci_hi'])}] "
                             f"· n={int(row['n_trades'])} · INCONCLUSIVE")
                refusals.append(_render_inconclusive(row))
            else:
                cells.append(f"**{_f(row['expectancy_r'])}R** "
                             f"[{_f(row['expectancy_r_ci_lo'])}, {_f(row['expectancy_r_ci_hi'])}] "
                             f"· n={int(row['n_trades'])} · reportable")
        out.append(f"| `{ex_id}` | " + " | ".join(cells) + " |")
    out.append("")
    if refusals:
        out.append("### Why those cells carry no conclusion\n")
        seen: set[str] = set()
        for r in refusals:
            if r not in seen:
                seen.add(r)
                out.append(r + "\n")

    # ---- 4. full grid ----------------------------------------------------
    out.append(f"## 3. Full grid (top {top_n})\n")
    out.append("Sorted by **sufficiency first, then expectancy descending**. A pure "
               "expectancy sort would place an un-reportable 3-trade cell in row 1; "
               "cells that cannot be reported are shown last with their statistics "
               "blanked, per SPEC §6.1.\n")
    res["_rank"] = res["sufficiency"].map(_SUFFICIENCY_RANK).fillna(9).astype(int)
    ranked = res.sort_values(
        ["_rank", "expectancy_r", "n_trades"], ascending=[True, False, False],
        kind="mergesort",
    ).head(top_n)
    out.append("| arm | exit | δ | quad | off | n | eoD | exp R | 95% CI | win% | PF | "
               "maxDD% | hold(bars) | trades/mo | sufficiency |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, row in ranked.iterrows():
        blank = row["sufficiency"] == "not_reported"
        tpm = trades_per_month(int(row["n_trades"]), bars)
        cells = [
            f"{row['arm_id']} {row['arm_label']}", f"`{row['exit_id']}`",
            _f(row["delta_threshold"], 1), _f(row["quadrant_threshold"], 2),
            str(int(row["entry_offset_bars"])), str(int(row["n_trades"])),
            str(int(row["n_end_of_data"])),
            "—" if blank else _f(row["expectancy_r"]),
            "—" if blank else f"[{_f(row['expectancy_r_ci_lo'])}, {_f(row['expectancy_r_ci_hi'])}]",
            "—" if blank else _f(100.0 * row["win_rate"], 1),
            "—" if blank else _f(row["profit_factor"], 2),
            "—" if blank else _f(row["max_drawdown_pct"], 2),
            "—" if blank else _f(row["avg_bars_held"], 1),
            _f(tpm, 2), row["sufficiency"],
        ]
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    out.append("`eoD` = trades force-closed at the end of the data. A cell where those "
               "dominate has not measured its exit model (SPEC §6.5).\n")

    n_rep = int((res["sufficiency"] == "reportable").sum())
    if n_rep == 0:
        out.append("**No cell in this grid is reportable.** No winner is named, "
                   "because naming one from a sub-30-trade cell would be naming noise.\n")

    # ---- 5. verdict ------------------------------------------------------
    out.append("## 4. Statistical-honesty verdict\n")
    out.append(_verdict_block(res) + "\n")
    out.append(f"Cells evaluated: **{len(res)}** · reportable: **{n_rep}** · "
               f"expected spurious separations at alpha=0.05: "
               f"**{_f(expected_false_positives(n_rep), 1)}**\n")
    return "\n".join(out)


def render_walk_forward_section(wf_results: pd.DataFrame) -> str:
    """Render the walk-forward section as markdown (appended by the CLI).

    Kept out of render_report because SPEC §3.6 freezes that signature to
    (results, bars, *, top_n). Returns a short 'not available' note when the
    series was too short to split.
    """
    out = ["## 5. Walk-forward (out-of-sample check)\n"]
    summary = walk_forward_summary(wf_results)
    if summary.empty:
        out.append("> **NOT AVAILABLE.** No fold produced an in-sample cell reaching "
                   f"{MIN_TRADES_REPORTABLE} trades, so there was nothing to select "
                   "and nothing to validate out-of-sample. This is the expected "
                   "outcome on short history and is not a failure of the harness.\n")
        return "\n".join(out)
    out.append("For each fold the best **reportable** in-sample cell is selected, then "
               "that same cell — not a re-optimised one — is read out-of-sample. A cell "
               "that wins in-sample and collapses out-of-sample was fitted, not found.\n")
    out.append("| fold | selected cell | IS exp R | IS n | OOS exp R | OOS n | OOS suff | held up |")
    out.append("|---|---|---|---|---|---|---|---|")
    for _, r in summary.iterrows():
        out.append(
            f"| {int(r['fold_id'])} | {r['arm_id']} × `{r['exit_id']}` "
            f"(δ={_f(r['delta_threshold'],1)}, q={_f(r['quadrant_threshold'],2)}, "
            f"off={int(r['entry_offset_bars'])}) | {_f(r['is_expectancy_r'])} | "
            f"{int(r['is_n'])} | {_f(r['oos_expectancy_r'])} | {int(r['oos_n'])} | "
            f"{r['oos_sufficiency']} | {'yes' if r['held_up'] else 'NO'} |"
        )
    held = int(summary["held_up"].sum())
    out.append("")
    out.append(f"**{held} of {len(summary)} folds held up out-of-sample.** Anything "
               "below all folds is evidence of curve-fitting, not of an edge.\n")
    return "\n".join(out)
