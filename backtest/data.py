"""Data layer for the BTC 4h backtest harness.

Owns: the canonical BarFrame contract, Coinbase 1h fetch, UTC-aligned 1h->4h
aggregation, contiguity trimming, the on-disk 1h cache, the deterministic
synthetic generator, the checked-in fixtures, and the real signal log.

Import-safe: no network, no disk access, no printing at import time.

OPEN QUESTIONS (recorded per SPEC.md build rule 3 -- the literal wording of the
contract was implemented and the ambiguity is recorded rather than improvised
around):

  Q1. SPEC says ``aggregate_4h`` "Returns a validated 4h BarFrame", but
      ``load_bars`` orders the pipeline as
      ``fetch -> aggregate_4h -> trim_to_contiguous -> validate_bars``.
      Real Coinbase 1h data has gaps, so a 4h frame straight out of
      aggregation can legitimately be non-contiguous -- that is precisely why
      ``trim_to_contiguous`` runs after it. A strict ``validate_bars`` inside
      ``aggregate_4h`` would therefore raise on exactly the data the pipeline
      exists to repair. RESOLUTION: ``aggregate_4h`` validates every part of
      the contract except index contiguity (schema, dtypes, tz, ordering,
      4h epoch alignment, OHLC invariants, finiteness). The contiguity
      requirement is discharged by ``trim_to_contiguous``. Same reasoning for
      ``fetch_1h_coinbase``'s 1h output.

  Q2. SPEC gives ``start``/``end`` on ``fetch_1h_coinbase`` and ``load_bars``
      as literal ``...``. Implemented literally: ``Ellipsis`` is the default
      and acts as a sentinel meaning "resolve a sensible window"
      (``end`` = now floored to the hour, ``start`` = end - DEFAULT_HISTORY_DAYS).
      ``None`` is accepted as a synonym. DEFAULT_HISTORY_DAYS is 720, chosen so
      a default call clears the >= 700 bar minimum with room for ATR(200)
      warmup; it is not specified anywhere in SPEC.

  Q3. SPEC requires ``load_fixture("btc_4h_signal_log")`` to return REAL BTC
      bars covering the four SIGNAL_LOG events. Real market data cannot be
      obtained in the dev sandbox (Coinbase egress is blocked, 403 on CONNECT),
      and synthetic bars can never reproduce osc=+0.694 on 2026-08-07, so a
      fabricated fixture would be worse than no fixture -- it would silently
      turn the U-0 regression check into a check of nothing. RESOLUTION: the
      fixture is NOT checked in from this sandbox. ``build_signal_log_fixture``
      is provided for the user to run once on his VPS, where the network works;
      ``load_fixture`` raises FileNotFoundError naming the exact path and that
      command until he does. ``build_signal_log_fixture`` is an addition to the
      SPEC surface, not a change to it.

  Q4. "Cache covers [start, end]" is not defined in SPEC. Implemented as
      ``cached.index.min() <= start and cached.index.max() >= end - 1h``,
      i.e. the last 1h bar whose OPEN time falls inside the half-open window
      [start, end) must be present.

NOTE ON TIMESTAMPS: every bar is labelled by its OPEN time (Coinbase
convention). A 4h bar labelled 2026-08-30 16:00 covers [16:00, 20:00) and
closes at 20:00. See checklist item U-6.
"""

from __future__ import annotations

import logging
import pathlib
import time
from typing import Any, Final

import numpy as np
import pandas as pd

try:  # import-guarded per SPEC 3.0.1: requests is needed only for live fetches.
    import requests as _requests  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - exercised only where requests is absent
    _requests = None  # type: ignore[assignment]

__all__ = [
    "BAR_COLUMNS",
    "BAR_DTYPES",
    "INDEX_NAME",
    "BAR_FREQ_4H",
    "BAR_FREQ_1H",
    "DEFAULT_HISTORY_DAYS",
    "FIXTURES_DIR",
    "SIGNAL_LOG_FIXTURE",
    "BarSchemaError",
    "DataFetchError",
    "validate_bars",
    "fetch_1h_coinbase",
    "aggregate_4h",
    "trim_to_contiguous",
    "load_bars",
    "synthetic_bars",
    "load_fixture",
    "build_signal_log_fixture",
    "SIGNAL_LOG",
    "CHART_ANCHORS",
]

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# The canonical BarFrame contract (SPEC 3.1). Every other module imports these.
# --------------------------------------------------------------------------

BAR_COLUMNS: list[str] = ["open", "high", "low", "close", "volume"]
BAR_DTYPES: dict[str, str] = {
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
}
INDEX_NAME: str = "timestamp"
BAR_FREQ_4H: str = "4h"
BAR_FREQ_1H: str = "1h"

#: Window used when ``start``/``end`` are left at their ``...`` defaults (Q2).
DEFAULT_HISTORY_DAYS: int = 720

#: Directory holding checked-in fixture BarFrames, resolved relative to this file.
FIXTURES_DIR: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent / "fixtures"

#: The fixture required by SPEC 3.2 for the U-0 regression check.
SIGNAL_LOG_FIXTURE: Final[str] = "btc_4h_signal_log"

_COINBASE_BASE: Final[str] = "https://api.exchange.coinbase.com"
_COINBASE_MAX_CANDLES: Final[int] = 300
_EPOCH: Final[pd.Timestamp] = pd.Timestamp("1970-01-01T00:00:00Z")

# 4h bars per calendar year, used to convert annualised vol/drift to per-bar
# terms in ``synthetic_bars``. 365 days * 6 bars/day.
_BARS_PER_YEAR_4H: Final[float] = 365.0 * 6.0

_REGIMES: Final[tuple[str, ...]] = ("gbm", "trend_up", "trend_down", "chop", "sweep")


class BarSchemaError(ValueError):
    """A DataFrame violates the BarFrame contract of SPEC 3.1."""


class DataFetchError(RuntimeError):
    """Market data could not be obtained from the network or from cache."""


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _epoch_ns(idx: pd.DatetimeIndex) -> np.ndarray:
    """Return the index as int64 nanoseconds since the UTC epoch.

    Written as an explicit subtraction from the epoch Timestamp rather than
    ``.astype("int64")`` / ``.asi8`` so it behaves identically across pandas 1.x
    and 2.x on tz-aware indexes.
    """
    return (idx - _EPOCH).to_numpy().astype("timedelta64[ns]").astype("int64")


def _freq_delta(freq: str) -> pd.Timedelta:
    """Parse a frequency string such as ``"4h"`` into a Timedelta."""
    try:
        delta = pd.Timedelta(freq)
    except Exception as exc:  # pragma: no cover - defensive
        raise BarSchemaError(f"unparseable frequency {freq!r}") from exc
    if delta <= pd.Timedelta(0):
        raise BarSchemaError(f"frequency must be positive, got {freq!r}")
    return delta


def _to_utc(ts: pd.Timestamp | str) -> pd.Timestamp:
    """Coerce a timestamp-like to a tz-aware UTC Timestamp.

    Naive input is interpreted as UTC (SPEC 3.0.3: naive datetimes are a bug,
    so we localise rather than silently assuming local time).
    """
    out = pd.Timestamp(ts)
    if out.tzinfo is None:
        return out.tz_localize("UTC")
    return out.tz_convert("UTC")


def _resolve_window(
    start: pd.Timestamp | str | None,
    end: pd.Timestamp | str | None,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Resolve the ``...``/``None`` sentinels into a concrete [start, end) window (Q2)."""
    if end is Ellipsis or end is None:
        end_ts = pd.Timestamp.now(tz="UTC").floor("h")
    else:
        end_ts = _to_utc(end).floor("h")
    if start is Ellipsis or start is None:
        start_ts = end_ts - pd.Timedelta(days=DEFAULT_HISTORY_DAYS)
    else:
        start_ts = _to_utc(start).floor("h")
    if start_ts >= end_ts:
        raise ValueError(f"start ({start_ts}) must be strictly before end ({end_ts})")
    return start_ts, end_ts


def _validate_schema(df: pd.DataFrame, *, what: str = "bars") -> None:
    """Check everything in the BarFrame contract EXCEPT index contiguity (Q1).

    Raises BarSchemaError naming the first violated invariant.
    """
    if not isinstance(df, pd.DataFrame):
        raise BarSchemaError(f"{what}: expected a pandas DataFrame, got {type(df)!r}")
    if len(df) < 1:
        raise BarSchemaError(f"{what}: len(df) >= 1 required, got 0 rows")

    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        raise BarSchemaError(f"{what}: index must be a DatetimeIndex, got {type(idx)!r}")
    if idx.name != INDEX_NAME:
        raise BarSchemaError(f"{what}: index.name must be {INDEX_NAME!r}, got {idx.name!r}")
    if idx.tz is None:
        raise BarSchemaError(f"{what}: index must be tz-aware UTC, got a naive index")
    offset = pd.Timestamp("2000-01-01", tz=idx.tz).utcoffset()
    if offset != pd.Timedelta(0):
        raise BarSchemaError(f"{what}: index tz must be UTC (zero offset), got {idx.tz!r}")

    if list(df.columns) != BAR_COLUMNS:
        raise BarSchemaError(
            f"{what}: columns must be exactly {BAR_COLUMNS}, got {list(df.columns)}"
        )
    for col, want in BAR_DTYPES.items():
        got = str(df[col].dtype)
        if got != want:
            raise BarSchemaError(f"{what}: column {col!r} must be {want}, got {got}")

    if idx.has_duplicates:
        dup = idx[idx.duplicated()][:1]
        raise BarSchemaError(f"{what}: index has duplicates, first at {dup[0]}")
    if not idx.is_monotonic_increasing:
        ns = _epoch_ns(idx)
        bad = int(np.argmax(np.diff(ns) <= 0)) + 1
        raise BarSchemaError(
            f"{what}: index must be strictly increasing; {idx[bad]} follows {idx[bad - 1]}"
        )

    values = df.to_numpy(dtype="float64", copy=False)
    if not np.isfinite(values).all():
        row, col = np.unravel_index(int(np.argmin(np.isfinite(values))), values.shape)
        raise BarSchemaError(
            f"{what}: non-finite value in column {BAR_COLUMNS[col]!r} at {idx[row]}"
        )

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()

    bad = np.flatnonzero(lo > np.minimum(o, c))
    if bad.size:
        i = int(bad[0])
        raise BarSchemaError(
            f"{what}: low <= min(open, close) violated at {idx[i]} "
            f"(low={lo[i]!r}, open={o[i]!r}, close={c[i]!r})"
        )
    bad = np.flatnonzero(h < np.maximum(o, c))
    if bad.size:
        i = int(bad[0])
        raise BarSchemaError(
            f"{what}: high >= max(open, close) violated at {idx[i]} "
            f"(high={h[i]!r}, open={o[i]!r}, close={c[i]!r})"
        )
    bad = np.flatnonzero(lo > h)
    if bad.size:
        i = int(bad[0])
        raise BarSchemaError(f"{what}: low <= high violated at {idx[i]}")
    bad = np.flatnonzero(v < 0.0)
    if bad.size:
        i = int(bad[0])
        raise BarSchemaError(f"{what}: volume >= 0 violated at {idx[i]} (volume={v[i]!r})")


def _validate_alignment(df: pd.DataFrame, freq: str, *, what: str = "bars") -> None:
    """Check every timestamp sits on an exact epoch multiple of ``freq``.

    For freq="4h" this is equivalent to the SPEC 3.1 requirement that every
    label is one of 00/04/08/12/16/20 UTC with zero minutes/seconds, because
    the UTC epoch itself falls on 00:00.
    """
    step_ns = int(_freq_delta(freq).value)
    ns = _epoch_ns(df.index)
    bad = np.flatnonzero(ns % step_ns != 0)
    if bad.size:
        i = int(bad[0])
        raise BarSchemaError(
            f"{what}: timestamp {df.index[i]} is not aligned to a {freq} epoch boundary"
        )


def _validate_contiguity(df: pd.DataFrame, freq: str, *, what: str = "bars") -> None:
    """Check the index is exactly contiguous at ``freq`` (no gaps, no overlaps)."""
    step_ns = int(_freq_delta(freq).value)
    ns = _epoch_ns(df.index)
    if ns.size < 2:
        return
    diffs = np.diff(ns)
    bad = np.flatnonzero(diffs != step_ns)
    if bad.size:
        i = int(bad[0])
        gap = pd.Timedelta(int(diffs[i]), unit="ns")
        raise BarSchemaError(
            f"{what}: index is not contiguous at {freq}; gap of {gap} between "
            f"{df.index[i]} and {df.index[i + 1]}"
        )


def _coerce_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with exactly BAR_COLUMNS as float64 and the index named/UTC.

    Does not validate; callers validate afterwards. Never mutates the input
    (SPEC 3.0.6).
    """
    out = df.copy()
    missing = [c for c in BAR_COLUMNS if c not in out.columns]
    if missing:
        raise BarSchemaError(f"missing required columns: {missing}")
    out = out.loc[:, BAR_COLUMNS].astype("float64")
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    idx.name = INDEX_NAME
    out.index = idx
    return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def validate_bars(df: pd.DataFrame, freq: str = BAR_FREQ_4H) -> None:
    """Validate a BarFrame against the SPEC 3.1 contract.

    Checks, in order: type, non-empty, DatetimeIndex named ``"timestamp"``,
    tz-aware UTC, exact column set/order ``BAR_COLUMNS`` all float64, no
    duplicate labels, strictly increasing index, exact contiguity at ``freq``,
    epoch alignment at ``freq`` (00/04/08/12/16/20 UTC for 4h), all values
    finite, and the OHLC invariants ``low <= min(open, close)``,
    ``high >= max(open, close)``, ``low <= high``, ``volume >= 0``.

    Args:
        df: The frame to check.
        freq: Bar frequency the index must be contiguous and aligned at.

    Returns:
        None on success.

    Raises:
        BarSchemaError: Naming the first violated invariant.
    """
    _validate_schema(df)
    _validate_contiguity(df, freq)
    _validate_alignment(df, freq)


def fetch_1h_coinbase(
    product_id: str = "BTC-USD",
    start: pd.Timestamp | str = ...,
    end: pd.Timestamp | str = ...,
    *,
    max_retries: int = 5,
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Fetch paginated 1h candles from the Coinbase Exchange REST API.

    ``GET {base}/products/{product_id}/candles?granularity=3600&start=&end=``

    Coinbase caps a response at 300 candles, so the window is walked in
    ascending <=300h pages. Coinbase returns rows as
    ``[time, low, high, open, close, volume]`` in DESCENDING time order; rows
    are reordered to ``BAR_COLUMNS`` and sorted ascending. ``time`` is a UNIX
    second integer, converted with ``pd.to_datetime(unit="s", utc=True)``.

    Coinbase has NO 4h granularity -- ``granularity=14400`` returns HTTP 400
    "Unsupported granularity". Valid values are 60, 300, 900, 3600, 21600,
    86400. This function only ever requests 3600.

    NETWORK IS BLOCKED IN THE DEV SANDBOX (403 on CONNECT). This function must
    never be called from tests; it runs on the user's Ubuntu VPS. Do not
    attempt to route around the block.

    Args:
        product_id: Coinbase product, e.g. ``"BTC-USD"``.
        start: Inclusive window start. ``...`` resolves per Q2.
        end: Exclusive window end. ``...`` resolves to now, floored to the hour.
        max_retries: Attempts per page before giving up.
        timeout: Per-request timeout in seconds.

    Returns:
        A 1h frame with index ``timestamp`` (tz-aware UTC, strictly increasing,
        deduplicated) and columns exactly ``BAR_COLUMNS`` as float64. Gaps in
        Coinbase's own history are preserved, not filled: contiguity is
        discharged downstream by ``trim_to_contiguous`` (see Q1).

    Raises:
        DataFetchError: If ``requests`` is unavailable, or a page still fails
            after ``max_retries`` attempts.
    """
    if _requests is None:
        raise DataFetchError(
            "the 'requests' package is not installed; fetch_1h_coinbase cannot run. "
            "Install it on the VPS, or use load_bars(..., allow_network=False)."
        )

    start_ts, end_ts = _resolve_window(start, end)
    step = pd.Timedelta(hours=_COINBASE_MAX_CANDLES)
    url = f"{_COINBASE_BASE}/products/{product_id}/candles"

    pages: list[pd.DataFrame] = []
    cursor = start_ts
    while cursor < end_ts:
        # Coinbase treats both bounds as inclusive on the candle open time, so a
        # window of [t, t + 299h] yields exactly 300 hourly candles.
        page_end = min(cursor + step - pd.Timedelta(hours=1), end_ts - pd.Timedelta(hours=1))
        params = {
            "granularity": 3600,
            "start": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": page_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        rows = _fetch_page(url, params, max_retries=max_retries, timeout=timeout)
        if rows:
            frame = pd.DataFrame(
                rows, columns=["time", "low", "high", "open", "close", "volume"]
            )
            frame.index = pd.to_datetime(frame["time"], unit="s", utc=True)
            frame.index.name = INDEX_NAME
            pages.append(frame.loc[:, BAR_COLUMNS].astype("float64"))
        cursor = page_end + pd.Timedelta(hours=1)
        # Coinbase public rate limit is ~10 req/s; stay well clear of it.
        time.sleep(0.2)

    if not pages:
        raise DataFetchError(
            f"Coinbase returned no candles for {product_id} in [{start_ts}, {end_ts})"
        )

    out = pd.concat(pages)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.loc[(out.index >= start_ts) & (out.index < end_ts)]
    out = _coerce_frame(out)
    _validate_schema(out, what="fetched 1h bars")
    _validate_alignment(out, BAR_FREQ_1H, what="fetched 1h bars")
    return out


def _fetch_page(
    url: str,
    params: dict[str, Any],
    *,
    max_retries: int,
    timeout: float,
) -> list[list[float]]:
    """Fetch one candle page with exponential backoff. Internal to fetch_1h_coinbase."""
    assert _requests is not None
    last_error: str = ""
    for attempt in range(max_retries):
        try:
            resp = _requests.get(url, params=params, timeout=timeout)
        except Exception as exc:  # network/DNS/TLS failure
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                payload = resp.json()
                if not isinstance(payload, list):
                    last_error = f"unexpected payload type {type(payload)!r}"
                else:
                    return payload
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if attempt < max_retries - 1:
            time.sleep(min(2.0**attempt, 16.0))
    raise DataFetchError(
        f"Coinbase request failed after {max_retries} attempts "
        f"(params={params}): {last_error}"
    )


def aggregate_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a 1h frame into UTC-aligned 4h bars.

    ``df.resample("4h", label="left", closed="left", origin="epoch")`` with
    ``open=first, high=max, low=min, close=last, volume=sum``.
    ``origin="epoch"`` is what guarantees the 00/04/08/12/16/20 UTC buckets
    required by SPEC 3.1; without it pandas anchors on the first observation
    and every bar label silently shifts.

    Any bucket that does not contain exactly 4 source bars is DROPPED -- a
    partial bucket at either end (or either side of a Coinbase gap) must never
    become a bar, because a short bar would corrupt the SMA(25) window that the
    oscillator normalises against.

    Args:
        df_1h: A 1h frame. Must satisfy the BarFrame schema; contiguity is NOT
            required (see Q1), since gap repair is ``trim_to_contiguous``'s job.

    Returns:
        A 4h frame, index ``timestamp`` tz-aware UTC and epoch-aligned to 4h,
        columns exactly ``BAR_COLUMNS`` as float64. May contain gaps where the
        1h input did; call ``trim_to_contiguous`` next.

    Raises:
        BarSchemaError: If the input violates the schema, or if no complete 4h
            bucket could be formed.
    """
    _validate_schema(df_1h, what="1h input to aggregate_4h")
    _validate_alignment(df_1h, BAR_FREQ_1H, what="1h input to aggregate_4h")

    resampler = df_1h.resample(BAR_FREQ_4H, label="left", closed="left", origin="epoch")
    agg = resampler.agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    counts = df_1h["close"].resample(
        BAR_FREQ_4H, label="left", closed="left", origin="epoch"
    ).count()
    agg = agg.loc[counts.reindex(agg.index).to_numpy() == 4]

    if len(agg) == 0:
        raise BarSchemaError(
            "aggregate_4h: no 4h bucket contained exactly 4 source 1h bars; "
            f"input spans {df_1h.index[0]}..{df_1h.index[-1]} ({len(df_1h)} bars)"
        )

    out = _coerce_frame(agg)
    _validate_schema(out, what="aggregated 4h bars")
    _validate_alignment(out, BAR_FREQ_4H, what="aggregated 4h bars")
    return out


def trim_to_contiguous(df: pd.DataFrame, freq: str = BAR_FREQ_4H) -> pd.DataFrame:
    """Return the longest contiguous run of bars at ``freq``.

    A data gap must never silently shift an SMA window -- this is the guard
    against that. On a tie in length, the run that ends latest wins (most
    recent data is the more useful sample).

    Emits a ``logging.warning`` naming the dropped span and bar count whenever
    it trims anything.

    Args:
        df: A frame satisfying the BarFrame schema; may contain gaps.
        freq: The frequency contiguity is measured at.

    Returns:
        A validated BarFrame that is a contiguous slice of ``df``. Returns a
        copy; the input is never mutated.

    Raises:
        BarSchemaError: If the input violates the schema.
    """
    _validate_schema(df, what="input to trim_to_contiguous")

    step_ns = int(_freq_delta(freq).value)
    ns = _epoch_ns(df.index)
    if ns.size == 1:
        out = df.copy()
        validate_bars(out, freq)
        return out

    # Segment boundaries: a diff other than exactly one step starts a new run.
    breaks = np.diff(ns) != step_ns
    seg_id = np.concatenate([[0], np.cumsum(breaks)])
    seg_ids, seg_starts, seg_lengths = np.unique(
        seg_id, return_index=True, return_counts=True
    )
    # np.unique returns segments in ascending id order, which is chronological,
    # so argmax over lengths on the REVERSED array picks the latest-ending run
    # among ties.
    rev = seg_lengths[::-1]
    pick_rev = int(np.argmax(rev))
    pick = len(seg_lengths) - 1 - pick_rev

    lo = int(seg_starts[pick])
    hi = lo + int(seg_lengths[pick])
    out = df.iloc[lo:hi].copy()

    dropped = len(df) - len(out)
    if dropped:
        _LOG.warning(
            "trim_to_contiguous: dropped %d of %d bars to enforce %s contiguity; "
            "kept %s..%s (%d bars), discarded %s..%s",
            dropped,
            len(df),
            freq,
            out.index[0],
            out.index[-1],
            len(out),
            df.index[0],
            df.index[-1],
        )

    validate_bars(out, freq)
    return out


def _cache_paths(cache_dir: str | pathlib.Path, product_id: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Return (parquet_path, csv_path) for a product's 1h cache."""
    base = pathlib.Path(cache_dir)
    return (
        base / f"{product_id}_1h.parquet",
        base / f"{product_id}_1h.csv",
    )


def _read_cache(cache_dir: str | pathlib.Path, product_id: str) -> pd.DataFrame | None:
    """Read the on-disk 1h cache, or return None if there is none."""
    parquet_path, csv_path = _cache_paths(cache_dir, product_id)
    frame: pd.DataFrame | None = None
    if parquet_path.exists():
        try:
            frame = pd.read_parquet(parquet_path)
        except Exception as exc:
            _LOG.warning("could not read parquet cache %s: %s", parquet_path, exc)
            frame = None
    if frame is None and csv_path.exists():
        try:
            raw = pd.read_csv(csv_path)
            idx = pd.to_datetime(raw[INDEX_NAME], utc=True, format="%Y-%m-%dT%H:%M:%SZ")
            frame = raw.drop(columns=[INDEX_NAME])
            frame.index = pd.DatetimeIndex(idx)
        except Exception as exc:
            _LOG.warning("could not read csv cache %s: %s", csv_path, exc)
            frame = None
    if frame is None or len(frame) == 0:
        return None
    out = _coerce_frame(frame)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _write_cache(df_1h: pd.DataFrame, cache_dir: str | pathlib.Path, product_id: str) -> None:
    """Write the 1h cache, preferring parquet and falling back to ISO-8601 CSV."""
    parquet_path, csv_path = _cache_paths(cache_dir, product_id)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df_1h.to_parquet(parquet_path)
        return
    except Exception as exc:
        _LOG.warning("parquet cache unavailable (%s); falling back to CSV", exc)
    out = df_1h.copy()
    out.insert(0, INDEX_NAME, out.index.strftime("%Y-%m-%dT%H:%M:%SZ"))
    out.to_csv(csv_path, index=False)


def load_bars(
    product_id: str = "BTC-USD",
    start: pd.Timestamp | str = ...,
    end: pd.Timestamp | str = ...,
    *,
    cache_dir: str | pathlib.Path = "backtest/.cache",
    allow_network: bool = True,
) -> pd.DataFrame:
    """The one entrypoint the rest of the harness uses for real market data.

    Pipeline: fetch (cache-first) -> ``aggregate_4h`` -> ``trim_to_contiguous``
    -> ``validate_bars``.

    The cache stores 1h bars only, never 4h -- aggregation is cheap and
    re-derivable, and caching 4h would bake in an aggregation bug permanently.
    Cache file is ``{cache_dir}/{product_id}_1h.parquet``, falling back to
    ``.csv`` with an ISO-8601 UTC ``timestamp`` column when pyarrow is absent.

    Args:
        product_id: Coinbase product, e.g. ``"BTC-USD"``.
        start: Inclusive window start; ``...`` resolves per Q2.
        end: Exclusive window end; ``...`` resolves to now floored to the hour.
        cache_dir: Directory holding the 1h cache.
        allow_network: When False, serve from cache only. Tests always pass
            False -- Coinbase is egress-blocked in the dev sandbox.

    Returns:
        A validated 4h BarFrame covering (at most) [start, end).

    Raises:
        DataFetchError: If ``allow_network`` is False and the cache cannot
            cover [start, end), or if the network fetch fails.
        BarSchemaError: If the resulting frame violates the BarFrame contract.
    """
    start_ts, end_ts = _resolve_window(start, end)
    one_hour = pd.Timedelta(hours=1)
    cached = _read_cache(cache_dir, product_id)

    covered = (
        cached is not None
        and cached.index.min() <= start_ts
        and cached.index.max() >= end_ts - one_hour  # Q4
    )

    if covered:
        merged = cached
    elif not allow_network:
        if cached is None:
            raise DataFetchError(
                f"allow_network=False and no cache at "
                f"{_cache_paths(cache_dir, product_id)[0]} (or .csv). "
                f"Populate it on the VPS with load_bars(..., allow_network=True)."
            )
        raise DataFetchError(
            f"allow_network=False and the cache does not cover [{start_ts}, {end_ts}); "
            f"cache spans {cached.index.min()}..{cached.index.max()}"
        )
    else:
        frames: list[pd.DataFrame] = []
        if cached is None:
            frames.append(fetch_1h_coinbase(product_id, start_ts, end_ts))
        else:
            frames.append(cached)
            if cached.index.min() > start_ts:
                frames.append(fetch_1h_coinbase(product_id, start_ts, cached.index.min()))
            if cached.index.max() < end_ts - one_hour:
                frames.append(
                    fetch_1h_coinbase(product_id, cached.index.max() + one_hour, end_ts)
                )
        merged = pd.concat(frames)
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged = _coerce_frame(merged)
        _write_cache(merged, cache_dir, product_id)

    window = merged.loc[(merged.index >= start_ts) & (merged.index < end_ts)]
    if len(window) == 0:
        raise DataFetchError(
            f"no 1h bars available for {product_id} in [{start_ts}, {end_ts})"
        )

    bars = aggregate_4h(window)
    bars = trim_to_contiguous(bars, BAR_FREQ_4H)
    validate_bars(bars, BAR_FREQ_4H)
    return bars


def _ohlc_from_closes(
    closes: np.ndarray,
    start_price: float,
    rng: np.random.Generator,
    wick_frac: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build open/high/low arrays around a close path.

    Opens are continuous (``open[i] == close[i-1]``); wicks are drawn as
    half-normal fractions outside the body. This construction makes
    ``high >= max(open, close)`` and ``low <= min(open, close) <= high``
    true by algebra, satisfying the SPEC 3.2 guarantee unconditionally.
    """
    n = closes.size
    opens = np.empty(n, dtype="float64")
    opens[0] = start_price
    opens[1:] = closes[:-1]
    body_hi = np.maximum(opens, closes)
    body_lo = np.minimum(opens, closes)
    up = np.abs(rng.normal(0.0, wick_frac, n))
    dn = np.abs(rng.normal(0.0, wick_frac, n))
    high = body_hi * (1.0 + up)
    low = body_lo * (1.0 - dn)
    return opens, high, low


def synthetic_bars(
    n: int = 3000,
    *,
    seed: int = 0,
    start: pd.Timestamp | str = "2024-01-01T00:00:00Z",
    start_price: float = 50_000.0,
    annual_vol: float = 0.60,
    drift: float = 0.0,
    regime: str = "gbm",
) -> pd.DataFrame:
    """Generate a deterministic synthetic 4h BarFrame.

    Deterministic given ``seed`` -- a single ``numpy.random.default_rng(seed)``
    is consumed in a fixed order.

    Regimes exist so indicator tests can force a known outcome:

    * ``"gbm"``    -- geometric Brownian motion; honours ``annual_vol`` and
      ``drift`` (both annualised, log terms; per-bar values divide by
      ``365 * 6`` bars/year).
    * ``"trend_up"`` / ``"trend_down"`` -- sustained low-vol trend, sized so the
      VIDYA(34) line's lag behind price exceeds the 2*ATR(200) band by roughly
      5x, producing at least one trend flip. Per-bar drift is
      ``min(0.0015, ln(20)/n)`` so the total excursion is capped at 20x
      regardless of ``n``. These regimes OVERRIDE ``annual_vol`` and ``drift``.
    * ``"chop"``   -- a 120-bar sinusoid of 12% log amplitude plus iid level
      noise. The excursions are sustained across many bars, which is what the
      heavily-smoothed two-pole filter needs (a single sharp candle moves it by
      ~0.001), so this produces oscillator dots in BOTH directions. Overrides
      ``annual_vol`` and ``drift``.
    * ``"sweep"``  -- GBM plus embedded stop-run wicks: at intervals, a bar's
      low is pushed ~0.3% below the prior 20-bar low while its close stays
      above that low. This exercises stop-beyond-extreme exits (the 76,233
      case). Honours ``annual_vol`` and ``drift``.

    Guarantee counts: the trend regimes are only guaranteed to flip for
    ``n >= 400`` (the harness warmup budget), ``"chop"`` is only guaranteed to
    print dots in both directions for ``n >= 300`` (2.5 cycles), and
    ``"sweep"`` embeds its first sweep at bar ~250 so needs ``n >= 300``.

    Args:
        n: Number of 4h bars.
        seed: RNG seed; identical seeds give byte-identical frames.
        start: First bar's OPEN time. Must be 4h epoch-aligned.
        start_price: Price at the first bar's open.
        annual_vol: Annualised log volatility (gbm/sweep only).
        drift: Annualised log drift (gbm/sweep only).
        regime: One of ``"gbm"``, ``"trend_up"``, ``"trend_down"``, ``"chop"``,
            ``"sweep"``.

    Returns:
        A validated 4h BarFrame of length ``n``: contiguous, UTC-aligned, index
        named ``timestamp``, columns exactly ``BAR_COLUMNS`` as float64, with
        ``high >= max(open, close)`` and ``low <= min(open, close)`` always.

    Raises:
        ValueError: On an unknown regime or ``n < 1``.
        BarSchemaError: If ``start`` is not 4h epoch-aligned.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    regime = str(regime).lower()
    if regime not in _REGIMES:
        raise ValueError(f"unknown regime {regime!r}; expected one of {list(_REGIMES)}")

    start_ts = _to_utc(start)
    step = _freq_delta(BAR_FREQ_4H)
    if int((start_ts - _EPOCH).value) % int(step.value) != 0:
        raise BarSchemaError(
            f"synthetic_bars: start {start_ts} is not aligned to a 4h epoch boundary "
            "(must be 00/04/08/12/16/20 UTC on the hour)"
        )

    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start_ts, periods=n, freq=BAR_FREQ_4H, tz="UTC")
    idx.name = INDEX_NAME
    log_p0 = float(np.log(start_price))

    if regime in ("gbm", "sweep"):
        sigma_bar = float(annual_vol) / np.sqrt(_BARS_PER_YEAR_4H)
        mu_bar = float(drift) / _BARS_PER_YEAR_4H
        shocks = rng.normal(mu_bar, sigma_bar, n)
        log_close = log_p0 + np.cumsum(shocks)
        wick_frac = 0.004
    elif regime in ("trend_up", "trend_down"):
        # Cap the total excursion at 20x so long runs stay numerically sane,
        # while keeping per-bar drift large relative to the band width.
        mu_bar = min(0.0015, float(np.log(20.0)) / max(n, 1))
        if regime == "trend_down":
            mu_bar = -mu_bar
        sigma_bar = 0.0025  # deliberately low: a wide band would swallow the trend
        shocks = rng.normal(mu_bar, sigma_bar, n)
        log_close = log_p0 + np.cumsum(shocks)
        wick_frac = 0.0015
    else:  # chop
        period = 120.0  # bars; ~5x the SMA(25) window, so excursions are sustained
        amplitude = 0.12  # log amplitude => ~+/-12% swings
        i = np.arange(n, dtype="float64")
        log_close = log_p0 + amplitude * np.sin(2.0 * np.pi * i / period)
        log_close = log_close + rng.normal(0.0, 0.0025, n)  # iid level noise, not a walk
        wick_frac = 0.003

    closes = np.exp(log_close)
    opens, high, low = _ohlc_from_closes(closes, float(start_price), rng, wick_frac)
    volume = rng.lognormal(mean=np.log(1_000.0), sigma=0.5, size=n)

    if regime == "sweep":
        # Embed stop-run wicks: take out the prior 20-bar low by ~0.3% on a bar
        # whose CLOSE is comfortably back above that low. Only `low` is touched,
        # so the OHLC invariants and the close path are both untouched.
        lookback = 20
        for anchor in range(250, n, 200):
            for i in range(anchor, min(anchor + 50, n)):
                if i < lookback:
                    continue
                prior_min = float(low[i - lookback : i].min())
                if closes[i] > prior_min * 1.002:
                    low[i] = min(float(low[i]), prior_min * 0.997)
                    break

    out = pd.DataFrame(
        {
            "open": opens,
            "high": high,
            "low": low,
            "close": closes,
            "volume": volume,
        },
        index=idx,
    ).astype("float64")

    validate_bars(out, BAR_FREQ_4H)
    return out


def load_fixture(name: str) -> pd.DataFrame:
    """Load a checked-in fixture BarFrame by name from ``backtest/fixtures/``.

    Required fixture names:

    * ``"btc_4h_signal_log"`` -- real BTC 4h bars covering at least
      2026-06-01..2026-09-05 so all four ``SIGNAL_LOG`` events fall inside with
      >= 400 bars of warmup before the first (2026-08-07 04:00). This is the
      U-0 regression anchor.

    Args:
        name: Fixture stem, without extension.

    Returns:
        A validated 4h BarFrame.

    Raises:
        FileNotFoundError: Naming the expected paths (and, for the signal-log
            fixture, the command that builds it -- see Q3: it cannot be created
            in the dev sandbox because Coinbase is egress-blocked and a
            synthetic stand-in would turn the U-0 check into a check of
            nothing).
    """
    parquet_path = FIXTURES_DIR / f"{name}.parquet"
    csv_path = FIXTURES_DIR / f"{name}.csv"

    frame: pd.DataFrame | None = None
    if parquet_path.exists():
        frame = pd.read_parquet(parquet_path)
    elif csv_path.exists():
        raw = pd.read_csv(csv_path)
        idx = pd.to_datetime(raw[INDEX_NAME], utc=True, format="%Y-%m-%dT%H:%M:%SZ")
        frame = raw.drop(columns=[INDEX_NAME])
        frame.index = pd.DatetimeIndex(idx)

    if frame is None:
        hint = ""
        if name == SIGNAL_LOG_FIXTURE:
            hint = (
                "\n\nThis fixture holds REAL BTC bars and cannot be generated offline. "
                "On the VPS (where Coinbase is reachable) run:\n"
                "    python3 -c \"from backtest.data import build_signal_log_fixture; "
                "build_signal_log_fixture()\""
            )
        raise FileNotFoundError(
            f"fixture {name!r} not found; expected {parquet_path} or {csv_path}{hint}"
        )

    out = _coerce_frame(frame)
    validate_bars(out, BAR_FREQ_4H)
    return out


def build_signal_log_fixture(
    *,
    product_id: str = "BTC-USD",
    start: pd.Timestamp | str = "2026-04-01T00:00:00Z",
    end: pd.Timestamp | str = "2026-09-06T00:00:00Z",
    cache_dir: str | pathlib.Path = "backtest/.cache",
) -> pathlib.Path:
    """Materialise the ``btc_4h_signal_log`` fixture from live Coinbase data.

    NOT part of the SPEC 3.2 surface -- added because the fixture cannot be
    created in the dev sandbox (Coinbase returns 403 on CONNECT there) and a
    synthetic stand-in would silently defeat the U-0 regression check. Run this
    ONCE on the user's VPS; thereafter ``load_fixture`` serves it offline.

    The default window starts 2026-04-01 rather than the SPEC minimum of
    2026-06-01 so the first SIGNAL_LOG event (2026-08-07 04:00) has ~768 bars of
    warmup rather than the bare 402, comfortably clearing both
    ``WARMUP_BARS = 400`` and the ``--min-bars 700`` floor.

    Args:
        product_id: Coinbase product.
        start: Fixture window start (inclusive).
        end: Fixture window end (exclusive).
        cache_dir: 1h cache directory to populate/reuse.

    Returns:
        The path actually written.

    Raises:
        DataFetchError: If the fetch fails.
        BarSchemaError: If the fetched data does not cover every SIGNAL_LOG
            timestamp, which would make the fixture useless as an anchor.
    """
    bars = load_bars(product_id, start, end, cache_dir=cache_dir, allow_network=True)

    missing = [
        entry["ts"] for entry in SIGNAL_LOG if _to_utc(entry["ts"]) not in bars.index
    ]
    if missing:
        raise BarSchemaError(
            f"fixture would not cover SIGNAL_LOG timestamps {missing}; "
            f"fetched span is {bars.index[0]}..{bars.index[-1]}"
        )

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = FIXTURES_DIR / f"{SIGNAL_LOG_FIXTURE}.parquet"
    try:
        bars.to_parquet(parquet_path)
        return parquet_path
    except Exception as exc:
        _LOG.warning("parquet unavailable (%s); writing CSV fixture instead", exc)
    csv_path = FIXTURES_DIR / f"{SIGNAL_LOG_FIXTURE}.csv"
    out = bars.copy()
    out.insert(0, INDEX_NAME, out.index.strftime("%Y-%m-%dT%H:%M:%SZ"))
    out.to_csv(csv_path, index=False)
    return csv_path


# --------------------------------------------------------------------------
# Regression anchors (SPEC 3.2). Do not edit without the user's confirmation --
# these are readings from his live bot and his TradingView chart.
# --------------------------------------------------------------------------

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
