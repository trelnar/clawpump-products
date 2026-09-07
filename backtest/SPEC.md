# SPEC.md — BTC 4h Backtest Harness

**Status:** authoritative. Six builders implement against this in parallel. Where this document conflicts with any of the five source specs, this document wins. No builder edits another builder's file.

---

## 1. The question this harness exists to answer

The Two-Pole Oscillator dot is the entry event. The Volumatic VIDYA ribbon and the cumulative Delta Volume are gates on that event. **The VIDYA flips *after* a turn, so the ribbon+delta gates structurally reject turn signals and admit only pullback-in-trend signals.**

> **Does that filter add expectancy or destroy it?**

The evidence that motivates the question: on 2026-08-13 the gates rejected a long that preceded a 62k→80k run (+25%). On 2026-08-22 the gates rejected a short that marked a local top. On 2026-08-30 the gates passed a long. Three points is an anecdote; this harness turns it into a measurement, or proves that the sample is too small to measure — which is an acceptable and expected outcome.

Secondary question, equally important because it is the largest unknown in the user's system: **what exit model does this signal actually need?** The one live trade was stopped at 76,233 — the exact low of a sweep — and price then ran through its take-profit to 82,272. The exit grid must therefore include stops placed **beyond the signal bar's extreme**, not fixed percentages alone.

**Scope note (BTC-only):** the live rules include a BTC-confluence filter invalidating alt signals that oppose BTC. A BTC-only backtest cannot exercise it. It is out of scope here and must be re-tested before the rules are applied to alts.

**Standing disclaimer that every report must carry:** the oscillator port is synthetic-data tested only. Until the user diffs `--verify` output against his TradingView chart, every number in this harness is provisional.

---

## 2. Consolidated indicator math

Notation: `x[i]` is the value at bar `i`; bars are UTC-aligned 4h closes.

### 2.1 Two-Pole Oscillator [BigBeluga]

Parameters (user's chart, confirmed against Pine source):

| Param | Value | Note |
|---|---|---|
| `filter_length` | **20** | Script default is 15; the user's chart is customised to 20. Default in code is 20. |
| `sma_length` | **25** | Used for **both** the mean and the normalisation denominator. |
| `ddof` | **0** | **Population** standard deviation. Not sample. `Series.std(ddof=0)`. |
| `signal_delay` | **4** | Signal line is a 4-bar delayed copy of the oscillator. |

**Step 1 — normalise:**
```
sma[i] = mean(close[i-24 .. i])              # 25-bar window
sd[i]  = popstd(close[i-24 .. i])            # 25-bar window, ddof=0
z[i]   = (close[i] - sma[i]) / sd[i]         # NaN where sd == 0
```
`z` is NaN for the first 24 bars.

**Step 2 — two-pole filter** (EMA-style smoothing applied twice):
```
a = 2 / (filter_length + 1)                  # = 2/21 for length 20
p1[i] = a*z[i]   + (1-a)*p1[i-1]
p2[i] = a*p1[i]  + (1-a)*p2[i-1]
osc   = p2
```
Seeding: at the first index `i0` where `z` is finite, `p1[i0] = p2[i0] = z[i0]`. Bars before `i0` are NaN. Any NaN in `z` after `i0` (a zero-variance window) **holds** the previous filter state rather than propagating NaN.

**Step 3 — signal line:**
```
osc_signal[i] = osc[i-4]                     # plain shift, NaN-filled at the head
```

**Step 4 — dots (ZERO-LINE GATED — do not omit this gate):**
```
cross_up[i]  = osc[i] >  osc_signal[i]  and  osc[i-1] <= osc_signal[i-1]
cross_dn[i]  = osc[i] <  osc_signal[i]  and  osc[i-1] >= osc_signal[i-1]

dot_long[i]  = cross_up[i] and osc[i] < 0    # teal
dot_short[i] = cross_dn[i] and osc[i] > 0    # purple
```
An earlier version of the user's bot omitted the `osc < 0` / `osc > 0` gate and printed phantom mid-zone dots absent from the chart. The gate is mandatory.

**Step 5 — tint (DISPUTED — see U-2).** Two candidate rules, switchable:
```
tint_rule = "slope":  tint[i] = +1 if osc[i] > osc[i-1] else -1
tint_rule = "cross":  tint[i] = +1 if osc[i] > osc_signal[i] else -1
```
**Resolution:** the specs disagree. Chart evidence favours `"slope"` (teal appeared one bar *before* the dot, which the cross rule cannot produce). **Default = `"slope"`. Marked as needing chart verification (U-2).**

**Trade the dot, not the colour.** The tint is diagnostic output only. No gate, entry, or exit may read `tint`. This is a hard rule: builders of `strategy.py` must not reference the `tint` column.

**Smoothing warning for test authors:** the filter is heavily smoothed — a single −2% candle moved it by 0.001. No test may assume a sharp candle swings the oscillator. Tests that assert dot production must use a synthetic series with a sustained multi-bar excursion.

### 2.2 Volumatic VIDYA [BigBeluga]

Parameters from the user's chart: **34 / 20 / 2 / close**.

| Param | Value |
|---|---|
| `vidya_length` | 34 |
| `momentum_length` | 20 |
| `band_mult` | 2.0 |
| `source` | `close` |
| `atr_length` | **200** |

The ATR(200) dependency is why a run needs far more history than 300 bars. See §3.7 warmup.

**CMO-weighted VIDYA:**
```
mom[i]    = src[i] - src[i-1]
up[i]     = max(mom[i], 0)
dn[i]     = max(-mom[i], 0)
sum_up[i] = sum(up[i-19 .. i])
sum_dn[i] = sum(dn[i-19 .. i])
cmo[i]    = abs((sum_up[i] - sum_dn[i]) / (sum_up[i] + sum_dn[i]))    # 0 if denom == 0
alpha     = 2 / (vidya_length + 1)                                     # = 2/35
k[i]      = alpha * cmo[i]
vidya[i]  = k[i]*src[i] + (1 - k[i])*vidya[i-1]
```
Seed `vidya[i0] = src[i0]` at the first bar where `cmo` is finite.

**Bands:**
```
atr[i]   = Wilder ATR over atr_length=200 of (high, low, close)
upper[i] = vidya[i] + band_mult * atr[i]
lower[i] = vidya[i] - band_mult * atr[i]
```
ATR is **Wilder's** (RMA smoothing, `alpha = 1/200`), matching Pine `ta.atr`. Not a simple mean of true range.

**Trend state and flip (DISPUTED — see U-3):**
```
trend[i] = +1  if close[i] > upper[i]
         = -1  if close[i] < lower[i]
         = trend[i-1]  otherwise                # bands hold the state
trend_flip[i] = trend[i] != trend[i-1]
```
Initial `trend` before the first band break is `0` (undefined); no gate may pass while `trend == 0`.

**Resolution:** the specs disagree on whether the flip is a band break (above) or a simple `close` vs `vidya`-line cross. **Default = band break**, because the band multiplier `2` and the ATR(200) parameter are otherwise unused and BigBeluga's "Volumatic" naming refers to the band envelope. **Marked as needing chart verification (U-3).** Implement the alternative as `trend_rule="line"` so it is one flag away.

### 2.3 Delta Volume — cumulative, per trend leg

Formula, reverse-engineered and confirmed against four of the user's screenshots:
```
delta_pct = 2 * (buy - sell) / (buy + sell) * 100
```

**CRITICAL SEMANTICS — this is where a previous session went wrong and produced a meaningless −69%:**

- Buy/sell volume **accumulates across the entire trend leg since the last VIDYA flip**. It resets to zero on every `trend_flip`.
- Whole 4h bars are classified by **close vs open**:
  - `close > open` → the bar's **entire** volume counts as buy
  - `close < open` → the bar's **entire** volume counts as sell
  - `close == open` → the bar contributes to neither
- It is **not** a per-bar measurement and **not** derived from 1-minute candles.

```
leg_id[i]   = leg_id[i-1] + 1 if trend_flip[i] else leg_id[i-1]     # starts at 0
buy_vol[i]  = (buy_vol[i-1]  if not trend_flip[i] else 0) + (volume[i] if close[i] >  open[i] else 0)
sell_vol[i] = (sell_vol[i-1] if not trend_flip[i] else 0) + (volume[i] if close[i] <  open[i] else 0)
delta_pct[i]= 2*(buy_vol[i] - sell_vol[i]) / (buy_vol[i] + sell_vol[i]) * 100   # NaN if denom == 0
```
The flip bar itself **starts** the new leg and its own volume is the first contribution to the new leg's accumulator.

**The ±20% threshold is SUSPECT.** It was chosen against a per-bar reading. Observed real cumulative values on the user's charts: +27%, +46%, +54%, +86%, +132%, +179% — a cumulative leg reading is structurally much larger than a per-bar one, so a ±20% gate on cumulative data is close to a no-op. **The threshold is therefore a swept parameter, never a constant.** See §5.2 and U-1.

---

## 3. THE INTERFACE CONTRACT

This section is binding. Two agents writing different files must produce code that imports and runs. If something here is under-specified for your module, **implement the literal wording and record the ambiguity in your module docstring** — do not invent a different signature.

### 3.0 Universal rules

1. Python 3.11. Only `numpy`, `pandas`, and stdlib in module code (`requests` allowed in `data.py` only, import-guarded). `pytest` in tests only.
2. Every module is importable with **no side effects**: no network, no disk read, no printing at import time.
3. All timestamps are **tz-aware UTC** (`pandas.Timestamp` with `tz="UTC"`). Naive datetimes are a bug. Never `tz_localize(None)`.
4. All float columns are `float64`. All bool columns are real `numpy.bool_` dtype `bool`, never object.
5. Functions that return a per-bar result return a DataFrame **whose index is `.equals()` to the input bars index**. Same length, same order, no reindexing, no dropna. Warmup periods are `NaN`/`False`/`0`, never dropped.
6. No function mutates its input. Copy before assigning.
7. Look-ahead is forbidden. Any value at bar `i` may depend only on bars `<= i`. `verify.py` includes a shift-invariance check (§3.6).

### 3.1 The canonical bar DataFrame — `BarFrame`

**Defined once. Every module consumes and produces exactly this.** Declared in `data.py`; all other modules import the constants from there.

```python
# data.py
BAR_COLUMNS: list[str] = ["open", "high", "low", "close", "volume"]
BAR_DTYPES: dict[str, str] = {
    "open": "float64", "high": "float64", "low": "float64",
    "close": "float64", "volume": "float64",
}
INDEX_NAME: str = "timestamp"
BAR_FREQ_4H: str = "4h"
BAR_FREQ_1H: str = "1h"
```

A **BarFrame** is a `pandas.DataFrame` where:

- `df.index` is a `pandas.DatetimeIndex`, `df.index.name == "timestamp"`, `df.index.tz` is UTC.
- The index is **strictly increasing**, has **no duplicates**, and is **exactly contiguous** at the stated frequency — `df.index.to_series().diff().dropna().unique()` is a single value equal to the frequency.
- For 4h frames every timestamp is UTC-aligned to `00:00, 04:00, 08:00, 12:00, 16:00, 20:00` — i.e. `(hour % 4 == 0) and minute == second == microsecond == 0`.
- Columns are **exactly** `BAR_COLUMNS`, in that order, all `float64`. No extra columns. No `NaN` in any OHLCV cell.
- The timestamp is the bar's **OPEN** time (Coinbase convention). A bar labelled `2026-08-30 16:00` covers `[16:00, 20:00)` and **closes** at `20:00`. Every "on bar close N" statement in this document means "using the bar whose open-time label is N, decided at time N+4h".
- `len(df) >= 1`.

**Invariants** (`validate_bars` enforces): `low <= min(open, close)`, `high >= max(open, close)`, `low <= high`, `volume >= 0`, all values finite.

### 3.2 `data.py` — Coinbase fetch, aggregation, cache, synthetic, fixtures

```python
class BarSchemaError(ValueError): ...
class DataFetchError(RuntimeError): ...

def validate_bars(df: pd.DataFrame, freq: str = BAR_FREQ_4H) -> None:
    """Raise BarSchemaError with a message naming the first violated invariant.
    Returns None on success. Cheap enough to call at every module boundary."""

def fetch_1h_coinbase(
    product_id: str = "BTC-USD",
    start: pd.Timestamp | str = ...,
    end: pd.Timestamp | str = ...,
    *,
    max_retries: int = 5,
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Paginated 1h candles from Coinbase Exchange.
    GET https://api.exchange.coinbase.com/products/{product_id}/candles
        ?granularity=3600&start=<iso>&end=<iso>
    Coinbase caps a response at 300 candles -> page in <=300h windows, ascending.
    Coinbase returns rows as [time, low, high, open, close, volume] in DESCENDING
    time order -> reorder columns to BAR_COLUMNS and sort ascending.
    `time` is a UNIX second integer -> pd.to_datetime(unit="s", utc=True).
    Returns a 1h BarFrame. Raises DataFetchError on non-200 after retries.

    Coinbase has NO 4h granularity: granularity=14400 returns HTTP 400
    "Unsupported granularity". Valid: 60, 300, 900, 3600, 21600, 86400.
    Never request 14400.

    NETWORK IS BLOCKED IN THE DEV SANDBOX (403 on CONNECT). This function must
    never be called in tests. It runs on the user's Ubuntu VPS. Do not attempt to
    route around the block."""

def aggregate_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """1h BarFrame -> UTC-aligned 4h BarFrame.
    df.resample("4h", label="left", closed="left", origin="epoch")
      open=first, high=max, low=min, close=last, volume=sum
    origin="epoch" guarantees 00/04/08/12/16/20 UTC buckets.
    DROP any bucket that does not contain exactly 4 source bars -- a partial
    bucket at either end must not become a bar. Returns a validated 4h BarFrame."""

def trim_to_contiguous(df: pd.DataFrame, freq: str = BAR_FREQ_4H) -> pd.DataFrame:
    """Return the LONGEST contiguous run at `freq`. On a tie, return the run that
    ends latest (most recent data wins). A gap must never silently shift an SMA
    window -- this is the guard against that. Emits a logging.warning naming the
    dropped span and bar count when it trims anything."""

def load_bars(
    product_id: str = "BTC-USD",
    start: pd.Timestamp | str = ...,
    end: pd.Timestamp | str = ...,
    *,
    cache_dir: str | pathlib.Path = "backtest/.cache",
    allow_network: bool = True,
) -> pd.DataFrame:
    """The one entrypoint the rest of the harness uses for real data.
    fetch (cache-first) -> aggregate_4h -> trim_to_contiguous -> validate_bars.
    Cache file: {cache_dir}/{product_id}_1h.parquet (fall back to .csv with an
    ISO-8601 UTC timestamp column if pyarrow is unavailable). Cache stores 1h
    bars, never 4h -- aggregation is cheap and re-derivable.
    allow_network=False -> serve from cache only; raise DataFetchError if the
    cache cannot cover [start, end]. Tests always pass allow_network=False."""

def synthetic_bars(
    n: int = 3000,
    *,
    seed: int = 0,
    start: pd.Timestamp | str = "2024-01-01T00:00:00Z",
    start_price: float = 50_000.0,
    annual_vol: float = 0.60,
    drift: float = 0.0,
    regime: str = "gbm",          # "gbm" | "trend_up" | "trend_down" | "chop" | "sweep"
) -> pd.DataFrame:
    """Deterministic given `seed` (numpy.random.default_rng(seed)). Returns a
    valid 4h BarFrame of length n starting at `start`, contiguous, UTC-aligned.
    Regimes exist so indicator tests can force a known outcome:
      "trend_up"/"trend_down" -- must produce at least one VIDYA trend flip
      "chop"                  -- must produce oscillator dots in both directions
      "sweep"                 -- embeds a wick that takes out a prior low by
                                 ~0.3% and closes back above it, to exercise
                                 stop-beyond-extreme exits (the 76,233 case)
    Guarantee: high >= max(open, close) and low <= min(open, close) always."""

def load_fixture(name: str) -> pd.DataFrame:
    """Load a checked-in fixture BarFrame by name from backtest/fixtures/.
    Required fixture names (data.py owner creates these files):
      "btc_4h_signal_log"  -- 4h bars spanning at least 2026-06-01..2026-09-05
                              so all four SIGNAL_LOG events fall inside it with
                              >= 400 bars of warmup before the first.
    Raises FileNotFoundError with the expected path on a miss."""

SIGNAL_LOG: tuple[dict, ...] = (
    # Raw oscillator dots from the user's LIVE BOT. NO gates applied.
    # Gate readings are from his charts. These are the regression anchors.
    {"ts": "2026-08-07T04:00:00Z", "osc": +0.694, "side": "short",
     "ribbon": None,     "delta_pct": None,   "gates_pass": None},
    {"ts": "2026-08-13T12:00:00Z", "osc": -1.087, "side": "long",
     "ribbon": "short",  "delta_pct": None,   "gates_pass": False},   # ribbon PINK, delta negative
    {"ts": "2026-08-22T08:00:00Z", "osc": +1.690, "side": "short",
     "ribbon": "long",   "delta_pct": +132.0, "gates_pass": False},   # ribbon GREEN
    {"ts": "2026-08-30T16:00:00Z", "osc": -0.975, "side": "long",
     "ribbon": "long",   "delta_pct": +53.88, "gates_pass": True},
)

CHART_ANCHORS: tuple[dict, ...] = (
    # Independent points where the user's bot matched his TradingView chart.
    {"ts": "2026-07-31T20:00:00Z", "osc": +0.135, "event": "purple_dot"},
    {"ts": None, "osc_bot": 1.693, "osc_chart": 1.71, "event": "osc_value"},
)
```

`side` is always the lowercase string `"long"` or `"short"`. This is the only side vocabulary in the harness — no `"buy"`, no `+1/-1` at API boundaries.

### 3.3 `two_pole.py` — oscillator and dots

```python
@dataclasses.dataclass(frozen=True)
class TwoPoleParams:
    filter_length: int = 20        # user's chart; script default is 15
    sma_length:    int = 25        # mean AND normalisation denominator
    signal_delay:  int = 4
    ddof:          int = 0         # POPULATION std. Do not change.
    tint_rule:     str = "slope"   # "slope" | "cross"  -- DISPUTED, see U-2
    source:        str = "close"

def two_pole_filter(x: pd.Series, length: int) -> pd.Series:
    """alpha = 2/(length+1), EMA-style recursion applied TWICE.
    Returns float64 Series, same index/length as x. Leading NaNs preserved until
    the first finite x; interior NaNs hold the previous filter state."""

def compute(bars: pd.DataFrame, params: TwoPoleParams = TwoPoleParams()) -> pd.DataFrame:
    """THE module entrypoint. Input: 4h BarFrame. Output index .equals(bars.index).

    Columns, exactly, in this order:
      osc         float64   the oscillator
      osc_signal  float64   osc.shift(signal_delay)
      tint        int8      +1 teal / -1 purple / 0 undefined (diagnostic ONLY)
      dot_long    bool      cross_up  AND osc < 0
      dot_short   bool      cross_dn  AND osc > 0

    Warmup: osc NaN for the first (sma_length - 1) bars; dots False there.
    dot_long and dot_short are never both True on the same bar."""
```

`strategy.py` must not read `tint`. `verify.py` dumps it.

### 3.4 `vidya.py` — VIDYA, trend flip, cumulative Delta Volume

```python
@dataclasses.dataclass(frozen=True)
class VidyaParams:
    vidya_length:    int   = 34
    momentum_length: int   = 20
    band_mult:       float = 2.0
    atr_length:      int   = 200
    source:          str   = "close"
    trend_rule:      str   = "band"   # "band" | "line"  -- DISPUTED, see U-3

def compute(bars: pd.DataFrame, params: VidyaParams = VidyaParams()) -> pd.DataFrame:
    """THE module entrypoint. Input: 4h BarFrame. Output index .equals(bars.index).

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

    delta_pct is CUMULATIVE OVER THE TREND LEG, not per bar. A per-bar delta is
    a bug -- it produced a meaningless -69% in an earlier attempt.
    Warmup: trend is 0 and delta_pct is NaN until the first band break."""
```

### 3.5 `strategy.py` — gates, staleness, entry timing, exits, simulation

```python
@dataclasses.dataclass(frozen=True)
class StrategyParams:
    # --- gates ---
    quadrant_threshold: float = 0.5    # LONG needs osc <= -x ; SHORT needs osc >= +x
    delta_threshold:    float = 20.0   # LONG needs delta_pct >= +x ; SHORT <= -x
                                       # SWEPT, never a constant -- see U-1
    use_quadrant: bool = True          # G1 magnitude test (the dot is always required)
    use_ribbon:   bool = True          # G2
    use_delta:    bool = True          # G3
    invert_ribbon: bool = False        # A5 arm: require ribbon to OPPOSE the trade

    # --- timing ---
    entry_offset_bars: int  = 1        # "wait 2 closed candles, signal bar counts as one"
                                       #  -> dot on bar N, entry at CLOSE of bar N+1
    max_signal_age_bars: int = 3       # dead if strictly older than this
    zero_cross_kills:   bool = True    # dead if osc crossed zero since the dot

    # --- position ---
    allow_pyramiding: bool = False     # one open position at a time
    allow_shorts:     bool = True
    fee_bps:          float = 10.0     # 0.10% per side, taker
    slippage_bps:     float = 5.0      # 0.05% per side, applied against the position

@dataclasses.dataclass(frozen=True)
class ExitModel:
    exit_id:      str                  # stable identifier, e.g. "atrbeyond0.5_R2_t42"
    stop_kind:    str                  # "fixed_pct" | "atr_from_entry" | "atr_beyond_extreme"
    stop_param:   float
    target_kind:  str                  # "fixed_pct" | "r_multiple" | "atr_trail" | "none"
    target_param: float
    time_stop_bars: int | None = 42     # 42 bars of 4h = 7 days ("roughly a one-week hold")
    exit_on_ribbon_flip: bool = False   # close if VIDYA trend flips against the position
    atr_length:   int = 14              # ATR used by exits; INDEPENDENT of VIDYA's ATR(200)
```

**Signals.** `build_signals` joins the two indicator frames and applies the gates.

```python
def build_signals(
    bars: pd.DataFrame,
    tp: pd.DataFrame,          # two_pole.compute output
    vd: pd.DataFrame,          # vidya.compute output
    params: StrategyParams = StrategyParams(),
) -> pd.DataFrame:
    """Index .equals(bars.index). Columns, exactly:
      dot_side      object   "long" | "short" | None   (raw dot, no gates)
      osc           float64  copied through, for the report
      delta_pct     float64  copied through
      trend         int8     copied through
      g1_quadrant   bool     dot present AND magnitude test passes
      g2_ribbon     bool     trend matches (or, if invert_ribbon, opposes) dot_side
      g3_delta      bool     delta test passes
      gates_pass    bool     all ENABLED gates pass (a disabled gate is True)
      signal_side   object   "long"|"short" if gates_pass else None

    Gate definitions -- all three required to take a trade:
      G1 QUADRANT: long  = dot_long  and (not use_quadrant or osc <= -quadrant_threshold)
                   short = dot_short and (not use_quadrant or osc >= +quadrant_threshold)
      G2 RIBBON:   long  needs trend == +1 ; short needs trend == -1
                   (invert_ribbon flips the required sign; trend == 0 NEVER passes)
      G3 DELTA:    long  needs delta_pct >= +delta_threshold
                   short needs delta_pct <= -delta_threshold
                   (NaN delta_pct NEVER passes)
    The DOT is always required. use_quadrant=False relaxes only the MAGNITUDE test
    (equivalent to quadrant_threshold=0.0), it does not admit non-dot bars."""

def resolve_entries(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    params: StrategyParams = StrategyParams(),
) -> list[dict]:
    """Apply entry timing and staleness. Returns a list of pending-entry dicts in
    chronological order, one per signal that survives:
      {"signal_ts": pd.Timestamp,   # bar whose CLOSE printed the dot
       "entry_ts":  pd.Timestamp,   # signal_ts + entry_offset_bars bars
       "side":      "long"|"short",
       "signal_osc":   float,
       "signal_high":  float,       # high of the SIGNAL bar (for short stops)
       "signal_low":   float,       # low  of the SIGNAL bar (for long stops)
       "signal_delta_pct": float,
       "signal_trend": int}

    ENTRY TIMING -- the rule is "wait 2 closed candles after signal, where THE
    SIGNAL BAR COUNTS AS ONE". Therefore a dot confirmed at the close of bar N
    enters at the CLOSE of bar N+1. entry_offset_bars=1 encodes that. The
    alternative reading (N+2) is swept, not assumed -- see U-4.

    STALENESS -- drop the pending entry if, at any bar in (signal_ts, entry_ts]:
      * the bar age exceeds max_signal_age_bars (age of signal bar itself = 0), OR
      * zero_cross_kills and sign(osc) differs from sign(osc at signal_ts).
    Drop if entry_ts falls past the end of `bars`."""
```

**The trade record — the boundary object between `strategy.py` and `ablation.py`.** This schema is frozen; `ablation.py` computes every metric from it and reads nothing else.

```python
TRADE_COLUMNS: dict[str, str] = {
    "trade_id":       "int64",
    "side":           "object",    # "long" | "short"
    "signal_ts":      "datetime64[ns, UTC]",
    "entry_ts":       "datetime64[ns, UTC]",
    "exit_ts":        "datetime64[ns, UTC]",
    "entry_price":    "float64",   # bar close at entry_ts, BEFORE costs
    "exit_price":     "float64",   # BEFORE costs
    "stop_price":     "float64",
    "target_price":   "float64",   # NaN when target_kind == "none"
    "exit_reason":    "object",    # "stop"|"target"|"time"|"ribbon_flip"|"end_of_data"
    "bars_held":      "int64",
    "return_pct":     "float64",   # NET of fees+slippage, signed for direction
    "r_multiple":     "float64",   # net return / |entry-stop| risk, in R
    "mae_pct":        "float64",   # max adverse excursion, GROSS, positive number
    "mfe_pct":        "float64",   # max favourable excursion, GROSS, positive number
    "signal_osc":       "float64",
    "signal_delta_pct": "float64",
    "signal_trend":     "int8",
}

def simulate(
    bars: pd.DataFrame,
    entries: list[dict],
    exit_model: ExitModel,
    params: StrategyParams = StrategyParams(),
) -> pd.DataFrame:
    """Returns a trades DataFrame with EXACTLY TRADE_COLUMNS in that order and
    those dtypes, RangeIndex 0..n-1, sorted by entry_ts. An empty result is a
    zero-row DataFrame with the same columns and dtypes -- NEVER None, never a
    bare DataFrame(). ablation.py depends on that.

    EXECUTION MODEL (fixed, not a parameter):
      * Entry fills at bars.close[entry_ts]. Costs: entry_price is used raw in
        the price columns; fee_bps+slippage_bps are charged to return_pct at
        BOTH entry and exit (total 2*(fee+slip) bps).
      * From the bar AFTER entry_ts onward, each bar is checked intrabar:
          stop first, then target.
        If a single bar's range contains BOTH, assume the STOP filled. This is
        the pessimistic assumption and it is deliberate -- 4h bars cannot
        resolve intrabar order and optimistic resolution manufactures edge.
      * Stop/target fill AT the stop/target price (no gap modelling), except
        when the bar OPENS beyond the level, in which case it fills at the open.
      * Time stop and ribbon-flip exits fill at that bar's CLOSE.
      * end_of_data: open positions are closed at the final bar's close and
        marked "end_of_data". ablation.py reports their count separately and
        MUST NOT let them dominate a cell.
      * One position at a time when allow_pyramiding is False: entries arriving
        while a position is open are DISCARDED (not queued). Count them and
        return the count via simulate.discarded_ (an int attribute set on the
        returned frame's .attrs["discarded_entries"]).

    STOP PLACEMENT:
      fixed_pct:           long  stop = entry*(1 - stop_param)
                           short stop = entry*(1 + stop_param)   [stop_param is a FRACTION, 0.016 = 1.6%]
      atr_from_entry:      long  stop = entry - stop_param*ATR14[entry_ts]
                           short stop = entry + stop_param*ATR14[entry_ts]
      atr_beyond_extreme:  long  stop = signal_low  - stop_param*ATR14[entry_ts]
                           short stop = signal_high + stop_param*ATR14[entry_ts]
        THIS IS THE POINT OF THE EXERCISE. The one live trade was stopped at
        76,233 -- the exact low of a sweep -- and price then ran through its
        take-profit to 82,272. Stops placed beyond the signal bar's extreme are
        the hypothesis under test.

    TARGET PLACEMENT:
      fixed_pct:   long target = entry*(1 + target_param)
      r_multiple:  risk = abs(entry - stop); long target = entry + target_param*risk
      atr_trail:   no fixed target; trail a chandelier stop at
                   (running MFE extreme) -/+ target_param*ATR14, updated at each
                   bar close, never loosening. exit_reason is "target".
      none:        no target; only stop / time / ribbon_flip can close.

    ATR14 is Wilder ATR over exit_model.atr_length, computed inside strategy.py
    from `bars`. It is NOT the VIDYA ATR(200) and must not be imported from vidya.py."""

def run_once(
    bars: pd.DataFrame,
    exit_model: ExitModel,
    params: StrategyParams = StrategyParams(),
    tp_params: "two_pole.TwoPoleParams" = ...,
    vd_params: "vidya.VidyaParams" = ...,
) -> pd.DataFrame:
    """Convenience: two_pole.compute -> vidya.compute -> build_signals ->
    resolve_entries -> simulate. Returns the trades frame. This is what
    ablation.py calls for every grid cell."""
```

### 3.6 `ablation.py` — grid runner, metrics, statistical guards, report

```python
MIN_TRADES_REPORTABLE:  int = 30    # below this, no conclusion may be drawn
MIN_TRADES_DIRECTIONAL: int = 10    # below this, no number is printed at all

@dataclasses.dataclass(frozen=True)
class Arm:
    arm_id:  str
    label:   str
    params:  StrategyParams

def compute_metrics(trades: pd.DataFrame) -> dict:
    """Input: a trades frame matching TRADE_COLUMNS (possibly zero rows).
    Returns a dict with EXACTLY these keys and types (never None; use
    float("nan") for undefined):
      n_trades            int
      n_long              int
      n_short             int
      n_end_of_data       int
      win_rate            float   fraction in [0,1]
      avg_return_pct      float
      median_return_pct   float
      total_return_pct    float   sum of return_pct (NOT compounded; the harness
                                  is fixed-fractional-agnostic and reports sums)
      expectancy_r        float   mean r_multiple  <-- THE headline metric
      expectancy_r_ci_lo  float   95% CI, bootstrap, 10_000 resamples, seed 12345
      expectancy_r_ci_hi  float
      profit_factor       float   sum(wins)/abs(sum(losses)); inf if no losses
      max_drawdown_pct    float   on the cumulative sum of return_pct, positive
      avg_bars_held       float
      avg_mae_pct         float
      avg_mfe_pct         float
      pct_stopped         float   fraction with exit_reason == "stop"
      pct_timed_out       float   fraction with exit_reason == "time"
      sufficiency         str     "reportable" | "inconclusive" | "not_reported"

    sufficiency is derived ONLY from n_trades:
      n >= MIN_TRADES_REPORTABLE           -> "reportable"
      MIN_TRADES_DIRECTIONAL <= n < 30     -> "inconclusive"
      n <  MIN_TRADES_DIRECTIONAL          -> "not_reported"
    The bootstrap CI is computed whenever n >= 2, but rendering must respect
    sufficiency (see §6)."""

def build_arms() -> list[Arm]:
    """Return the ablation arms of §5.1, in the order listed there."""

def build_exit_grid() -> list[ExitModel]:
    """Return the exit models of §5.3, in the order listed there."""

def run_grid(
    bars: pd.DataFrame,
    arms: list[Arm] | None = None,
    exits: list[ExitModel] | None = None,
    *,
    sweeps: dict[str, list] | None = None,   # §5.2; None -> DEFAULT_SWEEPS
    progress: bool = False,
) -> pd.DataFrame:
    """Cartesian product of arms x exits x sweeps. Returns a results DataFrame,
    RangeIndex, columns EXACTLY:
      arm_id str, arm_label str, exit_id str,
      delta_threshold float64, quadrant_threshold float64,
      max_signal_age_bars int64, entry_offset_bars int64, zero_cross_kills bool,
      <then every key of compute_metrics(), same names and types>
    One row per cell. Cells that produce zero trades still get a row, with
    n_trades=0 and sufficiency="not_reported". Never drop empty cells -- an
    empty cell is a finding (it is how "the gates reject everything" shows up).
    Deterministic: identical inputs -> byte-identical output."""

def render_report(
    results: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    top_n: int = 20,
) -> str:
    """Markdown report string. MUST contain, in order:
      1. The provisional-port disclaimer of §6.4, verbatim, as the first block.
      2. Data coverage: first/last bar, bar count, span in days.
      3. THE CENTRAL QUESTION table: for each exit model, the all-gates arm (A0)
         beside the quadrant-only arm (A1) and the inverted-ribbon arm (A5),
         showing expectancy_r with CI and sufficiency for each.
      4. Full grid, sorted by expectancy_r descending, top_n rows, each row
         carrying its sufficiency verdict.
      5. The statistical-honesty verdict block of §6.3.
    Rows with sufficiency != "reportable" MUST be rendered using the exact
    wording of §6.1/§6.2. render_report must never sort a not_reported cell to
    the top of a 'best arm' list -- filter to reportable cells first when
    naming a winner, and say so if that leaves nothing."""
```

### 3.7 `verify.py` — the `--verify` CSV dump and the CLI

The `--verify` dump is a **first-class requirement**, not a nice-to-have. The user's hard-won lesson: *"Do not assume the oscillator port is accurate — synthetic-data tested only, never diffed against a live chart."*

```python
VERIFY_COLUMNS: list[str] = [
    "timestamp",        # ISO-8601 UTC, e.g. 2026-08-30T16:00:00Z -- bar OPEN time
    "open", "high", "low", "close", "volume",
    "osc", "osc_signal", "tint", "dot_long", "dot_short",
    "vidya", "atr200", "upper", "lower", "trend", "trend_flip",
    "leg_id", "buy_vol", "sell_vol", "delta_pct",
    "g1_quadrant", "g2_ribbon", "g3_delta", "gates_pass", "signal_side",
]

def build_verify_frame(
    bars: pd.DataFrame,
    tp_params=..., vd_params=..., st_params=...,
) -> pd.DataFrame:
    """One row per bar, columns exactly VERIFY_COLUMNS, RangeIndex.
    Floats formatted to 6 decimals on write. Booleans written as TRUE/FALSE.
    timestamp written as an ISO-8601 string with a literal trailing Z so it
    pastes into a spreadsheet next to a TradingView export without tz ambiguity."""

def dump_verify_csv(frame: pd.DataFrame, path: str | pathlib.Path) -> None: ...

def check_signal_log(verify_frame: pd.DataFrame, tolerance: float = 0.02) -> pd.DataFrame:
    """Compare against data.SIGNAL_LOG. Returns one row per SIGNAL_LOG entry:
      ts, expected_osc, actual_osc, abs_err, osc_ok, expected_side,
      actual_dot_side, dot_ok, expected_gates_pass, actual_gates_pass, gates_ok
    Missing timestamps yield a row with NaNs and ok=False rather than raising.
    tolerance is ABSOLUTE on the oscillator value: the user's own bot-vs-chart
    agreement was 1.693 vs 1.71 (0.017), so 0.02 is the working band."""

def check_no_lookahead(bars: pd.DataFrame) -> bool:
    """Recompute all indicators on bars.iloc[:k] for several k in the last
    quarter of the series and assert every value at bar k-1 matches the
    full-series value to 1e-9. Returns True on success, raises AssertionError
    naming the first offending column and index on failure. Any indicator that
    fails this is peeking at the future."""

def main(argv: list[str] | None = None) -> int:
    """THE CLI entrypoint. `python -m backtest.verify [args]`. argparse:
      --source {coinbase,cache,fixture,synthetic}   default: cache
      --product        default BTC-USD
      --start / --end  ISO dates
      --fixture NAME   for --source fixture
      --seed / --n / --regime   for --source synthetic
      --verify PATH    write the verify CSV to PATH and run check_signal_log
      --report PATH    run the full grid and write the markdown report
      --check-lookahead
      --min-bars N     default 700; abort with a clear message if fewer
    Returns a process exit code: 0 ok, 1 a check failed, 2 bad args/data.
    Prints check_signal_log as a table to stdout when --verify is used."""
```

**Warmup budget — binding on every module and every test.** ATR(200) plus VIDYA(34) plus the SMA(25)/two-pole(20) chain means indicators are not trustworthy for roughly the first **400 bars**. `run_grid` and `render_report` must **exclude the first 400 bars from trade generation** (`WARMUP_BARS = 400`, defined in `ablation.py`, imported by `verify.py`). A run needs `>= 700` 4h bars (~117 days) to be meaningful; `main` aborts below `--min-bars`. Fixtures must satisfy this.

### 3.8 Import graph (acyclic — do not violate)

```
data.py      -> (stdlib, numpy, pandas, requests[guarded])
two_pole.py  -> data.py            [for BAR_COLUMNS, validate_bars]
vidya.py     -> data.py            [same]
strategy.py  -> data.py, two_pole.py, vidya.py
ablation.py  -> data.py, strategy.py, two_pole.py, vidya.py
verify.py    -> all of the above
tests/       -> all of the above   [integrator-owned; builders do not edit]
```
No module imports `ablation.py` or `verify.py`. No circular imports. `strategy.py` never reimplements indicator math; `ablation.py` never reimplements strategy math.

---

## 4. Open uncertainties — the user's verification checklist

Prioritised. **P0 = a wrong answer here invalidates the whole backtest.** Each has a test the user can run against his own TradingView chart. All CSV references are to the `--verify` dump.

| # | P | Uncertainty | Chart-verifiable test |
|---|---|---|---|
| **U-0** | **P0** | **Is the oscillator port correct at all?** Synthetic-tested only, never diffed against a live chart. | Run `--verify`. Open the CSV beside the TradingView chart. Check the four `SIGNAL_LOG` bars: 2026-08-07 04:00 → +0.694 short, 08-13 12:00 → −1.087 long, 08-22 08:00 → +1.690 short, 08-30 16:00 → −0.975 long; plus the anchor purple dot at +0.135 on 2026-07-31 20:00. **Every one must match within ±0.02.** If any fails, stop — no other result means anything. |
| **U-1** | **P0** | **The ±20% delta threshold is wrong.** It was chosen against a per-bar reading; the harness computes a cumulative-per-leg reading. Observed real cumulative values: +27, +46, +54, +86, +132, +179%. A ±20% gate on those is nearly a no-op. | At each of the four signal bars, read the ribbon's delta % off the chart and compare to the CSV `delta_pct`. Aug 22 must read ≈ **+132%**, Aug 30 ≈ **+53.88%**, Aug 13 must be **negative**. If they match, the threshold sweep (0/10/20/30/50/80) tells you the real gate level. If they do not match, the accumulation semantics are wrong, not the threshold. |
| **U-2** | P1 | **Tint rule: slope or osc/signal cross?** Chart evidence suggested slope (teal appeared one bar *before* the dot), which the cross rule cannot produce. Default = `"slope"`. | Find any dot on the chart. Look at the bar immediately before it. If the line is already the dot's colour on that prior bar, slope is right. Repeat on 3 dots. **Does not affect trades** — the dot is the signal, the tint is diagnostic — so this is P1, not P0. |
| **U-3** | P1 | **VIDYA trend flip: band break or line cross?** Default = band break (`close` crossing `upper`/`lower`). The alternative is `close` crossing the VIDYA line itself. | Find a chart bar where the ribbon changes colour. Read that bar's `close`, `vidya`, `upper`, `lower` from the CSV. If the flip bar's close is beyond the band, band break is right; if it merely crossed the line while staying inside the band, set `trend_rule="line"`. **This shifts every `leg_id` and therefore every `delta_pct`, so it is high-leverage despite being P1.** |
| **U-4** | P1 | **Entry timing: N+1 or N+2?** "Wait 2 closed candles, the signal bar counts as one" reads as entry at the close of N+1. It is genuinely ambiguous. | Not chart-verifiable — it is a rules question. Both are swept (`entry_offset_bars ∈ {1,2}`). The user decides which he actually traded; if the two produce materially different expectancy, that difference is a fragility warning about the whole signal. |
| **U-5** | P2 | **Filter length 20 vs script default 15.** The user's chart is customised to 20. | Open the indicator settings on his chart and read the value. One click. If it is 15, the whole `SIGNAL_LOG` comparison in U-0 must be re-run. |
| **U-6** | P2 | **Bar timestamp convention.** This harness labels bars by OPEN time (Coinbase convention). TradingView also labels by open time on crypto, but a mismatch shifts every comparison by one bar. | In the CSV, the row labelled `2026-08-30T16:00:00Z` must have the same OHLC as the chart candle whose tooltip reads 16:00 UTC. If it matches the 20:00 candle instead, everything is off by one. |
| **U-7** | P2 | **Staleness: is `max_signal_age_bars=3` inclusive?** "Dead if more than 3 bars old" — implemented as `age > 3` with the signal bar at age 0. | Rules question, swept over {1,2,3,5}. Note that with `entry_offset_bars=1` the staleness rule almost never binds; it only matters if the entry offset is raised. |
| **U-8** | P2 | **Hold duration.** "4h is roughly a one-week hold" → `time_stop_bars=42`. | Check on the chart how long a typical post-dot move takes to exhaust. Swept over {21, 42, 84}. |
| **U-9** | P3 | **Fees and slippage.** Assumed 10bps fee + 5bps slippage per side. | Check his actual Coinbase fee tier. At 4h frequency this is small, but it flips marginal cells. |
| **U-10** | P3 | **BTC confluence filter is untested.** Out of scope for a BTC-only backtest. | Must be re-verified before any of these results are applied to alts. |

---

## 5. Ablation arms, sweeps, and the exit grid

### 5.1 Ablation arms

The **dot is always required** — it is the signal event. The arms vary the gates around it.

| arm_id | label | Configuration |
|---|---|---|
| `A0` | `all_gates` | `use_quadrant=True, use_ribbon=True, use_delta=True` — the user's live rules. The baseline under test. |
| `A1` | `quadrant_only` | `use_quadrant=True, use_ribbon=False, use_delta=False` — the dot plus the ±0.5 zone, nothing else. **This is the arm that would have taken the Aug 13 long.** |
| `A2` | `quadrant_ribbon` | `use_quadrant=True, use_ribbon=True, use_delta=False` — isolates the ribbon's contribution. |
| `A3` | `quadrant_delta` | `use_quadrant=True, use_ribbon=False, use_delta=True` — isolates delta's contribution. |
| `A4` | `dot_only` | `use_quadrant=False, use_ribbon=False, use_delta=False` — every zero-line-gated dot. The rawest form of the signal. |
| `A5` | `inverted_ribbon` | `use_quadrant=True, use_ribbon=True, invert_ribbon=True, use_delta=False` — **requires the ribbon to OPPOSE the trade. This is the direct test of the central question:** if A5 beats A2, the gates are rejecting the good trades. |
| `A6` | `ribbon_delta_only` | `use_quadrant=False, use_ribbon=True, use_delta=True` — trend-following filter with no zone requirement. |
| `A7` | `random_baseline` | Entries drawn at random with `seed=777`, matched in count and long/short ratio to A0, same exit model. **The null hypothesis.** Any arm that does not beat A7 has demonstrated nothing. |

### 5.2 Parameter sweeps

```python
DEFAULT_SWEEPS: dict[str, list] = {
    "delta_threshold":     [0.0, 10.0, 20.0, 30.0, 50.0, 80.0],   # U-1
    "quadrant_threshold":  [0.0, 0.25, 0.5, 0.75, 1.0],
    "entry_offset_bars":   [1, 2],                                 # U-4
    "max_signal_age_bars": [3],                                    # widen to [1,2,3,5] only on demand
    "zero_cross_kills":    [True],
}
```
A sweep key is applied to an arm only where it is live: `delta_threshold` does nothing when `use_delta=False`. `run_grid` must **deduplicate** cells that differ only in a dead parameter, so a disabled gate does not inflate the grid or the multiple-comparison count. Report the deduplicated cell count.

### 5.3 The exit grid

`stop_param` for `fixed_pct` is a **fraction** (0.016 = 1.6%). For ATR kinds it is an ATR **multiple**. `atr_length=14` throughout.

| exit_id | stop_kind | stop_param | target_kind | target_param | time_stop_bars | ribbon_flip |
|---|---|---|---|---|---|---|
| `live_bracket` | `fixed_pct` | 0.016 | `fixed_pct` | 0.031 | 42 | False |
| `pct1.6_R2` | `fixed_pct` | 0.016 | `r_multiple` | 2.0 | 42 | False |
| `pct3.0_R2` | `fixed_pct` | 0.030 | `r_multiple` | 2.0 | 42 | False |
| `atr1.5_R2` | `atr_from_entry` | 1.5 | `r_multiple` | 2.0 | 42 | False |
| `atr2.5_R2` | `atr_from_entry` | 2.5 | `r_multiple` | 2.0 | 42 | False |
| `beyond0.25_R2` | `atr_beyond_extreme` | 0.25 | `r_multiple` | 2.0 | 42 | False |
| `beyond0.5_R1` | `atr_beyond_extreme` | 0.50 | `r_multiple` | 1.0 | 42 | False |
| `beyond0.5_R2` | `atr_beyond_extreme` | 0.50 | `r_multiple` | 2.0 | 42 | False |
| `beyond0.5_R3` | `atr_beyond_extreme` | 0.50 | `r_multiple` | 3.0 | 42 | False |
| `beyond1.0_R2` | `atr_beyond_extreme` | 1.00 | `r_multiple` | 2.0 | 42 | False |
| `beyond0.5_trail3` | `atr_beyond_extreme` | 0.50 | `atr_trail` | 3.0 | 42 | False |
| `beyond0.5_timeonly` | `atr_beyond_extreme` | 0.50 | `none` | 0.0 | 42 | False |
| `beyond0.5_flip` | `atr_beyond_extreme` | 0.50 | `none` | 0.0 | 42 | **True** |
| `beyond0.5_R2_t21` | `atr_beyond_extreme` | 0.50 | `r_multiple` | 2.0 | 21 | False |
| `beyond0.5_R2_t84` | `atr_beyond_extreme` | 0.50 | `r_multiple` | 2.0 | 84 | False |

`live_bracket` reproduces the one real trade (entry 77,474 / SL 76,235 / TP 79,850, R:R 1.89). It exists so the harness can be checked against a known real outcome and so the `beyond*` family has something to beat.

**The `beyond*` family is the hypothesis.** The live trade was stopped at 76,233 — the exact low of a sweep — and price then ran to 82,272, through its take-profit. If stops beyond the signal-bar extreme do not outperform `fixed_pct`, that hypothesis is dead and should be reported as dead.

---

## 6. Statistical-honesty rules

These are not stylistic. They are the difference between a backtest and a story.

```python
MIN_TRADES_REPORTABLE  = 30   # below this, no conclusion may be drawn
MIN_TRADES_DIRECTIONAL = 10   # below this, no number is printed at all
```

### 6.1 Fewer than 10 trades — print no statistics

Exact wording, with `{n}` and `{cell}` substituted:

> **NOT REPORTED — {cell}: {n} trades.** Below the 10-trade floor. No win rate, expectancy, or profit factor is shown for this cell, because any statistic computed on {n} trades would be indistinguishable from noise. This cell is evidence about nothing.

### 6.2 10–29 trades — print the number, forbid the conclusion

> **INCONCLUSIVE — {cell}: {n} trades** (30 required for a conclusion). Point estimate: expectancy {expectancy_r:.3f}R, 95% CI [{lo:.3f}, {hi:.3f}]. **This sample cannot distinguish this arm from the baseline and must not be used to make a trading decision.** The interval is shown so the reader can see how wide it is, not so the point estimate can be quoted.

### 6.3 The report-level verdict block

`render_report` emits exactly one of these three, chosen mechanically:

**(a) No cell reaches 30 trades:**
> **NO CONCLUSION IS AVAILABLE FROM THIS RUN.** No arm produced 30 or more trades over {n_bars} bars ({days} days). The central question — whether the ribbon and delta gates add or destroy expectancy — **is not answered by this data.** The correct next step is more history, not a different parameter. Do not act on the ranking below; it is sorted noise.

**(b) Reportable cells exist but the best arm's CI overlaps the baseline's:**
> **NO ARM SEPARATES FROM BASELINE.** The best reportable arm is {arm} at {exp:.3f}R (95% CI [{lo:.3f}, {hi:.3f}], n={n}); the baseline {base_arm} is {base_exp:.3f}R (95% CI [{blo:.3f}, {bhi:.3f}], n={bn}). **These intervals overlap.** On this data the gates are neither shown to add expectancy nor shown to destroy it. A difference in point estimates without separated intervals is not a finding.

**(c) Separation exists:**
> **SEPARATION FOUND.** {arm} at {exp:.3f}R (95% CI [{lo:.3f}, {hi:.3f}], n={n}) does not overlap {base_arm} at {base_exp:.3f}R (95% CI [{blo:.3f}, {bhi:.3f}], n={bn}). **This is a single un-corrected comparison drawn from {k} grid cells; at that many cells, some separation is expected by chance alone.** Treat this as a hypothesis to test forward, not a validated edge.

### 6.4 Mandatory report header — verbatim, first block of every report

> **PROVISIONAL — NOT VERIFIED AGAINST A LIVE CHART.**
> The Two-Pole Oscillator and Volumatic VIDYA in this harness are ports. They have been tested against synthetic data only. Until the `--verify` CSV has been diffed against the user's TradingView chart and the four signal-log bars match within ±0.02 (checklist item U-0), **every number in this report is provisional and may be measuring a bug rather than a market.**
> Additionally, the Delta Volume threshold of ±20% was chosen against a per-bar reading, while this harness computes the correct cumulative-per-leg reading (checklist item U-1). Any result quoted at a ±20% threshold is quoting a gate that is probably close to a no-op.

### 6.5 Further binding rules

- **Never name a "best" arm from a non-reportable cell.** Filter to `sufficiency == "reportable"` before ranking. If that leaves nothing, say so with §6.3(a).
- **Always report `n_end_of_data`.** A cell where most trades were force-closed at the end of the data has not measured its exit model.
- **Always report the grid cell count** next to any winner, so the multiple-comparisons problem is visible on the same line as the claim.
- **Never compound returns across trades.** Report the sum of `return_pct` and expectancy in R. Compounding a backtest with no position-sizing model manufactures a curve.
- **An empty cell is a result.** If the gates reject every signal, that is the answer to the central question — report it, do not suppress it.

---

## 7. Build rules for the six agents

1. You own exactly one file. **Do not create, edit, or delete another agent's file.** `tests/` belongs to the integrator.
2. Implement the signatures in §3 **exactly** — names, argument order, defaults, return types, column names, column order, dtypes. A renamed column breaks another agent's file.
3. If §3 is ambiguous for your module, implement the literal wording and record the ambiguity in your module docstring under `OPEN QUESTIONS:`. Do not improvise a different interface.
4. Every public function gets a docstring restating its exact output schema.
5. Call `data.validate_bars(bars)` on entry to any function that takes a BarFrame.
6. No network calls anywhere except `data.fetch_1h_coinbase`. **Coinbase is blocked in this sandbox (403 on CONNECT). Do not attempt to fetch, and do not route around the block.** Test against `synthetic_bars` and fixtures.
7. Determinism: same inputs → same outputs. Seed every RNG explicitly.
8. Respect `WARMUP_BARS = 400`. Do not generate trades in the warmup region.
9. `strategy.py` must not read the `tint` column. Trade the dot, not the colour.
10. No test may assume a single sharp candle moves the oscillator — it is heavily smoothed (a −2% candle moved it by 0.001). Use sustained multi-bar excursions.

---

### What still needs doing once tooling is restored

- Write the above to `/home/user/clawpump-products/backtest/SPEC.md`.
- Create empty `/home/user/clawpump-products/backtest/__init__.py`.
- `data.py`'s owner must also create `backtest/fixtures/` and `backtest/tests/` (the latter empty, for the integrator).
- I could not inspect the repo, so I could not check for pre-existing `backtest/` files or an existing oscillator port that should be reused rather than rewritten. **Someone should check that before the six builders start** — if the user's working bot code is already in this repo, `two_pole.py` and `vidya.py` should port from it rather than from the Pine description, since that code is the thing that matched his chart three times.