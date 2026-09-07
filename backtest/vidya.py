"""Volumatic VIDYA [BigBeluga] -- VIDYA line, ATR bands, discrete trend flip, and
cumulative-per-leg Delta Volume.

Port of the BigBeluga "Volumatic Variable Index Dynamic Average" Pine indicator at
the user's chart settings 34 / 20 / 2 / close, with ta.atr(200) bands.

The flip timestamps are the load-bearing output of this module: they define the
trend legs, and the trend legs define the Delta Volume accumulator. A flip that is
one bar early or late silently rewrites every delta_pct downstream of it.

See SPEC.md sections 2.2, 2.3 and 3.4. This module implements 3.4 exactly.

OPEN QUESTIONS:
  U-3 (P1, high leverage) -- trend flip rule. Default `trend_rule="band"`: the trend
      flips when close closes beyond the ATR band (upper/lower). The alternative,
      `trend_rule="line"`, flips on close vs the VIDYA line itself. SPEC.md 2.2
      resolves this to "band" because band_mult=2 and atr_length=200 are otherwise
      unused parameters. This choice shifts every leg_id and therefore every
      delta_pct, so it must be checked against the chart. See the VERIFY note on
      `trend_state`.

  Leg 0 delta masking -- SPEC.md 2.3 gives an accumulation formula that starts at
      bar 0, while SPEC.md 3.4 states "delta_pct is NaN until the first band break".
      These conflict for the bars before the first flip (leg_id == 0). This module
      follows 3.4 and masks delta_pct to NaN on leg 0, because a leg-0 accumulation
      measures nothing but where the data happens to start. buy_vol and sell_vol are
      left raw and unmasked on leg 0 so the accumulator is still visible in the
      --verify CSV. This is immaterial to trading: leg 0 always ends far inside the
      400-bar warmup, and trend == 0 there fails gate G2 regardless.

  Pine `ta.rma` seeding -- Pine seeds RMA with an SMA of the first `length` values and
      then recurses. That is implemented here, and it is NOT the same as
      pandas `ewm(alpha=1/n, adjust=False)`. See the VERIFY note on `wilder_atr`.

NO NETWORK. NO DISK. NO PRINTING. Import has no side effects.
"""

from __future__ import annotations

import dataclasses
from typing import Final

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------
# data.py dependency (SPEC.md 3.8: vidya.py -> data.py)
#
# data.py is authored in parallel by another builder. The import is attempted both
# relative and absolute so this module works as `backtest.vidya` and as a plain
# script import. If data.py is not on disk yet, a local validator enforcing the
# SPEC.md 3.1 invariants is used so that this module remains importable and still
# honours build rule 5 ("call validate_bars on entry"). data.validate_bars is always
# preferred when available; the fallback is a scaffold, not a second source of truth.
# --------------------------------------------------------------------------------
try:  # pragma: no cover - import plumbing
    from . import data as _data  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    try:
        from backtest import data as _data  # type: ignore[no-redef]
    except ImportError:
        _data = None  # type: ignore[assignment]

_BAR_COLUMNS: Final[list[str]] = ["open", "high", "low", "close", "volume"]

#: Columns of ``compute`` output, in order. Frozen by SPEC.md 3.4.
VIDYA_COLUMNS: Final[list[str]] = [
    "vidya", "atr", "upper", "lower",
    "trend", "trend_flip", "leg_id",
    "buy_vol", "sell_vol", "delta_pct",
]

VIDYA_DTYPES: Final[dict[str, str]] = {
    "vidya": "float64", "atr": "float64", "upper": "float64", "lower": "float64",
    "trend": "int8", "trend_flip": "bool", "leg_id": "int64",
    "buy_vol": "float64", "sell_vol": "float64", "delta_pct": "float64",
}


@dataclasses.dataclass(frozen=True)
class VidyaParams:
    """Volumatic VIDYA parameters. Defaults are the user's chart: 34 / 20 / 2 / close.

    Attributes:
        vidya_length: VIDYA smoothing length. alpha = 2/(vidya_length+1).
        momentum_length: CMO lookback for the up/down volume sums.
        band_mult: ATR multiple for the upper/lower band envelope.
        atr_length: Wilder ATR length for the bands. 200 on the user's chart, which
            is why a run needs >= 700 bars of history.
        source: Price source. "close" on the user's chart.
        trend_rule: "band" (close beyond the ATR band) or "line" (close vs the VIDYA
            line). DISPUTED -- see U-3 in SPEC.md 4.
    """

    vidya_length: int = 34
    momentum_length: int = 20
    band_mult: float = 2.0
    atr_length: int = 200
    source: str = "close"
    trend_rule: str = "band"  # "band" | "line" -- DISPUTED, see U-3


# --------------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------------

def _validate(bars: pd.DataFrame) -> None:
    """Validate a BarFrame, preferring ``data.validate_bars``."""
    if _data is not None and hasattr(_data, "validate_bars"):
        _data.validate_bars(bars)
        return
    _fallback_validate_bars(bars)


def _fallback_validate_bars(df: pd.DataFrame) -> None:
    """Enforce the SPEC.md 3.1 BarFrame invariants when data.py is unavailable.

    Deliberately does NOT check frequency contiguity -- that is data.py's job via
    ``trim_to_contiguous``. Raises ValueError naming the first violated invariant.
    """
    if not isinstance(df, pd.DataFrame):
        raise ValueError("bars must be a pandas DataFrame")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("bars.index must be a DatetimeIndex")
    if df.index.name != "timestamp":
        raise ValueError(f"bars.index.name must be 'timestamp', got {df.index.name!r}")
    if df.index.tz is None or str(df.index.tz) not in ("UTC", "utc"):
        raise ValueError("bars.index must be tz-aware UTC")
    if len(df) < 1:
        raise ValueError("bars must have at least one row")
    if not df.index.is_monotonic_increasing:
        raise ValueError("bars.index must be strictly increasing")
    if df.index.has_duplicates:
        raise ValueError("bars.index must not contain duplicates")
    if list(df.columns) != _BAR_COLUMNS:
        raise ValueError(f"bars.columns must be exactly {_BAR_COLUMNS}, got {list(df.columns)}")
    for col in _BAR_COLUMNS:
        if df[col].dtype != np.float64:
            raise ValueError(f"bars[{col!r}] must be float64, got {df[col].dtype}")
        if not np.isfinite(df[col].to_numpy()).all():
            raise ValueError(f"bars[{col!r}] contains NaN or inf")
    o, h, l, c, v = (df[k].to_numpy() for k in _BAR_COLUMNS)
    if not (l <= np.minimum(o, c)).all():
        raise ValueError("invariant violated: low <= min(open, close)")
    if not (h >= np.maximum(o, c)).all():
        raise ValueError("invariant violated: high >= max(open, close)")
    if not (l <= h).all():
        raise ValueError("invariant violated: low <= high")
    if not (v >= 0).all():
        raise ValueError("invariant violated: volume >= 0")


def _resolve_source(bars: pd.DataFrame, source: str) -> pd.Series:
    """Return the float64 price series named by ``source``."""
    s = source.lower()
    if s in ("open", "high", "low", "close"):
        out = bars[s]
    elif s == "hl2":
        out = (bars["high"] + bars["low"]) / 2.0
    elif s == "hlc3":
        out = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    elif s == "ohlc4":
        out = (bars["open"] + bars["high"] + bars["low"] + bars["close"]) / 4.0
    else:
        raise ValueError(
            f"unsupported source {source!r}; expected one of "
            "close/open/high/low/hl2/hlc3/ohlc4"
        )
    return out.astype("float64")


# --------------------------------------------------------------------------------
# Public indicator primitives
# --------------------------------------------------------------------------------

def rma(x: pd.Series, length: int) -> pd.Series:
    """Pine ``ta.rma`` -- Wilder's running moving average.

    Seeded with the simple mean of the first ``length`` finite values (Pine's own
    seeding), then ``r[i] = alpha*x[i] + (1-alpha)*r[i-1]`` with ``alpha = 1/length``.
    Interior NaN holds the previous state rather than propagating.

    Args:
        x: float64 Series.
        length: averaging length, >= 1.

    Returns:
        float64 Series, same index and length as ``x``, NaN before the seed bar.
    """
    if length < 1:
        raise ValueError("length must be >= 1")
    vals = x.to_numpy(dtype="float64", copy=True)
    n = len(vals)
    out = np.full(n, np.nan, dtype="float64")

    finite = np.isfinite(vals)
    if not finite.any():
        return pd.Series(out, index=x.index, dtype="float64")
    first = int(np.argmax(finite))
    seed_end = first + length
    if seed_end > n:
        return pd.Series(out, index=x.index, dtype="float64")

    window = vals[first:seed_end]
    if not np.isfinite(window).all():
        # A NaN inside the seed window: fall back to the mean of its finite members.
        window = window[np.isfinite(window)]
        if window.size == 0:
            return pd.Series(out, index=x.index, dtype="float64")

    alpha = 1.0 / float(length)
    prev = float(window.mean())
    out[seed_end - 1] = prev
    for i in range(seed_end, n):
        xi = vals[i]
        if np.isfinite(xi):
            prev = alpha * xi + (1.0 - alpha) * prev
        # else: hold previous state
        out[i] = prev
    return pd.Series(out, index=x.index, dtype="float64")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Pine ``ta.tr`` -- max(h-l, |h-prev_close|, |l-prev_close|).

    On the first bar there is no previous close, so the result is ``high - low``;
    that falls out of the NaN-skipping max below.

    Returns:
        float64 Series, same index/length as the inputs.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)  # NaN-skipping: bar 0 reduces to high - low, matching ta.tr
    return tr.astype("float64")


def wilder_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int = 200
) -> pd.Series:
    """Pine ``ta.atr(length)`` -- RMA of the true range. NOT a simple mean.

    VERIFY: Pine seeds ta.rma with an SMA of the first `length` true ranges, then
    recurses at alpha = 1/length. That is what this implements. pandas
    `ewm(alpha=1/length, adjust=False)` seeds on the FIRST value instead and will
    differ for hundreds of bars at length=200. If the user's chart ATR(200) and this
    column diverge, the seeding convention is the first thing to check -- compare the
    CSV `atr200` against the chart's ATR(200) at a bar at least 400 bars in, where any
    seeding difference has decayed to nothing. A mismatch that persists that deep is
    a real bug; a mismatch only in the first ~200 bars is the seed and is harmless
    (WARMUP_BARS=400 excludes it).

    Returns:
        float64 Series, same index/length as the inputs, NaN for the first
        ``length - 1`` bars.
    """
    return rma(true_range(high, low, close), length)


def cmo_abs(src: pd.Series, momentum_length: int = 20) -> pd.Series:
    """Absolute Chande Momentum Oscillator in [0, 1], as used by BigBeluga's VIDYA.

    Pine origin (BigBeluga Volumatic VIDYA, `vidya_calc`)::

        mom     = ta.change(src)
        upSum   = math.sum(math.max(mom, 0), momentum)
        downSum = math.sum(-math.min(mom, 0), momentum)
        cmo     = math.abs((upSum - downSum) / (upSum + downSum))

    A zero denominator (a perfectly flat window) yields 0.0, which makes k = 0 and
    holds the VIDYA line flat -- the same outcome as Pine's na guard.

    VERIFY: this uses min_periods=momentum_length on the rolling sums, so the first
    finite value lands at index `momentum_length` (index 20 by default), because
    ta.change is na at bar 0. Pine's math.sum warmup at bar 19 may differ by one bar.
    This is 380 bars before any trade can be generated (WARMUP_BARS=400) and cannot
    affect a result, but it is stated here so nobody rediscovers it as a "bug".

    Returns:
        float64 Series in [0, 1], same index/length as ``src``, NaN during warmup.
    """
    if momentum_length < 1:
        raise ValueError("momentum_length must be >= 1")
    mom = src.diff()
    up = mom.clip(lower=0.0)
    dn = (-mom).clip(lower=0.0)
    sum_up = up.rolling(momentum_length, min_periods=momentum_length).sum()
    sum_dn = dn.rolling(momentum_length, min_periods=momentum_length).sum()
    denom = sum_up + sum_dn
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = (sum_up - sum_dn) / denom
    out = raw.abs()
    # Flat window -> denominator 0 -> Pine's na guard -> treat as zero momentum.
    out = out.where(denom.isna() | (denom != 0.0), 0.0)
    return out.astype("float64")


def vidya_line(
    src: pd.Series, vidya_length: int = 34, momentum_length: int = 20
) -> pd.Series:
    """CMO-weighted Variable Index Dynamic Average.

    Pine origin (BigBeluga Volumatic VIDYA, `vidya_calc`)::

        alpha = 2 / (length + 1)
        vidya := src*alpha*cmo + nz(vidya[1])*(1 - alpha*cmo)

    Seeded at the first bar where the CMO is finite with ``vidya[i0] = src[i0]``.
    A NaN weight after the seed holds the previous value.

    Returns:
        float64 Series, same index/length as ``src``, NaN before the seed bar.
    """
    if vidya_length < 1:
        raise ValueError("vidya_length must be >= 1")
    cmo = cmo_abs(src, momentum_length)
    alpha = 2.0 / (float(vidya_length) + 1.0)  # Pine: 2/(length+1), = 2/35 at 34
    k = (alpha * cmo).to_numpy(dtype="float64")
    s = src.to_numpy(dtype="float64")
    n = len(s)
    out = np.full(n, np.nan, dtype="float64")

    valid = np.isfinite(k) & np.isfinite(s)
    if not valid.any():
        return pd.Series(out, index=src.index, dtype="float64")
    i0 = int(np.argmax(valid))
    prev = float(s[i0])
    out[i0] = prev
    for i in range(i0 + 1, n):
        ki, si = k[i], s[i]
        if np.isfinite(ki) and np.isfinite(si):
            prev = ki * si + (1.0 - ki) * prev
        # else: hold previous state
        out[i] = prev
    return pd.Series(out, index=src.index, dtype="float64")


def trend_state(
    close: pd.Series,
    vidya: pd.Series,
    upper: pd.Series,
    lower: pd.Series,
    trend_rule: str = "band",
) -> pd.Series:
    """Discrete VIDYA trend state: +1 up, -1 down, 0 undefined before the first break.

    Pine origin (BigBeluga Volumatic VIDYA)::

        if ta.crossover(source, upper)
            is_trend_up := true
        if ta.crossunder(source, lower)
            is_trend_up := false

    Implemented as the level test of SPEC.md 2.2 (``close > upper`` / ``close < lower``)
    with the state held in between. That is equivalent to Pine's crossover/crossunder
    here: because the state is held, re-triggering on a level the series is already
    beyond is a no-op, and a fresh break necessarily comes from the other side of the
    envelope. The two formulations produce an identical flip sequence.

    Bands are NaN during the ATR warmup; the comparisons are then False and the state
    correctly stays 0.

    VERIFY (U-3, high leverage): this defaults to the BAND break. Find a bar on the
    chart where the ribbon changes colour, then read that bar's close, vidya, upper
    and lower out of the --verify CSV. If the flip bar's close is beyond the band, the
    default is right. If it merely crossed the VIDYA line while staying inside the
    band, set trend_rule="line". This changes every leg_id and therefore every
    delta_pct, so it must be settled before any delta number is trusted.

    Returns:
        int8 Series in {-1, 0, +1}, same index/length as ``close``.
    """
    if trend_rule not in ("band", "line"):
        raise ValueError(f"trend_rule must be 'band' or 'line', got {trend_rule!r}")

    if trend_rule == "band":
        up_break = close > upper
        dn_break = close < lower
    else:  # "line" -- the U-3 alternative
        up_break = close > vidya
        dn_break = close < vidya

    raw = pd.Series(np.nan, index=close.index, dtype="float64")
    raw[dn_break.to_numpy()] = -1.0
    raw[up_break.to_numpy()] = 1.0
    # ffill = "the bands hold the state"; fillna(0) = undefined before the first break.
    return raw.ffill().fillna(0.0).astype("int8")


# --------------------------------------------------------------------------------
# Module entrypoint
# --------------------------------------------------------------------------------

def compute(bars: pd.DataFrame, params: VidyaParams = VidyaParams()) -> pd.DataFrame:
    """THE module entrypoint. Input: 4h BarFrame. Output index ``.equals(bars.index)``.

    Columns, exactly, in this order:
      vidya       float64  the VIDYA line
      atr         float64  Wilder ATR(atr_length)
      upper       float64  vidya + band_mult*atr
      lower       float64  vidya - band_mult*atr
      trend       int8     +1 up / -1 down / 0 undefined-before-first-break
      trend_flip  bool     trend[i] != trend[i-1] AND trend[i] != 0
      leg_id      int64    increments on each trend_flip; starts at 0
      buy_vol     float64  cumulative since last flip, close>open bars
      sell_vol    float64  cumulative since last flip, close<open bars
      delta_pct   float64  2*(buy-sell)/(buy+sell)*100; NaN if buy+sell == 0

    delta_pct is CUMULATIVE OVER THE TREND LEG, not per bar. A per-bar delta is a bug
    -- it produced a meaningless -69% in an earlier attempt. Whole 4h bars are
    classified by close vs open: close>open contributes the bar's ENTIRE volume to
    buy, close<open the entire volume to sell, close==open contributes to neither.
    The accumulator resets on every flip, and the flip bar's own volume is the first
    contribution to the NEW leg.

    Warmup: trend is 0 and delta_pct is NaN until the first band break. buy_vol and
    sell_vol are left raw on leg 0 (see the module docstring's OPEN QUESTIONS).

    Does not mutate ``bars``. Causal: every value at bar i depends only on bars <= i.

    Args:
        bars: a validated 4h BarFrame (SPEC.md 3.1).
        params: VidyaParams; defaults are the user's chart, 34/20/2/close, ATR 200.

    Returns:
        DataFrame with exactly VIDYA_COLUMNS in that order and VIDYA_DTYPES dtypes,
        index identical to ``bars.index``.
    """
    _validate(bars)
    if params.trend_rule not in ("band", "line"):
        raise ValueError(f"trend_rule must be 'band' or 'line', got {params.trend_rule!r}")
    for name in ("vidya_length", "momentum_length", "atr_length"):
        if getattr(params, name) < 1:
            raise ValueError(f"{name} must be >= 1")

    src = _resolve_source(bars, params.source)
    open_ = bars["open"].astype("float64")
    high = bars["high"].astype("float64")
    low = bars["low"].astype("float64")
    close = bars["close"].astype("float64")
    volume = bars["volume"].astype("float64")

    # --- line and bands -------------------------------------------------------
    vidya = vidya_line(src, params.vidya_length, params.momentum_length)
    atr = wilder_atr(high, low, close, params.atr_length)
    upper = vidya + float(params.band_mult) * atr
    lower = vidya - float(params.band_mult) * atr

    # --- discrete trend and flip ---------------------------------------------
    trend = trend_state(close, vidya, upper, lower, params.trend_rule)
    prev_trend = trend.shift(1).fillna(0.0).astype("int8")
    # SPEC.md 3.4: a flip requires a real state, so 0 -> +/-1 flips but nothing
    # ever flips INTO 0. The first band break is therefore a flip and opens leg 1.
    trend_flip = ((trend != prev_trend) & (trend != 0)).astype("bool")
    leg_id = trend_flip.cumsum().astype("int64")

    # --- cumulative Delta Volume, reset on every flip -------------------------
    # Whole-bar classification by close vs open. close == open contributes to neither.
    buy_contrib = volume.where(close > open_, 0.0)
    sell_contrib = volume.where(close < open_, 0.0)
    # cumsum WITHIN leg_id == "accumulate since the last flip, reset on flip".
    # Causal: leg_id is causal and a within-group cumsum only ever reads the prefix.
    buy_vol = buy_contrib.groupby(leg_id, sort=False).cumsum().astype("float64")
    sell_vol = sell_contrib.groupby(leg_id, sort=False).cumsum().astype("float64")

    denom = buy_vol + sell_vol
    with np.errstate(invalid="ignore", divide="ignore"):
        # Confirmed against four of the user's screenshots:
        #   Delta% = 2 * (buy - sell) / (buy + sell) * 100
        delta_pct = 2.0 * (buy_vol - sell_vol) / denom * 100.0
    delta_pct = delta_pct.where(denom > 0.0, np.nan).astype("float64")
    # SPEC.md 3.4 warmup clause: no leg has begun before the first flip.
    delta_pct = delta_pct.where(leg_id > 0, np.nan)

    out = pd.DataFrame(
        {
            "vidya": vidya.astype("float64"),
            "atr": atr.astype("float64"),
            "upper": upper.astype("float64"),
            "lower": lower.astype("float64"),
            "trend": trend.astype("int8"),
            "trend_flip": trend_flip.astype("bool"),
            "leg_id": leg_id.astype("int64"),
            "buy_vol": buy_vol,
            "sell_vol": sell_vol,
            "delta_pct": delta_pct,
        },
        index=bars.index,
    )[VIDYA_COLUMNS]

    if not out.index.equals(bars.index):  # pragma: no cover - defensive
        raise AssertionError("vidya.compute changed the index; this is a bug")
    return out


# --------------------------------------------------------------------------------
# Leg-age diagnostics
#
# These are helper functions, NOT extra columns: SPEC.md 3.4 freezes compute()'s
# output at exactly VIDYA_COLUMNS, so leg age is exposed separately.
# --------------------------------------------------------------------------------

def flip_timestamps(vd: pd.DataFrame) -> pd.DatetimeIndex:
    """Timestamps of every trend flip, ascending.

    Args:
        vd: a ``compute`` output frame.

    Returns:
        tz-aware UTC DatetimeIndex of the bars where ``trend_flip`` is True.
    """
    return pd.DatetimeIndex(vd.index[vd["trend_flip"].to_numpy()], name=vd.index.name)


def leg_age_bars(vd: pd.DataFrame) -> pd.Series:
    """Bars elapsed since the flip that opened the current leg. 0 on the flip bar.

    On leg 0 (before the first flip) this counts from the first bar of the data,
    which is an artefact of where the series starts and is not a real leg age.

    Args:
        vd: a ``compute`` output frame.

    Returns:
        int64 Series named "leg_age_bars", index identical to ``vd.index``.
    """
    pos = pd.Series(np.arange(len(vd), dtype="int64"), index=vd.index)
    start = pos.groupby(vd["leg_id"], sort=False).transform("min")
    return (pos - start).astype("int64").rename("leg_age_bars")


def leg_summary(vd: pd.DataFrame) -> pd.DataFrame:
    """One row per trend leg -- the human-readable view of the flip series.

    Use this to eyeball flip timing against the chart: if the legs do not line up
    with the ribbon's colour changes, `trend_rule` is wrong (U-3) and every
    ``delta_pct`` downstream is wrong with it.

    Args:
        vd: a ``compute`` output frame.

    Returns:
        DataFrame with RangeIndex and columns:
          leg_id int64, trend int8, start_ts / end_ts datetime64[ns, UTC],
          n_bars int64, buy_vol float64, sell_vol float64,
          final_delta_pct float64 (the leg's delta at its last bar).
    """
    g = vd.groupby("leg_id", sort=True)
    out = pd.DataFrame(
        {
            "leg_id": g["trend"].last().index.astype("int64"),
            "trend": g["trend"].last().to_numpy(dtype="int8"),
            "start_ts": g.apply(lambda d: d.index[0], include_groups=False).to_numpy(),
            "end_ts": g.apply(lambda d: d.index[-1], include_groups=False).to_numpy(),
            "n_bars": g.size().to_numpy().astype("int64"),
            "buy_vol": g["buy_vol"].last().to_numpy(dtype="float64"),
            "sell_vol": g["sell_vol"].last().to_numpy(dtype="float64"),
            "final_delta_pct": g["delta_pct"].last().to_numpy(dtype="float64"),
        }
    ).reset_index(drop=True)
    out["start_ts"] = pd.to_datetime(out["start_ts"], utc=True)
    out["end_ts"] = pd.to_datetime(out["end_ts"], utc=True)
    return out
