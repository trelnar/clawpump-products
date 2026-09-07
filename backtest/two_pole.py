"""Two-Pole Oscillator [BigBeluga] -- port for the BTC 4h backtest harness.

Implements SPEC.md 2.1 and 3.3: the 25-bar z-score normalisation, the two-pole
(twice-applied EMA) filter, the 4-bar delayed signal line, ZERO-LINE-GATED dot
detection, and the switchable diagnostic tint.

Pine origin
-----------
BigBeluga's "Two-Pole Oscillator". The user's chart is customised to
filter_length=20 (the published script defaults to 15), with sma_length=25 used
for BOTH the mean and the normalisation denominator, and a POPULATION standard
deviation (ddof=0).

TRADE THE DOT, NOT THE COLOUR
-----------------------------
`tint` is diagnostic output only. `strategy.py` must not read it (SPEC.md 2.1
step 5; build rule 9). The tradeable event is `dot_long` / `dot_short`.

THE ZERO-LINE GATE IS MANDATORY
-------------------------------
`dot_long` requires `osc < 0`; `dot_short` requires `osc > 0`. An earlier
version of the user's bot omitted this gate and printed phantom mid-zone dots
that were absent from his chart.

SMOOTHING WARNING
-----------------
The filter is heavily smoothed: a single -2% candle moved it by ~0.001. No test
may assume a sharp candle swings the oscillator; use sustained multi-bar
excursions.

PROVISIONAL
-----------
This port is synthetic-data tested only. Until the `--verify` CSV has been
diffed against the user's TradingView chart (checklist U-0), every value it
produces is provisional and may be measuring a bug rather than a market.
Points that need chart verification are marked `# VERIFY:` inline.

OPEN QUESTIONS:
  * U-2 (tint rule: slope or osc/signal cross). The source specs disagree.
    Default is "slope", because chart evidence showed teal appearing one bar
    BEFORE the dot, which the cross rule cannot produce. "cross" is one flag
    away. Diagnostic only, so it cannot change any trade.
  * U-5 (filter_length 20 vs the script default 15). Implemented as 20, per the
    user's chart. If his indicator settings read 15, every SIGNAL_LOG
    comparison in U-0 must be re-run.
  * Recursion seeding. SPEC.md 2.1 seeds p1[i0] = p2[i0] = z[i0] at the first
    finite z. That matches the usual Pine idiom
    `f1 := na(f1) ? src : ...` / `f2 := na(f2) ? f1 : ...`. If the real script
    instead seeds via `nz()` (i.e. from 0), early bars differ; the discrepancy
    decays as (1-alpha)^n and is far below 1e-15 after the harness's 400-bar
    warmup, so it cannot affect any reported trade.
  * Interior NaN in the normalised series (a zero-variance 25-bar window).
    SPEC.md says the filter "holds the previous state" but does not say whether
    the value EMITTED on such a bar is the held state or NaN. This module emits
    the held state, so the oscillator stays continuous and cross detection is
    not broken by a flat window. On BTC 4h a zero-variance 25-bar window does
    not occur in practice.
  * SPEC.md's tint formula is a bare `+1 if ... else -1` with no undefined
    case, but 3.3 declares a `0 = undefined` state. This module emits 0
    wherever either operand of the comparison is non-finite (i.e. warmup only)
    and never elsewhere.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

try:  # normal package import: `import backtest.two_pole`
    from .data import BAR_COLUMNS, validate_bars
except ImportError:  # pragma: no cover - flat-path fallback
    from backtest.data import BAR_COLUMNS, validate_bars

__all__ = [
    "TINT_RULES",
    "TWO_POLE_COLUMNS",
    "TwoPoleParams",
    "two_pole_filter",
    "compute",
]

#: The permitted values of ``TwoPoleParams.tint_rule`` (SPEC.md 2.1 step 5).
TINT_RULES: tuple[str, ...] = ("slope", "cross")

#: Exact output column order of :func:`compute` (SPEC.md 3.3). Frozen contract.
TWO_POLE_COLUMNS: list[str] = ["osc", "osc_signal", "tint", "dot_long", "dot_short"]


@dataclasses.dataclass(frozen=True)
class TwoPoleParams:
    """Parameters for the Two-Pole Oscillator, defaulted to the user's chart.

    Attributes
    ----------
    filter_length:
        Length of the two-pole filter. ``alpha = 2/(filter_length+1)``. The
        user's chart is customised to 20; the published Pine script defaults to
        15 (checklist U-5).
    sma_length:
        Window used for BOTH the rolling mean and the normalisation
        denominator (the standard deviation).
    signal_delay:
        The signal line is a plain N-bar delayed copy of the oscillator.
    ddof:
        Delta degrees of freedom for the standard deviation. MUST be 0 --
        Pine's ``ta.stdev`` is a POPULATION standard deviation.
    tint_rule:
        ``"slope"`` or ``"cross"``. Diagnostic only; see U-2.
    source:
        Which OHLCV column to normalise. Must be one of ``BAR_COLUMNS``.
    """

    filter_length: int = 20        # user's chart; script default is 15 (U-5)
    sma_length: int = 25           # mean AND normalisation denominator
    signal_delay: int = 4
    ddof: int = 0                  # POPULATION std. Do not change.
    tint_rule: str = "slope"       # "slope" | "cross" -- DISPUTED, see U-2
    source: str = "close"

    def __post_init__(self) -> None:
        """Validate the parameter set. Raises ValueError on a bad combination."""
        if self.filter_length < 1:
            raise ValueError(f"filter_length must be >= 1, got {self.filter_length}")
        if self.sma_length < 1:
            raise ValueError(f"sma_length must be >= 1, got {self.sma_length}")
        if self.signal_delay < 0:
            raise ValueError(f"signal_delay must be >= 0, got {self.signal_delay}")
        if self.ddof not in (0, 1):
            raise ValueError(f"ddof must be 0 (population) or 1, got {self.ddof}")
        if self.tint_rule not in TINT_RULES:
            raise ValueError(
                f"tint_rule must be one of {TINT_RULES}, got {self.tint_rule!r}"
            )
        if self.source not in BAR_COLUMNS:
            raise ValueError(
                f"source must be one of {BAR_COLUMNS}, got {self.source!r}"
            )


def two_pole_filter(x: pd.Series, length: int) -> pd.Series:
    """Apply the two-pole (twice-applied EMA-style) filter to ``x``.

    Pine origin: BigBeluga's ``f_two_pole_filter``, i.e. the recursion

    .. code-block:: text

        alpha = 2 / (length + 1)
        f1 := na(f1) ? src : alpha*src + (1-alpha)*f1
        f2 := na(f2) ? f1  : alpha*f1  + (1-alpha)*f2

    Parameters
    ----------
    x:
        Input series (the normalised source). Any index.
    length:
        Filter length; ``alpha = 2/(length+1)``.

    Returns
    -------
    pandas.Series
        float64, same index, same length and order as ``x``, name preserved.
        Leading NaNs are preserved until the first finite value of ``x``; at
        that bar the filter seeds ``p1 = p2 = x`` and emits ``x``. Interior
        NaNs HOLD the previous filter state and re-emit it (they do not
        propagate NaN and do not advance the recursion).

    Notes
    -----
    The recursion is inherently sequential, so this is an explicit loop over a
    float64 numpy array rather than a vectorised expression. Everything around
    it in :func:`compute` is vectorised.
    """
    if not isinstance(x, pd.Series):
        raise TypeError(f"x must be a pandas.Series, got {type(x).__name__}")
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")

    # Pine origin: alpha = 2 / (length + 1), the standard EMA smoothing factor.
    # VERIFY: that BigBeluga uses this alpha and applies it TWICE (two poles),
    # rather than e.g. a Butterworth/Ehlers two-pole with a different kernel.
    alpha = 2.0 / (float(length) + 1.0)
    one_minus_alpha = 1.0 - alpha

    values = x.to_numpy(dtype="float64", copy=True)
    out = np.full(values.shape, np.nan, dtype="float64")

    p1 = np.nan
    p2 = np.nan
    started = False

    for i in range(values.size):
        v = values[i]
        if not started:
            if np.isnan(v):
                continue  # leading NaN -> output stays NaN, filter not yet seeded
            # Pine origin: f1 := na(f1) ? src ; f2 := na(f2) ? f1  (== src here)
            p1 = v
            p2 = v
            started = True
        elif not np.isnan(v):
            # Pole 1 then pole 2 -- the same alpha applied twice, in series.
            p1 = alpha * v + one_minus_alpha * p1
            p2 = alpha * p1 + one_minus_alpha * p2
        # else: interior NaN -> hold p1/p2 unchanged (see OPEN QUESTIONS).
        out[i] = p2

    return pd.Series(out, index=x.index, dtype="float64", name=x.name)


def compute(
    bars: pd.DataFrame, params: TwoPoleParams = TwoPoleParams()
) -> pd.DataFrame:
    """Compute the Two-Pole Oscillator, its signal line, tint, and dots.

    THE module entrypoint (SPEC.md 3.3).

    Parameters
    ----------
    bars:
        A 4h BarFrame as defined in SPEC.md 3.1. Validated on entry via
        ``data.validate_bars``. Not mutated.
    params:
        See :class:`TwoPoleParams`.

    Returns
    -------
    pandas.DataFrame
        Index ``.equals(bars.index)`` -- same length, same order, nothing
        dropped or reindexed. Columns EXACTLY, in this order:

        ==============  =========  ==================================================
        column          dtype      meaning
        ==============  =========  ==================================================
        ``osc``         float64    the oscillator
        ``osc_signal``  float64    ``osc.shift(signal_delay)``
        ``tint``        int8       +1 teal / -1 purple / 0 undefined. DIAGNOSTIC ONLY
        ``dot_long``    bool       cross_up AND ``osc < 0``  (teal dot)
        ``dot_short``   bool       cross_dn AND ``osc > 0``  (purple dot)
        ==============  =========  ==================================================

        Warmup: ``osc`` is NaN for the first ``sma_length - 1`` bars and the dot
        columns are False there. ``dot_long`` and ``dot_short`` are never both
        True on the same bar (the two crossings are mutually exclusive, and the
        zero-line gates are disjoint on top of that).

    Raises
    ------
    BarSchemaError
        If ``bars`` violates the BarFrame contract.
    ValueError
        If ``params.source`` is not an OHLCV column.
    """
    validate_bars(bars)

    src = bars[params.source].astype("float64")

    # ---- Step 1: normalise (SPEC.md 2.1 step 1) -----------------------------
    # Pine origin: (src - ta.sma(src, 25)) / ta.stdev(src, 25).
    # VERIFY: that the user's chart uses the SAME length (25) for the mean and
    # for the denominator, and that Pine's ta.stdev here is population (ddof=0)
    # rather than sample. Both are asserted by SPEC.md 2.1 but neither has been
    # read off the live chart.
    window = params.sma_length
    sma = src.rolling(window, min_periods=window).mean()
    sd = src.rolling(window, min_periods=window).std(ddof=params.ddof)

    # A zero-variance window would divide by zero. Blank it; two_pole_filter
    # then HOLDS its previous state across the gap rather than emitting NaN.
    sd = sd.where(sd > 0.0)

    z = (src - sma) / sd  # NaN for the first (sma_length - 1) bars

    # ---- Step 2: two-pole filter (SPEC.md 2.1 step 2) -----------------------
    osc = two_pole_filter(z, params.filter_length)
    osc.name = "osc"

    # ---- Step 3: signal line (SPEC.md 2.1 step 3) ---------------------------
    # Pine origin: a plain N-bar delayed copy of the oscillator, two_p[4].
    # VERIFY: that the chart's signal line is a raw 4-bar delay and not a
    # separately smoothed line. On the chart the two lines should be visually
    # IDENTICAL in shape, one simply shifted 4 bars to the right.
    osc_signal = osc.shift(params.signal_delay)
    osc_signal.name = "osc_signal"

    # ---- Step 4: dots, ZERO-LINE GATED (SPEC.md 2.1 step 4) -----------------
    # Pine origin: ta.crossover(two_p, two_p[4]) / ta.crossunder(...), i.e.
    #   crossover  = a > b and a[1] <= b[1]
    #   crossunder = a < b and a[1] >= b[1]
    # NaN operands compare False, so no dot can fire during warmup.
    osc_prev = osc.shift(1)
    sig_prev = osc_signal.shift(1)
    cross_up = (osc > osc_signal) & (osc_prev <= sig_prev)
    cross_dn = (osc < osc_signal) & (osc_prev >= sig_prev)

    # THE ZERO-LINE GATE. Mandatory -- omitting it prints phantom mid-zone dots
    # that are absent from the chart.
    # VERIFY: that the gate is evaluated on the CROSSING bar's oscillator value
    # (as implemented) and not on the prior bar or on the signal line, and that
    # the comparison is strict (< 0 / > 0) rather than <= / >=. A dot sitting
    # exactly on the zero line is the only case that could distinguish these.
    dot_long = (cross_up & (osc < 0.0)).to_numpy(dtype=bool)
    dot_short = (cross_dn & (osc > 0.0)).to_numpy(dtype=bool)

    # ---- Step 5: tint -- DIAGNOSTIC ONLY (SPEC.md 2.1 step 5, U-2) ----------
    # strategy.py must NOT read this column. Trade the dot, not the colour.
    osc_arr = osc.to_numpy(dtype="float64")
    if params.tint_rule == "slope":
        # VERIFY (U-2): pick any dot on the chart and look at the bar
        # immediately before it. If the line already carries the dot's colour
        # on that PRIOR bar, the slope rule is correct. Repeat on 3 dots.
        ref = osc.shift(1).to_numpy(dtype="float64")
    else:  # "cross"
        ref = osc_signal.to_numpy(dtype="float64")

    # SPEC.md gives a bare `+1 if greater else -1`; 3.3 adds a 0 = undefined
    # state. 0 is emitted wherever either operand is non-finite (warmup only).
    defined = np.isfinite(osc_arr) & np.isfinite(ref)
    tint = np.where(defined, np.where(osc_arr > ref, 1, -1), 0).astype("int8")

    # ---- Assemble the frozen output schema ---------------------------------
    out = pd.DataFrame(
        {
            "osc": osc_arr,
            "osc_signal": osc_signal.to_numpy(dtype="float64"),
            "tint": tint,
            "dot_long": dot_long,
            "dot_short": dot_short,
        },
        index=bars.index,
    )
    return out[TWO_POLE_COLUMNS]
