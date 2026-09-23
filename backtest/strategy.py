"""backtest.strategy -- gates, staleness, entry timing, exit models, and
bar-level position simulation for the BTC 4h harness.

Authority: backtest/SPEC.md sections 3.5, 5.1, 5.3 and 7. Where this module and
the spec disagree, the spec wins and this module is the bug.

LOOK-AHEAD DEFENCE (this module is where the bias would hide). Every value used
to decide something at bar ``i`` is listed with the bar on which it became
knowable:

  * dot / osc / delta_pct / trend at bar i -- known at the CLOSE of bar i.
  * signal_high / signal_low              -- the SIGNAL bar's extremes, which are
                                             strictly earlier than the entry bar.
  * ATR(atr_length)[entry_ts]             -- Wilder ATR through the entry bar,
                                             known at the entry bar's close.
  * entry fill                            -- the entry bar's CLOSE.
  * exit scanning                         -- begins at entry_idx + 1. No single
                                             bar may both fill an entry and its
                                             exit.
  * trailing stop level used during bar i -- derived only from bars <= i - 1. A
                                             bar's own high never sets the level
                                             that the same bar's low is tested
                                             against.
  * staleness / zero-cross window         -- bars in (signal_ts, entry_ts], all
                                             of which are <= entry_ts.

Two further anti-edge-manufacturing guards, both deliberate:

  * STOP-FIRST. If one bar's range contains both the stop and the target, the
    stop is assumed to have filled (SPEC 3.5). 4h bars cannot resolve intrabar
    order and optimistic resolution invents edge that does not exist.
  * WRONG-SIDED STOPS ARE REFUSED, NOT CLAMPED. ``atr_beyond_extreme`` can place
    a long's stop ABOVE the entry when price fell hard between the signal bar and
    the entry bar. Simulating that trade would book an "exit at the stop" above
    the entry price -- i.e. free money out of a losing bracket. Such entries are
    skipped and counted in ``.attrs["skipped_invalid_stop"]``.

This module never reads the ``tint`` column (SPEC 3.3 and build rule 9): trade
the dot, not the colour. It never sources exit ATR from vidya.py's ATR(200)
(SPEC 3.5): the exit ATR is computed locally at ``exit_model.atr_length``.

OPEN QUESTIONS (ambiguities in SPEC 3.5, resolved by the literal wording plus the
most conservative available reading; each is flagged in-line with ``# VERIFY:``
where the user can settle it against his chart or his own rules):

  Q1  ``simulate`` has no VIDYA frame in its frozen signature, yet
      ``ExitModel.exit_on_ribbon_flip`` needs the trend series. Resolved with an
      ADDITIVE keyword-only parameter ``vd=None``; every spec-conformant
      positional call site is unaffected. When it is None and a ribbon-flip exit
      is requested, this module calls ``vidya.compute(bars)`` with DEFAULT
      VidyaParams -- which will silently disagree with ``run_once`` if a
      non-default ``vd_params`` was used. ``run_once`` therefore always passes
      ``vd`` explicitly. Callers using the ``beyond0.5_flip`` exit with custom
      VIDYA parameters must pass ``vd`` too.
  Q2  ``exit_reason`` for an ``atr_trail`` fill. The spec says the trail exit is
      ``"target"``, but the trail is seeded at the initial stop, so a first-bar
      loss would be recorded as a target hit and ``pct_stopped`` would read 0 for
      the entire trail family. Resolved: a fill at a level that has never
      ratcheted off the initial stop is ``"stop"``; a fill at a ratcheted level is
      ``"target"``.
  Q3  Precedence of two close-priced exits on the same bar: ribbon_flip is
      checked before the time stop. Both fill at the same price (that bar's
      close), so this only changes ``exit_reason``, never P&L.
  Q4  Re-entry on the exit bar. A position occupies ``[entry_ts, exit_ts]``; a new
      entry whose ``entry_ts == exit_ts`` is ACCEPTED, since both fill at the same
      close and the book is flat at that instant. Entries strictly inside an open
      holding period are discarded, never queued (SPEC 3.5).
  Q5  ``g2_ribbon``/``g3_delta`` are reported as RAW gate readings, independent of
      ``use_ribbon``/``use_delta``, so the ``--verify`` CSV shows what the gate
      actually saw. The enable flags are applied only when folding ``gates_pass``.
      Both are False on bars with no dot, there being no side to test.
  Q6  ``target_price`` is NaN for ``target_kind == "atr_trail"`` as well as
      ``"none"``: neither has a fixed level. The spec names only ``"none"``.
  Q7  WARMUP. SPEC 3.7 assigns the 400-bar warmup exclusion to
      ``ablation.run_grid``/``render_report``, and SPEC 3.8 forbids importing
      ablation.py, so this module does NOT slice warmup bars -- doing so would
      also break the ``index.equals`` contract on ``build_signals``. Gate-bearing
      arms self-censor during warmup (``trend == 0`` and ``delta_pct`` NaN never
      pass), but arm A4 ``dot_only`` WILL generate trades from roughly bar 25 if
      it is handed un-sliced bars. Callers must slice.
  Q8  MAE/MFE include the full range of the exit bar, though the position closed
      somewhere inside it. This is the standard gross-excursion convention and it
      overstates rather than flatters.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pandas as pd

from . import data
from . import two_pole
from . import vidya

__all__ = [
    "StrategyParams",
    "ExitModel",
    "TRADE_COLUMNS",
    "SIGNAL_COLUMNS",
    "build_signals",
    "resolve_entries",
    "simulate",
    "run_once",
]


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StrategyParams:
    """Gate, timing and position parameters. SPEC 3.5, verbatim."""

    # --- gates ---
    quadrant_threshold: float = 0.5    # LONG needs osc <= -x ; SHORT needs osc >= +x
    delta_threshold: float = 20.0      # LONG needs delta_pct >= +x ; SHORT <= -x
                                       # SWEPT, never a constant -- see U-1
    use_quadrant: bool = True          # G1 magnitude test (the dot is always required)
    use_ribbon: bool = True            # G2
    use_delta: bool = True             # G3
    invert_ribbon: bool = False        # A5 arm: require ribbon to OPPOSE the trade

    # --- timing ---
    entry_offset_bars: int = 1         # "wait 2 closed candles, signal bar counts as one"
                                       #  -> dot on bar N, entry at CLOSE of bar N+1
    max_signal_age_bars: int = 3       # dead if strictly older than this
    zero_cross_kills: bool = True      # dead if osc crossed zero since the dot

    # --- position ---
    allow_pyramiding: bool = False     # one open position at a time
    allow_shorts: bool = True
    fee_bps: float = 10.0              # 0.10% per side, taker
    slippage_bps: float = 5.0          # 0.05% per side, applied against the position


@dataclasses.dataclass(frozen=True)
class ExitModel:
    """One exit configuration. SPEC 3.5 and the grid of SPEC 5.3, verbatim."""

    exit_id: str                       # stable identifier, e.g. "beyond0.5_R2_t42"
    stop_kind: str                     # "fixed_pct" | "atr_from_entry" | "atr_beyond_extreme"
    stop_param: float
    target_kind: str                   # "fixed_pct" | "r_multiple" | "atr_trail" | "none"
    target_param: float
    time_stop_bars: int | None = 42    # 42 bars of 4h = 7 days ("roughly a one-week hold")
    exit_on_ribbon_flip: bool = False  # close if VIDYA trend flips against the position
    atr_length: int = 14               # ATR used by exits; INDEPENDENT of VIDYA's ATR(200)


_STOP_KINDS = ("fixed_pct", "atr_from_entry", "atr_beyond_extreme")
_TARGET_KINDS = ("fixed_pct", "r_multiple", "atr_trail", "none")

SIGNAL_COLUMNS: tuple[str, ...] = (
    "dot_side",
    "osc",
    "delta_pct",
    "trend",
    "g1_quadrant",
    "g2_ribbon",
    "g3_delta",
    "gates_pass",
    "signal_side",
)

TRADE_COLUMNS: dict[str, str] = {
    "trade_id": "int64",
    "side": "object",                       # "long" | "short"
    "signal_ts": "datetime64[ns, UTC]",
    "entry_ts": "datetime64[ns, UTC]",
    "exit_ts": "datetime64[ns, UTC]",
    "entry_price": "float64",               # bar close at entry_ts, BEFORE costs
    "exit_price": "float64",                # BEFORE costs
    "stop_price": "float64",
    "target_price": "float64",              # NaN when target_kind is "none"/"atr_trail"
    "exit_reason": "object",                # "stop"|"target"|"time"|"ribbon_flip"|"end_of_data"
    "bars_held": "int64",
    "return_pct": "float64",                # NET of fees+slippage, signed for direction
    "r_multiple": "float64",                # net return / |entry-stop| risk, in R
    "mae_pct": "float64",                   # max adverse excursion, GROSS, positive
    "mfe_pct": "float64",                   # max favourable excursion, GROSS, positive
    "signal_osc": "float64",
    "signal_delta_pct": "float64",
    "signal_trend": "int8",
}


# ---------------------------------------------------------------------------
# Local Wilder ATR (SPEC 3.5: must NOT come from vidya.py)
# ---------------------------------------------------------------------------


def true_range(bars: pd.DataFrame) -> pd.Series:
    """Pine ``ta.tr(true)`` -- the true range of every bar.

    Returns a float64 Series whose index ``.equals(bars.index)``. The first bar
    has no previous close, so its true range is ``high - low`` (Pine's
    ``handle_na=true`` behaviour).
    """
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")

    prev_close = np.empty_like(close)
    prev_close[0] = np.nan
    prev_close[1:] = close[:-1]

    # Pine: max(high - low, abs(high - close[1]), abs(low - close[1]))
    tr = np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
    )
    tr[0] = high[0] - low[0]
    return pd.Series(tr, index=bars.index, dtype="float64", name="tr")


def wilder_atr(bars: pd.DataFrame, length: int) -> pd.Series:
    """Wilder's ATR, matching Pine ``ta.atr(length)``.

    Pine's ``ta.rma`` seeds with the simple mean of the first ``length`` true
    ranges and then applies ``rma[i] = (rma[i-1]*(length-1) + tr[i]) / length``,
    which is an EMA with ``alpha = 1/length``.

    Returns a float64 Series, index ``.equals(bars.index)``, NaN for the first
    ``length - 1`` bars.

    # VERIFY: pandas' ``ewm(alpha=1/length, adjust=False)`` seeds on the FIRST
    # value, not on an SMA of the first ``length`` values, and would drift from
    # TradingView for hundreds of bars. This SMA-seeded recursion is the Pine
    # behaviour. Check one ATR(14) value in the --verify CSV against a
    # TradingView ATR(14) plot on the same 4h candle before trusting any
    # ATR-based stop distance.
    """
    if length < 1:
        raise ValueError(f"atr_length must be >= 1, got {length}")

    tr = true_range(bars).to_numpy(dtype="float64")
    n = tr.shape[0]
    out = np.full(n, np.nan, dtype="float64")
    if n < length:
        return pd.Series(out, index=bars.index, dtype="float64", name="atr")

    out[length - 1] = float(np.mean(tr[:length]))
    for i in range(length, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return pd.Series(out, index=bars.index, dtype="float64", name="atr")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _check_aligned(name: str, frame: pd.DataFrame, bars: pd.DataFrame) -> None:
    """Raise ValueError unless ``frame.index`` equals ``bars.index`` exactly."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a DataFrame, got {type(frame)!r}")
    if not frame.index.equals(bars.index):
        raise ValueError(
            f"{name}.index must be .equals() to bars.index "
            f"(len {len(frame)} vs {len(bars)}); no reindexing is permitted"
        )


def build_signals(
    bars: pd.DataFrame,
    tp: pd.DataFrame,
    vd: pd.DataFrame,
    params: StrategyParams = StrategyParams(),
) -> pd.DataFrame:
    """Join the indicator frames and apply the three gates. SPEC 3.5.

    Parameters
    ----------
    bars : 4h BarFrame.
    tp   : ``two_pole.compute`` output, index equal to ``bars.index``.
    vd   : ``vidya.compute`` output, index equal to ``bars.index``.

    Returns
    -------
    DataFrame, index ``.equals(bars.index)``, columns exactly and in this order:
      dot_side     object   "long" | "short" | None   (raw dot, no gates)
      osc          float64  copied through, for the report
      delta_pct    float64  copied through
      trend        int8     copied through
      g1_quadrant  bool     dot present AND magnitude test passes
      g2_ribbon    bool     trend matches (or, if invert_ribbon, opposes) dot_side
      g3_delta     bool     delta test passes
      gates_pass   bool     all ENABLED gates pass (a disabled gate is True)
      signal_side  object   "long"|"short" if gates_pass else None

    Gates (all three required to take a trade):
      G1 QUADRANT: long  = dot_long  and (not use_quadrant or osc <= -quadrant_threshold)
                   short = dot_short and (not use_quadrant or osc >= +quadrant_threshold)
      G2 RIBBON:   long needs trend == +1, short needs trend == -1; invert_ribbon
                   flips the required sign; trend == 0 NEVER passes.
      G3 DELTA:    long needs delta_pct >= +delta_threshold, short needs
                   delta_pct <= -delta_threshold; NaN NEVER passes.

    The DOT is always required. ``use_quadrant=False`` relaxes only the MAGNITUDE
    test, it does not admit non-dot bars.

    This function does not read ``tp["tint"]`` and must never be changed to.
    """
    data.validate_bars(bars)
    _check_aligned("tp", tp, bars)
    _check_aligned("vd", vd, bars)

    for col in ("osc", "dot_long", "dot_short"):
        if col not in tp.columns:
            raise ValueError(f"tp is missing required column {col!r}")
    for col in ("trend", "delta_pct"):
        if col not in vd.columns:
            raise ValueError(f"vd is missing required column {col!r}")

    osc = tp["osc"].to_numpy(dtype="float64")
    dot_long = tp["dot_long"].to_numpy(dtype="bool")
    dot_short = tp["dot_short"].to_numpy(dtype="bool")
    trend = vd["trend"].to_numpy(dtype="int8")
    delta = vd["delta_pct"].to_numpy(dtype="float64")

    if np.any(dot_long & dot_short):
        raise ValueError("two_pole produced dot_long and dot_short on the same bar")

    # --- G1 QUADRANT ------------------------------------------------------
    # The dot is the signal event and is always required; use_quadrant toggles
    # only the magnitude test, which is equivalent to quadrant_threshold = 0.
    if params.use_quadrant:
        qt = float(params.quadrant_threshold)
        # NaN comparisons are False, so a NaN osc can never pass a gate.
        g1_long = dot_long & (osc <= -qt)
        g1_short = dot_short & (osc >= qt)
    else:
        g1_long = dot_long.copy()
        g1_short = dot_short.copy()
    g1 = g1_long | g1_short

    # --- G2 RIBBON --------------------------------------------------------
    # trend == 0 (before the first VIDYA band break) never satisfies either sign,
    # so warmup bars are rejected automatically.
    req_long = -1 if params.invert_ribbon else 1
    req_short = 1 if params.invert_ribbon else -1
    g2 = (dot_long & (trend == req_long)) | (dot_short & (trend == req_short))

    # --- G3 DELTA ---------------------------------------------------------
    dt = float(params.delta_threshold)
    # delta_pct is CUMULATIVE OVER THE TREND LEG (SPEC 2.3). NaN => False.
    g3 = (dot_long & (delta >= dt)) | (dot_short & (delta <= -dt))

    # --- fold ------------------------------------------------------------
    gates_pass = g1.copy()
    if params.use_ribbon:
        gates_pass &= g2
    if params.use_delta:
        gates_pass &= g3

    dot_side = np.full(len(bars), None, dtype=object)
    dot_side[dot_long] = "long"
    dot_side[dot_short] = "short"

    signal_side = np.full(len(bars), None, dtype=object)
    signal_side[gates_pass] = dot_side[gates_pass]

    out = pd.DataFrame(
        {
            "dot_side": pd.Series(dot_side, index=bars.index, dtype="object"),
            "osc": pd.Series(osc, index=bars.index, dtype="float64"),
            "delta_pct": pd.Series(delta, index=bars.index, dtype="float64"),
            "trend": pd.Series(trend, index=bars.index, dtype="int8"),
            "g1_quadrant": pd.Series(g1, index=bars.index, dtype="bool"),
            "g2_ribbon": pd.Series(g2, index=bars.index, dtype="bool"),
            "g3_delta": pd.Series(g3, index=bars.index, dtype="bool"),
            "gates_pass": pd.Series(gates_pass, index=bars.index, dtype="bool"),
            "signal_side": pd.Series(signal_side, index=bars.index, dtype="object"),
        }
    )
    return out[list(SIGNAL_COLUMNS)]


# ---------------------------------------------------------------------------
# Entry timing and staleness
# ---------------------------------------------------------------------------


def resolve_entries(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    params: StrategyParams = StrategyParams(),
) -> list[dict]:
    """Apply entry timing and staleness. SPEC 3.5.

    Returns a chronologically ordered list of pending-entry dicts, one per signal
    that survives::

        {"signal_ts": pd.Timestamp,      # bar whose CLOSE printed the dot
         "entry_ts":  pd.Timestamp,      # signal_ts + entry_offset_bars bars
         "side":      "long"|"short",
         "signal_osc":       float,
         "signal_high":      float,      # high of the SIGNAL bar (for short stops)
         "signal_low":       float,      # low  of the SIGNAL bar (for long stops)
         "signal_delta_pct": float,
         "signal_trend":     int}

    ENTRY TIMING -- the rule is "wait 2 closed candles after the signal, where THE
    SIGNAL BAR COUNTS AS ONE", so a dot confirmed at the close of bar N enters at
    the CLOSE of bar N+1 and ``entry_offset_bars = 1`` encodes that.

    # VERIFY: U-4. The alternative reading is N+2. It is not chart-verifiable --
    # it is a rules question the user must answer from how he actually traded.
    # It is swept over {1, 2}; if the two produce materially different expectancy
    # that difference is itself a fragility warning about the signal.

    STALENESS -- the pending entry is dropped if, at any bar in
    ``(signal_ts, entry_ts]``:
      * the bar's age exceeds ``max_signal_age_bars`` (the signal bar is age 0), or
      * ``zero_cross_kills`` and ``sign(osc)`` differs from ``sign(osc)`` at
        ``signal_ts``.
    It is also dropped when ``entry_ts`` falls past the end of ``bars``.

    A NaN oscillator anywhere in the staleness window counts as a sign change and
    kills the signal: an unknown oscillator is not evidence that the setup is
    still alive.

    # VERIFY: U-7. Staleness is implemented as ``age > max_signal_age_bars``.
    # With entry_offset_bars = 1 the maximum age reached is 1, so the rule almost
    # never binds; it only starts mattering when the entry offset is raised.
    """
    data.validate_bars(bars)
    _check_aligned("signals", signals, bars)

    offset = int(params.entry_offset_bars)
    if offset < 0:
        raise ValueError(f"entry_offset_bars must be >= 0, got {offset}")

    index = bars.index
    n = len(index)

    osc = signals["osc"].to_numpy(dtype="float64")
    delta = signals["delta_pct"].to_numpy(dtype="float64")
    trend = signals["trend"].to_numpy(dtype="int8")
    side_arr = signals["signal_side"].to_numpy(dtype=object)
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")

    # Sign of the oscillator at every bar; NaN stays NaN and never compares equal,
    # which is exactly the conservative behaviour we want in the window scan.
    osc_sign = np.sign(osc)

    entries: list[dict] = []
    for i in range(n):
        side = side_arr[i]
        if side is None:
            continue

        entry_idx = i + offset
        if entry_idx >= n:
            # The entry bar has not printed yet -- drop, never extrapolate.
            continue

        # Age of bar j relative to the signal bar is (j - i); the window
        # (signal_ts, entry_ts] therefore spans ages 1..offset, whose maximum
        # age is `offset` itself.
        if offset > int(params.max_signal_age_bars):
            continue

        if params.zero_cross_kills and entry_idx > i:
            window = osc_sign[i + 1 : entry_idx + 1]
            # NaN != anything -> True -> killed. Deliberate.
            if bool(np.any(window != osc_sign[i])):
                continue

        entries.append(
            {
                "signal_ts": index[i],
                "entry_ts": index[entry_idx],
                "side": str(side),
                "signal_osc": float(osc[i]),
                "signal_high": float(high[i]),
                "signal_low": float(low[i]),
                "signal_delta_pct": float(delta[i]),
                "signal_trend": int(trend[i]),
            }
        )

    entries.sort(key=lambda e: (e["entry_ts"], e["signal_ts"]))
    return entries


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def _empty_trades() -> pd.DataFrame:
    """A zero-row trades frame with exactly TRADE_COLUMNS and their dtypes."""
    frame = pd.DataFrame(
        {col: pd.Series([], dtype=dtype) for col, dtype in TRADE_COLUMNS.items()}
    )
    return frame[list(TRADE_COLUMNS)]


def _finalise_trades(records: list[dict]) -> pd.DataFrame:
    """Coerce trade records to the frozen TRADE_COLUMNS schema."""
    if not records:
        return _empty_trades()

    frame = pd.DataFrame.from_records(records)
    for col in TRADE_COLUMNS:
        if col not in frame.columns:
            frame[col] = np.nan
    frame = frame[list(TRADE_COLUMNS)]
    frame = frame.sort_values("entry_ts", kind="mergesort").reset_index(drop=True)
    frame["trade_id"] = np.arange(len(frame), dtype="int64")
    for col, dtype in TRADE_COLUMNS.items():
        frame[col] = frame[col].astype(dtype)
    return frame


def _side_sign(side: str) -> int:
    """+1 for a long, -1 for a short."""
    if side == "long":
        return 1
    if side == "short":
        return -1
    raise ValueError(f"side must be 'long' or 'short', got {side!r}")


def _resolve_stop_price(
    side: str,
    entry_price: float,
    signal_high: float,
    signal_low: float,
    atr_at_entry: float,
    exit_model: ExitModel,
) -> float:
    """Initial stop level. SPEC 3.5 STOP PLACEMENT.

    fixed_pct           long  entry*(1 - p)          short entry*(1 + p)
                        [p is a FRACTION: 0.016 == 1.6%]
    atr_from_entry      long  entry - p*ATR          short entry + p*ATR
    atr_beyond_extreme  long  signal_low - p*ATR     short signal_high + p*ATR

    ``atr_beyond_extreme`` is the hypothesis under test: the one live trade was
    stopped at 76,233 -- the exact low of a sweep -- and price then ran through
    its take-profit to 82,272.

    # VERIFY: the extreme used is the SIGNAL bar's, not the entry bar's. With
    # entry_offset_bars = 1 the entry bar is the bar after the dot and may well
    # print a lower low; a stop beyond the ENTRY bar's extreme is a different
    # (untested) hypothesis. Confirm which extreme the user means by "beyond the
    # signal bar's extreme" before reading the beyond* family as settled.
    """
    sign = _side_sign(side)
    p = float(exit_model.stop_param)

    if exit_model.stop_kind == "fixed_pct":
        return entry_price * (1.0 - sign * p)
    if exit_model.stop_kind == "atr_from_entry":
        return entry_price - sign * p * atr_at_entry
    if exit_model.stop_kind == "atr_beyond_extreme":
        extreme = signal_low if sign > 0 else signal_high
        return extreme - sign * p * atr_at_entry
    raise ValueError(
        f"unknown stop_kind {exit_model.stop_kind!r}; expected one of {_STOP_KINDS}"
    )


def _resolve_target_price(
    side: str,
    entry_price: float,
    stop_price: float,
    exit_model: ExitModel,
) -> float:
    """Initial target level, or NaN when the model has no fixed target.

    fixed_pct   long entry*(1 + p)                short entry*(1 - p)
    r_multiple  risk = abs(entry - stop); long entry + p*risk, short entry - p*risk
    atr_trail   NaN -- the chandelier trail replaces the fixed target
    none        NaN
    """
    sign = _side_sign(side)
    p = float(exit_model.target_param)

    if exit_model.target_kind == "fixed_pct":
        return entry_price * (1.0 + sign * p)
    if exit_model.target_kind == "r_multiple":
        risk = abs(entry_price - stop_price)
        return entry_price + sign * p * risk
    if exit_model.target_kind in ("atr_trail", "none"):
        return float("nan")
    raise ValueError(
        f"unknown target_kind {exit_model.target_kind!r}; expected one of {_TARGET_KINDS}"
    )


def simulate(
    bars: pd.DataFrame,
    entries: list[dict],
    exit_model: ExitModel,
    params: StrategyParams = StrategyParams(),
    *,
    vd: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Bar-level position simulation. SPEC 3.5.

    Returns a trades DataFrame with EXACTLY ``TRADE_COLUMNS`` in that order and
    those dtypes, a RangeIndex 0..n-1, sorted by ``entry_ts``. An empty result is
    a zero-row DataFrame with the same columns and dtypes -- never None, never a
    bare ``DataFrame()``.

    ``vd`` is an ADDITIVE keyword-only argument (see OPEN QUESTION Q1): the frozen
    signature carries no VIDYA frame, but ``exit_on_ribbon_flip`` needs the trend
    series. Spec-conformant positional calls are unaffected. When a ribbon-flip
    exit is requested and ``vd`` is None, VIDYA is computed here with DEFAULT
    parameters.

    EXECUTION MODEL (fixed, not a parameter):
      * Entry fills at ``bars.close[entry_ts]``. Price columns carry raw prices;
        ``fee_bps + slippage_bps`` are charged to ``return_pct`` at BOTH entry and
        exit, i.e. ``2 * (fee + slip)`` bps in total.
      * From the bar AFTER ``entry_ts`` onward each bar is checked intrabar: stop
        first, then target. If one bar's range contains BOTH, the STOP is assumed
        to have filled.
      * Stop and target fill AT their level (no gap modelling), except when the
        bar OPENS beyond the level, in which case they fill at the open.
      * Time stop and ribbon-flip exits fill at that bar's CLOSE.
      * Open positions at the end of the data are closed at the final bar's close
        and marked ``"end_of_data"``.
      * With ``allow_pyramiding=False``, entries arriving while a position is open
        are DISCARDED (not queued) and counted in ``.attrs["discarded_entries"]``.

    Additional counters on ``.attrs`` (diagnostics, not part of the frozen
    schema): ``skipped_shorts``, ``skipped_nan_atr``, ``skipped_invalid_stop``.

    ATR is Wilder's over ``exit_model.atr_length``, computed inside this module
    from ``bars``. It is NOT the VIDYA ATR(200).
    """
    data.validate_bars(bars)

    if exit_model.stop_kind not in _STOP_KINDS:
        raise ValueError(f"unknown stop_kind {exit_model.stop_kind!r}")
    if exit_model.target_kind not in _TARGET_KINDS:
        raise ValueError(f"unknown target_kind {exit_model.target_kind!r}")

    index = bars.index
    n = len(index)
    open_ = bars["open"].to_numpy(dtype="float64")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")

    atr = wilder_atr(bars, exit_model.atr_length).to_numpy(dtype="float64")

    # --- VIDYA trend, only if a ribbon-flip exit needs it (OPEN QUESTION Q1) ---
    trend_arr: np.ndarray | None = None
    if exit_model.exit_on_ribbon_flip:
        if vd is None:
            vd = vidya.compute(bars)
        _check_aligned("vd", vd, bars)
        trend_arr = vd["trend"].to_numpy(dtype="int8")

    # Round-turn cost in PERCENT units: bps/100 gives percent, x2 for both sides.
    cost_pct = 2.0 * (float(params.fee_bps) + float(params.slippage_bps)) / 100.0

    pos_of_ts = {ts: i for i, ts in enumerate(index)}

    discarded = 0
    skipped_shorts = 0
    skipped_nan_atr = 0
    skipped_invalid_stop = 0

    records: list[dict] = []
    # Bar index at which the currently open position releases the book. A new
    # entry at exactly this bar is permitted (OPEN QUESTION Q4).
    busy_until_idx = -1

    ordered = sorted(entries, key=lambda e: (e["entry_ts"], e["signal_ts"]))

    for entry in ordered:
        side = str(entry["side"])
        if side == "short" and not params.allow_shorts:
            skipped_shorts += 1
            continue

        entry_ts = entry["entry_ts"]
        entry_idx = pos_of_ts.get(entry_ts)
        if entry_idx is None:
            # entry_ts is not a bar in this frame -- refuse to guess a fill.
            continue

        if not params.allow_pyramiding and entry_idx < busy_until_idx:
            discarded += 1
            continue

        sign = _side_sign(side)
        entry_price = float(close[entry_idx])
        atr_at_entry = float(atr[entry_idx])

        needs_atr = exit_model.stop_kind in ("atr_from_entry", "atr_beyond_extreme") or (
            exit_model.target_kind == "atr_trail"
        )
        if needs_atr and not np.isfinite(atr_at_entry):
            # Inside the ATR warmup: the stop distance is undefined. Refusing the
            # trade is the only honest option -- a fabricated stop would bias the
            # beyond* family precisely where it is being tested.
            skipped_nan_atr += 1
            continue

        stop_price = _resolve_stop_price(
            side,
            entry_price,
            float(entry["signal_high"]),
            float(entry["signal_low"]),
            atr_at_entry,
            exit_model,
        )

        # ANTI-EDGE-MANUFACTURING GUARD. A long's stop must sit BELOW the entry and
        # a short's ABOVE it. atr_beyond_extreme can violate this when price moved
        # hard between the signal bar and the entry bar; simulating it would book
        # a "stop loss" at a profit. Refuse the trade instead.
        if not np.isfinite(stop_price) or sign * (entry_price - stop_price) <= 0.0:
            skipped_invalid_stop += 1
            continue

        target_price = _resolve_target_price(side, entry_price, stop_price, exit_model)

        risk_abs = abs(entry_price - stop_price)
        risk_pct = risk_abs / entry_price * 100.0

        # --- trailing state -------------------------------------------------
        trailing = exit_model.target_kind == "atr_trail"
        # The running extreme starts at the ENTRY PRICE, not the entry bar's high:
        # that high printed before we were filled and is not ours to trail from.
        run_extreme = entry_price
        trail_level = stop_price
        trail_has_ratcheted = False

        # --- excursion state ------------------------------------------------
        run_min_low = np.inf
        run_max_high = -np.inf

        exit_idx: int | None = None
        exit_price = float("nan")
        exit_reason = ""

        for i in range(entry_idx + 1, n):
            # Excursions cover every bar the position was open, including the bar
            # it closed on (OPEN QUESTION Q8).
            run_min_low = min(run_min_low, float(low[i]))
            run_max_high = max(run_max_high, float(high[i]))

            # The level in force during bar i was fixed at the close of bar i-1.
            active_stop = trail_level if trailing else stop_price

            # --- 1. STOP (checked first; see the module docstring) -----------
            stop_hit = False
            stop_fill = float("nan")
            if sign > 0:
                if open_[i] <= active_stop:
                    stop_hit, stop_fill = True, float(open_[i])   # gapped through
                elif low[i] <= active_stop:
                    stop_hit, stop_fill = True, float(active_stop)
            else:
                if open_[i] >= active_stop:
                    stop_hit, stop_fill = True, float(open_[i])
                elif high[i] >= active_stop:
                    stop_hit, stop_fill = True, float(active_stop)

            if stop_hit:
                exit_idx = i
                exit_price = stop_fill
                # OPEN QUESTION Q2: a trail that never ratcheted is still the
                # original stop, and must not be reported as a target hit.
                exit_reason = "target" if (trailing and trail_has_ratcheted) else "stop"
                break

            # --- 2. TARGET ---------------------------------------------------
            # Reached only when the stop did NOT trigger on this bar. If a bar
            # contains both, the stop wins.
            #
            # # VERIFY: this is pessimistic in one specific case -- a bar whose
            # # OPEN is already beyond the target and whose LOW later reaches the
            # # stop. Chronologically the target filled first at the open, but the
            # # spec's stop-first rule is absolute, so that trade is booked as a
            # # loss here. Rare on 4h BTC; it understates the beyond* family
            # # slightly rather than flattering it.
            if np.isfinite(target_price):
                target_hit = False
                target_fill = float("nan")
                if sign > 0:
                    if open_[i] >= target_price:
                        target_hit, target_fill = True, float(open_[i])
                    elif high[i] >= target_price:
                        target_hit, target_fill = True, float(target_price)
                else:
                    if open_[i] <= target_price:
                        target_hit, target_fill = True, float(open_[i])
                    elif low[i] <= target_price:
                        target_hit, target_fill = True, float(target_price)

                if target_hit:
                    exit_idx = i
                    exit_price = target_fill
                    exit_reason = "target"
                    break

            # --- 3. RIBBON FLIP (fills at this bar's close) ------------------
            if trend_arr is not None and int(trend_arr[i]) == -sign:
                exit_idx = i
                exit_price = float(close[i])
                exit_reason = "ribbon_flip"
                break

            # --- 4. TIME STOP (fills at this bar's close) --------------------
            if exit_model.time_stop_bars is not None and (
                i - entry_idx
            ) >= int(exit_model.time_stop_bars):
                exit_idx = i
                exit_price = float(close[i])
                exit_reason = "time"
                break

            # --- 5. RATCHET THE TRAIL FOR THE NEXT BAR -----------------------
            # Chandelier exit: (running extreme since entry) -/+ p * ATR, updated
            # at the close of bar i using only data through bar i, and never
            # loosened. The level computed here governs bar i+1.
            if trailing:
                if sign > 0:
                    run_extreme = max(run_extreme, float(high[i]))
                else:
                    run_extreme = min(run_extreme, float(low[i]))
                atr_i = float(atr[i])
                if np.isfinite(atr_i):
                    candidate = run_extreme - sign * float(exit_model.target_param) * atr_i
                    # Never loosen: a long's trail only rises, a short's only falls.
                    if sign * (candidate - trail_level) > 0.0:
                        trail_level = candidate
                        trail_has_ratcheted = True

        if exit_idx is None:
            # Force-closed at the end of the data. ablation.py reports these
            # separately and must not let them dominate a cell.
            exit_idx = n - 1
            exit_price = float(close[exit_idx])
            exit_reason = "end_of_data"
            if exit_idx > entry_idx:
                run_min_low = min(run_min_low, float(np.min(low[entry_idx + 1 :])))
                run_max_high = max(run_max_high, float(np.max(high[entry_idx + 1 :])))

        if not np.isfinite(run_min_low):
            run_min_low = entry_price
        if not np.isfinite(run_max_high):
            run_max_high = entry_price

        # --- P&L -------------------------------------------------------------
        gross_pct = sign * (exit_price - entry_price) / entry_price * 100.0
        return_pct = gross_pct - cost_pct
        r_multiple = return_pct / risk_pct if risk_pct > 0.0 else float("nan")

        if sign > 0:
            mae_pct = max(0.0, (entry_price - run_min_low) / entry_price * 100.0)
            mfe_pct = max(0.0, (run_max_high - entry_price) / entry_price * 100.0)
        else:
            mae_pct = max(0.0, (run_max_high - entry_price) / entry_price * 100.0)
            mfe_pct = max(0.0, (entry_price - run_min_low) / entry_price * 100.0)

        records.append(
            {
                "trade_id": 0,  # rewritten in _finalise_trades
                "side": side,
                "signal_ts": entry["signal_ts"],
                "entry_ts": entry_ts,
                "exit_ts": index[exit_idx],
                "entry_price": entry_price,
                "exit_price": float(exit_price),
                "stop_price": float(stop_price),
                "target_price": float(target_price),
                "exit_reason": exit_reason,
                "bars_held": int(exit_idx - entry_idx),
                "return_pct": float(return_pct),
                "r_multiple": float(r_multiple),
                "mae_pct": float(mae_pct),
                "mfe_pct": float(mfe_pct),
                "signal_osc": float(entry["signal_osc"]),
                "signal_delta_pct": float(entry["signal_delta_pct"]),
                "signal_trend": int(entry["signal_trend"]),
            }
        )

        if not params.allow_pyramiding:
            busy_until_idx = exit_idx

    trades = _finalise_trades(records)
    trades.attrs["discarded_entries"] = int(discarded)
    trades.attrs["skipped_shorts"] = int(skipped_shorts)
    trades.attrs["skipped_nan_atr"] = int(skipped_nan_atr)
    trades.attrs["skipped_invalid_stop"] = int(skipped_invalid_stop)
    return trades


def run_once(
    bars: pd.DataFrame,
    exit_model: ExitModel,
    params: StrategyParams = StrategyParams(),
    tp_params: "two_pole.TwoPoleParams | None" = None,
    vd_params: "vidya.VidyaParams | None" = None,
) -> pd.DataFrame:
    """two_pole.compute -> vidya.compute -> build_signals -> resolve_entries ->
    simulate. Returns the trades frame. SPEC 3.5.

    ``tp_params``/``vd_params`` default to ``None``, which is read as "the
    dataclass default" (the spec writes ``...`` for these defaults).

    The VIDYA frame is passed through to ``simulate`` so that a ribbon-flip exit
    uses the SAME VIDYA parameterisation as the gates -- see OPEN QUESTION Q1.

    Warmup is NOT sliced here: SPEC 3.7 makes that ``ablation.run_grid``'s job,
    and SPEC 3.8 forbids importing ablation.py. See OPEN QUESTION Q7.
    """
    data.validate_bars(bars)

    tp_p = two_pole.TwoPoleParams() if tp_params is None else tp_params
    vd_p = vidya.VidyaParams() if vd_params is None else vd_params

    tp = two_pole.compute(bars, tp_p)
    vd = vidya.compute(bars, vd_p)
    signals = build_signals(bars, tp, vd, params)
    entries = resolve_entries(bars, signals, params)
    return simulate(bars, entries, exit_model, params, vd=vd)
