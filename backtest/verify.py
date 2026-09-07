"""backtest/verify.py -- the --verify CSV dump and the CLI entrypoint.

This module is the harness's honesty layer. Its single most important job is to
emit a per-bar CSV that the user can put beside his TradingView chart and diff
row by row, because the Two-Pole Oscillator and Volumatic VIDYA in this harness
are PORTS that have only ever been tested against synthetic data. Until that
diff is done (checklist item U-0), every number the harness produces is
provisional and may be measuring a bug rather than a market.

Public surface (SPEC.md sec 3.7):
    VERIFY_COLUMNS
    build_verify_frame(bars, tp_params, vd_params, st_params) -> pd.DataFrame
    dump_verify_csv(frame, path) -> None
    check_signal_log(verify_frame, tolerance=0.02) -> pd.DataFrame
    check_no_lookahead(bars) -> bool
    main(argv=None) -> int

Additive helpers (not in SPEC sec 3.7; they serve checklist items U-0 and U-1
directly and break no contract because nothing else imports this module):
    check_chart_anchors(verify_frame, tolerance=0.02) -> pd.DataFrame
    check_delta_readings(verify_frame, tolerance=1.0) -> pd.DataFrame

OPEN QUESTIONS (SPEC sec 7.3 -- ambiguities implemented literally, recorded here):

  Q1. CLI shape. SPEC sec 3.7 defines `main` as a FLAT flag parser
      (--source/--verify/--report/--check-lookahead/--min-bars). The build
      brief additionally asked for `fetch|verify|backtest|ablate` subcommands.
      Both are implemented: if argv[0] is one of the four subcommand names the
      subcommand parser runs, otherwise the flat SPEC parser runs. The flat
      form is the contract and is what tests should target; the subcommands are
      sugar that lower onto the same namespace. Neither form can shadow the
      other because no SPEC flag is spelled like a subcommand.

  Q2. `timestamp` column dtype in the frame returned by build_verify_frame.
      SPEC says the timestamp is "written as an ISO-8601 string with a literal
      trailing Z", which describes the CSV. It does not say whether the
      in-memory frame carries strings or Timestamps. This module puts the
      ISO-8601 STRING in the frame, so that the frame and the CSV are the same
      artifact and dump_verify_csv cannot introduce a tz ambiguity of its own.
      check_signal_log accepts either representation.

  Q3. `atr200` column name. VERIFY_COLUMNS freezes the name `atr200`, but
      VidyaParams.atr_length is a parameter. The column is always named
      `atr200` and always carries vidya's `atr` column, whatever the length. If
      atr_length is changed from 200 the name lies; see the VERIFY comment at
      the assembly site.

  Q4. `gates_ok` when SIGNAL_LOG carries `gates_pass: None` (the 2026-08-07
      entry). Nothing is asserted there, so gates_ok is vacuously True and the
      printed table shows "n/a" for the expectation. It is not evidence of
      agreement.

This module imports data, two_pole, vidya, strategy and ablation (SPEC sec 3.8).
Nothing imports this module. Import has no side effects: no network, no disk,
no printing.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from typing import Any

import numpy as np
import pandas as pd

from . import ablation, data, strategy, two_pole, vidya

__all__ = [
    "VERIFY_COLUMNS",
    "build_verify_frame",
    "dump_verify_csv",
    "check_signal_log",
    "check_chart_anchors",
    "check_delta_readings",
    "check_no_lookahead",
    "main",
]


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

VERIFY_COLUMNS: list[str] = [
    "timestamp",        # ISO-8601 UTC, e.g. 2026-08-30T16:00:00Z -- bar OPEN time
    "open", "high", "low", "close", "volume",
    "osc", "osc_signal", "tint", "dot_long", "dot_short",
    "vidya", "atr200", "upper", "lower", "trend", "trend_flip",
    "leg_id", "buy_vol", "sell_vol", "delta_pct",
    "g1_quadrant", "g2_ribbon", "g3_delta", "gates_pass", "signal_side",
]

#: Subcommand names recognised as argv[0]. See OPEN QUESTIONS Q1.
SUBCOMMANDS: tuple[str, ...] = ("fetch", "verify", "backtest", "ablate")

#: Compact reminder printed above any CLI output that contains numbers. The
#: full verbatim disclaimer of SPEC sec 6.4 belongs to ablation.render_report;
#: this is a pointer to it, deliberately not a competing second copy.
PROVISIONAL_BANNER: str = (
    "PROVISIONAL -- the oscillator and VIDYA ports are synthetic-data tested "
    "only and have never been diffed against a live chart (checklist U-0). "
    "Every number below may be measuring a bug rather than a market."
)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------
# The verify frame
# --------------------------------------------------------------------------

def build_verify_frame(
    bars: pd.DataFrame,
    tp_params: two_pole.TwoPoleParams = two_pole.TwoPoleParams(),
    vd_params: vidya.VidyaParams = vidya.VidyaParams(),
    st_params: strategy.StrategyParams = strategy.StrategyParams(),
) -> pd.DataFrame:
    """Build the per-bar verification frame: one row per 4h bar, all indicator
    internals exposed, so the user can diff it against TradingView.

    Parameters
    ----------
    bars
        A 4h BarFrame (SPEC sec 3.1). Validated on entry; not mutated.
    tp_params, vd_params, st_params
        Indicator and gate parameters. Defaults are the user's chart settings.

    Returns
    -------
    pd.DataFrame
        RangeIndex 0..len(bars)-1, columns EXACTLY ``VERIFY_COLUMNS`` in that
        order. One row per input bar; no bar is dropped and no warmup row is
        removed -- warmup shows as NaN / False / 0, which is itself information
        the user needs when reading the CSV. Column semantics:

          timestamp    object   ISO-8601 UTC string with a literal trailing Z,
                                the bar's OPEN time (see OPEN QUESTIONS Q2, U-6)
          open..volume float64  passed through from `bars`
          osc          float64  two_pole oscillator
          osc_signal   float64  osc delayed by tp_params.signal_delay bars
          tint         int8     +1 teal / -1 purple / 0 undefined; DIAGNOSTIC
                                ONLY -- no gate, entry or exit reads it
          dot_long     bool     zero-line-gated teal dot
          dot_short    bool     zero-line-gated purple dot
          vidya        float64  the VIDYA line
          atr200       float64  vidya's Wilder ATR (see OPEN QUESTIONS Q3)
          upper/lower  float64  vidya +/- band_mult * atr
          trend        int8     +1 up / -1 down / 0 before the first break
          trend_flip   bool     the discrete ribbon flip event
          leg_id       int64    increments on each flip; starts at 0
          buy_vol      float64  cumulative since the last flip, close>open bars
          sell_vol     float64  cumulative since the last flip, close<open bars
          delta_pct    float64  2*(buy-sell)/(buy+sell)*100, CUMULATIVE PER LEG
          g1_quadrant  bool     dot present AND magnitude test passes
          g2_ribbon    bool     ribbon agrees (or opposes, if invert_ribbon)
          g3_delta     bool     delta test passes
          gates_pass   bool     all ENABLED gates pass
          signal_side  object   "long" | "short" | None

    Raises
    ------
    data.BarSchemaError
        If `bars` is not a valid 4h BarFrame.
    AssertionError
        If any sibling module returns a frame whose index does not match
        `bars.index`. That is a contract violation in the sibling, and it must
        surface loudly here rather than being silently realigned.
    """
    data.validate_bars(bars)

    tp = two_pole.compute(bars, tp_params)
    vd = vidya.compute(bars, vd_params)
    sig = strategy.build_signals(bars, tp, vd, st_params)

    # SPEC sec 3.0 rule 5: every per-bar frame's index must be .equals() to the
    # input index. Assert rather than join, so a misaligned sibling cannot be
    # papered over by pandas alignment and silently shift the whole CSV.
    for name, frame in (("two_pole", tp), ("vidya", vd), ("strategy", sig)):
        assert frame.index.equals(bars.index), (
            f"{name}.compute returned an index that does not match bars.index "
            f"(len {len(frame)} vs {len(bars)}); SPEC sec 3.0 rule 5 violated"
        )

    out = pd.DataFrame(index=pd.RangeIndex(len(bars)))

    # Bar OPEN time, rendered with a literal Z. VERIFY: U-6 -- the row labelled
    # 2026-08-30T16:00:00Z must carry the same OHLC as the chart candle whose
    # tooltip reads 16:00 UTC. If it matches the 20:00 candle instead, every
    # comparison in this file is off by one bar and nothing else is meaningful.
    out["timestamp"] = bars.index.strftime(_ISO_FMT)

    for col in ("open", "high", "low", "close", "volume"):
        out[col] = bars[col].to_numpy()

    for col in ("osc", "osc_signal", "tint", "dot_long", "dot_short"):
        out[col] = tp[col].to_numpy()

    # VERIFY (OPEN QUESTIONS Q3): the CSV column is always named `atr200`
    # because VERIFY_COLUMNS is frozen, but it carries vidya's ATR at whatever
    # length vd_params.atr_length holds. With the default 34/20/2/close chart
    # settings that length is 200 and the name is honest.
    out["vidya"] = vd["vidya"].to_numpy()
    out["atr200"] = vd["atr"].to_numpy()
    for col in ("upper", "lower", "trend", "trend_flip",
                "leg_id", "buy_vol", "sell_vol", "delta_pct"):
        out[col] = vd[col].to_numpy()

    for col in ("g1_quadrant", "g2_ribbon", "g3_delta", "gates_pass", "signal_side"):
        out[col] = sig[col].to_numpy()

    out = out[VERIFY_COLUMNS]
    assert list(out.columns) == VERIFY_COLUMNS, "verify frame column drift"
    return out


def dump_verify_csv(frame: pd.DataFrame, path: str | pathlib.Path) -> None:
    """Write a verify frame to CSV in a spreadsheet-diffable form.

    Formatting rules, chosen so the file can be pasted straight into a sheet
    beside a TradingView export:

      * floats  -> fixed 6 decimal places; NaN -> empty cell
      * bools   -> the literal strings TRUE / FALSE (never True/False/0/1)
      * ints    -> plain integers (tint, trend, leg_id keep their sign)
      * objects -> the value as-is; None / NaN -> empty cell
      * timestamp is already an ISO-8601 string ending in Z and passes through

    Parent directories are created if missing. Returns None.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    out = pd.DataFrame(index=frame.index)
    for col in frame.columns:
        series = frame[col]
        if pd.api.types.is_bool_dtype(series):
            out[col] = np.where(series.to_numpy(dtype=bool), "TRUE", "FALSE")
        elif pd.api.types.is_integer_dtype(series):
            out[col] = series
        elif pd.api.types.is_float_dtype(series):
            out[col] = series.map(lambda v: "" if pd.isna(v) else f"{v:.6f}")
        elif pd.api.types.is_datetime64_any_dtype(series):
            # Tolerated in case a caller hands us Timestamps (OPEN QUESTIONS Q2).
            out[col] = pd.DatetimeIndex(series).strftime(_ISO_FMT)
        else:
            out[col] = series.map(lambda v: "" if pd.isna(v) else str(v))

    out.to_csv(path, index=False)


# --------------------------------------------------------------------------
# Checks against the user's own recorded reality
# --------------------------------------------------------------------------

def _timestamp_lookup(verify_frame: pd.DataFrame) -> dict[pd.Timestamp, int]:
    """Map each bar's UTC Timestamp to its positional row in `verify_frame`.

    Accepts a `timestamp` column of ISO-8601 strings (the normal case, see
    OPEN QUESTIONS Q2) or of tz-aware datetimes.
    """
    col = verify_frame["timestamp"]
    if pd.api.types.is_datetime64_any_dtype(col):
        idx = pd.DatetimeIndex(col)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
    else:
        idx = pd.DatetimeIndex(pd.to_datetime(col, utc=True))
    return {ts: i for i, ts in enumerate(idx)}


def _dot_side(row: pd.Series) -> str | None:
    """Return the RAW dot side on a verify row: "long", "short" or None.

    Reads dot_long / dot_short only. SIGNAL_LOG records raw dots from the
    user's live bot with NO gates applied, so comparing against `signal_side`
    (which is gated) would be comparing two different things.
    """
    if bool(row["dot_long"]):
        return "long"
    if bool(row["dot_short"]):
        return "short"
    return None


def check_signal_log(
    verify_frame: pd.DataFrame,
    tolerance: float = 0.02,
) -> pd.DataFrame:
    """Compare the verify frame against ``data.SIGNAL_LOG`` -- checklist U-0.

    SIGNAL_LOG holds four raw oscillator dots recorded by the user's LIVE BOT,
    plus gate readings taken off his charts. They are the regression anchors:
    if the oscillator port does not reproduce them, no other number in this
    harness means anything.

    Parameters
    ----------
    verify_frame
        Output of :func:`build_verify_frame`.
    tolerance
        ABSOLUTE tolerance on the oscillator value. The user's own bot-vs-chart
        agreement was 1.693 vs 1.71 (an absolute error of 0.017), so 0.02 is
        the working band. It is not a relative tolerance.

    Returns
    -------
    pd.DataFrame
        RangeIndex, one row per SIGNAL_LOG entry in SIGNAL_LOG order, columns:

          ts                   object  the ISO-8601 timestamp as recorded
          expected_osc         float64
          actual_osc           float64  NaN if the bar is absent
          abs_err              float64  NaN if the bar is absent
          osc_ok               bool     abs_err <= tolerance
          expected_side        object   "long" | "short"
          actual_dot_side      object   "long" | "short" | None (RAW dot)
          dot_ok               bool
          expected_gates_pass  object   True | False | None (None = unasserted)
          actual_gates_pass    bool
          gates_ok             bool

        A SIGNAL_LOG timestamp that is not present in `verify_frame` yields a
        row of NaN/None with every *_ok False rather than raising -- a missing
        bar is a finding about coverage, not a crash.

        When expected_gates_pass is None nothing is asserted about the gates
        and gates_ok is vacuously True (OPEN QUESTIONS Q4). That is not
        evidence of agreement.
    """
    lookup = _timestamp_lookup(verify_frame)
    rows: list[dict[str, Any]] = []

    for entry in data.SIGNAL_LOG:
        ts_raw = entry["ts"]
        ts = pd.Timestamp(ts_raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")

        expected_osc = float(entry["osc"])
        expected_side = entry["side"]
        expected_gates = entry.get("gates_pass")

        pos = lookup.get(ts)
        if pos is None:
            rows.append({
                "ts": ts_raw,
                "expected_osc": expected_osc,
                "actual_osc": float("nan"),
                "abs_err": float("nan"),
                "osc_ok": False,
                "expected_side": expected_side,
                "actual_dot_side": None,
                "dot_ok": False,
                "expected_gates_pass": expected_gates,
                "actual_gates_pass": False,
                "gates_ok": False,
            })
            continue

        row = verify_frame.iloc[pos]
        actual_osc = float(row["osc"])
        abs_err = abs(actual_osc - expected_osc) if not pd.isna(actual_osc) else float("nan")
        actual_side = _dot_side(row)
        actual_gates = bool(row["gates_pass"])

        rows.append({
            "ts": ts_raw,
            "expected_osc": expected_osc,
            "actual_osc": actual_osc,
            "abs_err": abs_err,
            "osc_ok": bool(not pd.isna(abs_err) and abs_err <= tolerance),
            "expected_side": expected_side,
            "actual_dot_side": actual_side,
            "dot_ok": bool(actual_side == expected_side),
            "expected_gates_pass": expected_gates,
            "actual_gates_pass": actual_gates,
            # Vacuously True when the log asserts nothing (OPEN QUESTIONS Q4).
            "gates_ok": True if expected_gates is None else bool(actual_gates == expected_gates),
        })

    out = pd.DataFrame(rows, columns=[
        "ts", "expected_osc", "actual_osc", "abs_err", "osc_ok",
        "expected_side", "actual_dot_side", "dot_ok",
        "expected_gates_pass", "actual_gates_pass", "gates_ok",
    ])
    for col in ("expected_osc", "actual_osc", "abs_err"):
        out[col] = out[col].astype("float64")
    for col in ("osc_ok", "dot_ok", "gates_ok", "actual_gates_pass"):
        out[col] = out[col].astype(bool)
    return out


def check_chart_anchors(
    verify_frame: pd.DataFrame,
    tolerance: float = 0.02,
) -> pd.DataFrame:
    """Compare against ``data.CHART_ANCHORS`` -- the second half of U-0.

    CHART_ANCHORS records independent points where the user's bot agreed with
    his TradingView chart, most importantly the purple dot at osc +0.135 on
    2026-07-31 20:00. Entries with ``ts is None`` (the free-standing
    1.693-vs-1.71 agreement note) are reported as skipped, not failed -- they
    carry no timestamp to check against.

    Returns
    -------
    pd.DataFrame
        RangeIndex, columns: ts, event, expected_osc, actual_osc, abs_err,
        actual_dot_side, ok, note.
    """
    lookup = _timestamp_lookup(verify_frame)
    rows: list[dict[str, Any]] = []

    for anchor in data.CHART_ANCHORS:
        ts_raw = anchor.get("ts")
        event = anchor.get("event")
        if ts_raw is None:
            rows.append({
                "ts": None, "event": event,
                "expected_osc": float(anchor.get("osc_chart", float("nan"))),
                "actual_osc": float("nan"), "abs_err": float("nan"),
                "actual_dot_side": None, "ok": True,
                "note": "no timestamp -- informational only, not checked",
            })
            continue

        ts = pd.Timestamp(ts_raw)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        expected_osc = float(anchor.get("osc", float("nan")))
        pos = lookup.get(ts)
        if pos is None:
            rows.append({
                "ts": ts_raw, "event": event, "expected_osc": expected_osc,
                "actual_osc": float("nan"), "abs_err": float("nan"),
                "actual_dot_side": None, "ok": False,
                "note": "bar not present in the loaded data",
            })
            continue

        row = verify_frame.iloc[pos]
        actual_osc = float(row["osc"])
        abs_err = abs(actual_osc - expected_osc) if not pd.isna(actual_osc) else float("nan")
        side = _dot_side(row)
        osc_ok = bool(not pd.isna(abs_err) and abs_err <= tolerance)
        # The one timestamped anchor is a purple (short) dot; if the event name
        # says so, require the dot too.
        dot_required = isinstance(event, str) and "purple" in event
        dot_ok = (side == "short") if dot_required else True
        rows.append({
            "ts": ts_raw, "event": event, "expected_osc": expected_osc,
            "actual_osc": actual_osc, "abs_err": abs_err,
            "actual_dot_side": side, "ok": bool(osc_ok and dot_ok),
            "note": "" if osc_ok and dot_ok else "MISMATCH -- see checklist U-0",
        })

    return pd.DataFrame(rows, columns=[
        "ts", "event", "expected_osc", "actual_osc", "abs_err",
        "actual_dot_side", "ok", "note",
    ])


def check_delta_readings(
    verify_frame: pd.DataFrame,
    tolerance: float = 1.0,
) -> pd.DataFrame:
    """Compare cumulative delta_pct against the user's chart readings -- U-1.

    This is the P0 test of the Delta Volume accumulation semantics, which are
    the thing a previous session got wrong (it produced a meaningless -69% by
    measuring per bar instead of cumulatively per trend leg). Two SIGNAL_LOG
    entries carry a chart-read delta: 2026-08-22 at +132.0 and 2026-08-30 at
    +53.88. The 2026-08-13 entry records no number but the chart showed a
    NEGATIVE delta, so that one is checked for sign only.

    `tolerance` is absolute, in percentage points; 1.0 is loose on purpose
    because these were read off a chart by eye.

    Returns
    -------
    pd.DataFrame
        RangeIndex, columns: ts, expected_delta_pct, expected_sign,
        actual_delta_pct, abs_err, leg_id, ok, note.

    VERIFY: if these do not match, the fault is the ACCUMULATION SEMANTICS
    (sec 2.3) or the leg boundaries (checklist U-3, the band-break-vs-line-cross
    question, which shifts every leg_id and therefore every delta_pct) -- it is
    NOT the +/-20% threshold. Fix the semantics before touching the threshold.
    """
    lookup = _timestamp_lookup(verify_frame)
    rows: list[dict[str, Any]] = []

    for entry in data.SIGNAL_LOG:
        expected = entry.get("delta_pct")
        ribbon = entry.get("ribbon")
        # The Aug 13 entry has no number but the chart showed delta negative.
        expected_sign: int | None = None
        if expected is None and entry.get("gates_pass") is False and ribbon == "short":
            expected_sign = -1
        if expected is None and expected_sign is None:
            continue

        ts = pd.Timestamp(entry["ts"])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        pos = lookup.get(ts)
        if pos is None:
            rows.append({
                "ts": entry["ts"], "expected_delta_pct": expected,
                "expected_sign": expected_sign, "actual_delta_pct": float("nan"),
                "abs_err": float("nan"), "leg_id": -1, "ok": False,
                "note": "bar not present in the loaded data",
            })
            continue

        row = verify_frame.iloc[pos]
        actual = float(row["delta_pct"])
        if expected is not None:
            abs_err = abs(actual - float(expected)) if not pd.isna(actual) else float("nan")
            ok = bool(not pd.isna(abs_err) and abs_err <= tolerance)
            note = "" if ok else "MISMATCH -- accumulation semantics or leg boundaries (U-1/U-3)"
        else:
            abs_err = float("nan")
            ok = bool(not pd.isna(actual) and np.sign(actual) == expected_sign)
            note = "sign-only check" if ok else "SIGN MISMATCH -- expected negative delta (U-1)"

        rows.append({
            "ts": entry["ts"],
            "expected_delta_pct": expected,
            "expected_sign": expected_sign,
            "actual_delta_pct": actual,
            "abs_err": abs_err,
            "leg_id": int(row["leg_id"]),
            "ok": ok,
            "note": note,
        })

    return pd.DataFrame(rows, columns=[
        "ts", "expected_delta_pct", "expected_sign", "actual_delta_pct",
        "abs_err", "leg_id", "ok", "note",
    ])


# --------------------------------------------------------------------------
# Look-ahead check
# --------------------------------------------------------------------------

def _values_match(a: Any, b: Any, atol: float = 1e-9) -> bool:
    """True if two per-bar cell values agree. NaN matches NaN."""
    a_na, b_na = pd.isna(a), pd.isna(b)
    if a_na or b_na:
        return bool(a_na and b_na)
    if isinstance(a, (bool, np.bool_)) or isinstance(b, (bool, np.bool_)):
        return bool(a) == bool(b)
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    try:
        return abs(float(a) - float(b)) <= atol
    except (TypeError, ValueError):
        return a == b


def check_no_lookahead(
    bars: pd.DataFrame,
    *,
    tp_params: two_pole.TwoPoleParams = two_pole.TwoPoleParams(),
    vd_params: vidya.VidyaParams = vidya.VidyaParams(),
    n_checks: int = 5,
    atol: float = 1e-9,
) -> bool:
    """Assert that no indicator value at bar i depends on bars after i.

    Method: recompute every indicator on the truncated prefix ``bars.iloc[:k]``
    for several k spread across the LAST QUARTER of the series, and require
    that the value at bar ``k-1`` -- the final bar of the prefix, the one with
    no future available -- equals the value the full-series computation
    produced at that same bar, to `atol`.

    Truncation is a prefix, so the warmup seeding of every recursive filter
    (the two-pole seed, the VIDYA seed, the Wilder ATR seed) is identical
    between the two runs. Any disagreement is therefore the indicator peeking
    forward, not a seeding artefact.

    Parameters
    ----------
    bars
        A 4h BarFrame. Validated on entry.
    tp_params, vd_params
        Indicator parameters to test under.
    n_checks
        How many truncation points to test.
    atol
        Absolute tolerance; SPEC sec 3.7 specifies 1e-9.

    Returns
    -------
    bool
        True if every checked value matches.

    Raises
    ------
    AssertionError
        Naming the first offending module, column and bar timestamp.
    ValueError
        If `bars` is too short to truncate meaningfully (< 8 bars).
    """
    data.validate_bars(bars)
    n = len(bars)
    if n < 8:
        raise ValueError(f"check_no_lookahead needs at least 8 bars, got {n}")

    full_tp = two_pole.compute(bars, tp_params)
    full_vd = vidya.compute(bars, vd_params)

    start_k = max(2, n - n // 4)
    ks = sorted({int(k) for k in np.linspace(start_k, n, num=max(1, n_checks))})

    for k in ks:
        prefix = bars.iloc[:k]
        part_tp = two_pole.compute(prefix, tp_params)
        part_vd = vidya.compute(prefix, vd_params)
        i = k - 1
        ts = bars.index[i]
        for label, full, part in (("two_pole", full_tp, part_tp),
                                  ("vidya", full_vd, part_vd)):
            for col in full.columns:
                a = full[col].iloc[i]
                b = part[col].iloc[i]
                if not _values_match(a, b, atol=atol):
                    raise AssertionError(
                        f"LOOK-AHEAD DETECTED in {label}.compute: column {col!r} "
                        f"at bar {i} ({ts.strftime(_ISO_FMT)}) is {a!r} when the "
                        f"full {n}-bar series is computed but {b!r} when only the "
                        f"first {k} bars are available. A value at bar i must "
                        f"depend only on bars <= i (SPEC sec 3.0 rule 7)."
                    )
    return True


# --------------------------------------------------------------------------
# Printing helpers
# --------------------------------------------------------------------------

def _cell(value: Any) -> str:
    """Render one table cell for stdout."""
    if value is None:
        return "n/a"
    if isinstance(value, (bool, np.bool_)):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (float, np.floating)):
        return "" if pd.isna(value) else f"{float(value):.4f}"
    return str(value)


def _print_table(title: str, frame: pd.DataFrame, stream: Any = None) -> None:
    """Print a DataFrame as a fixed-width table with a title."""
    out = stream or sys.stdout
    print(f"\n{title}", file=out)
    if frame.empty:
        print("  (no rows)", file=out)
        return
    cols = list(frame.columns)
    rendered = [[_cell(frame.iloc[r][c]) for c in cols] for r in range(len(frame))]
    widths = [max(len(c), *(len(row[j]) for row in rendered)) for j, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(widths[j]) for j, c in enumerate(cols)), file=out)
    print("  " + "  ".join("-" * widths[j] for j in range(len(cols))), file=out)
    for row in rendered:
        print("  " + "  ".join(row[j].ljust(widths[j]) for j in range(len(cols))), file=out)


def _print_coverage(bars: pd.DataFrame) -> None:
    """Print first/last bar, bar count and span so the user can sanity-check
    that he loaded what he thinks he loaded."""
    first, last = bars.index[0], bars.index[-1]
    span_days = (last - first).total_seconds() / 86400.0
    print(f"bars        : {len(bars)}")
    print(f"first bar   : {first.strftime(_ISO_FMT)}  (bar OPEN time)")
    print(f"last bar    : {last.strftime(_ISO_FMT)}  (bar OPEN time)")
    print(f"span        : {span_days:.1f} days")
    print(f"warmup      : first {ablation.WARMUP_BARS} bars are excluded from "
          f"trade generation (ATR200 + VIDYA34 + SMA25/two-pole20 chain)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _add_source_args(parser: argparse.ArgumentParser, *, default_source: str) -> None:
    """Attach the shared data-source arguments to a parser or subparser."""
    parser.add_argument("--source", choices=("coinbase", "cache", "fixture", "synthetic"),
                        default=default_source,
                        help="where bars come from (default: %(default)s)")
    parser.add_argument("--product", default="BTC-USD",
                        help="Coinbase product id (default: %(default)s)")
    parser.add_argument("--start", default=None, help="ISO start, e.g. 2025-01-01")
    parser.add_argument("--end", default=None, help="ISO end, e.g. 2026-09-05")
    parser.add_argument("--cache-dir", default="backtest/.cache",
                        help="1h bar cache directory (default: %(default)s)")
    parser.add_argument("--fixture", default="btc_4h_signal_log",
                        help="fixture name for --source fixture (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=0, help="synthetic RNG seed")
    parser.add_argument("--n", type=int, default=3000, help="synthetic bar count")
    parser.add_argument("--regime", default="gbm",
                        choices=("gbm", "trend_up", "trend_down", "chop", "sweep"),
                        help="synthetic regime (default: %(default)s)")
    parser.add_argument("--min-bars", type=int, default=700,
                        help="abort if fewer bars than this (default: %(default)s)")


def _build_flat_parser() -> argparse.ArgumentParser:
    """The SPEC sec 3.7 flat CLI: `python -m backtest.verify --verify out.csv`."""
    p = argparse.ArgumentParser(
        prog="backtest.verify",
        description="BTC 4h backtest harness: per-bar verification dump, "
                    "look-ahead check and ablation report.",
    )
    _add_source_args(p, default_source="cache")
    p.add_argument("--verify", metavar="PATH", default=None,
                   help="write the per-bar verify CSV to PATH and run the "
                        "signal-log / chart-anchor / delta checks")
    p.add_argument("--report", metavar="PATH", default=None,
                   help="run the full ablation grid and write the markdown report")
    p.add_argument("--check-lookahead", action="store_true",
                   help="assert no indicator peeks at future bars")
    p.add_argument("--top-n", type=int, default=20,
                   help="rows in the report's full-grid table (default: %(default)s)")
    p.add_argument("--tolerance", type=float, default=0.02,
                   help="absolute oscillator tolerance for the signal-log check "
                        "(default: %(default)s)")
    p.add_argument("--progress", action="store_true", help="print grid progress")
    return p


def _build_subcommand_parser() -> argparse.ArgumentParser:
    """The `fetch | verify | backtest | ablate` CLI (OPEN QUESTIONS Q1).

    Each subcommand lowers onto exactly the same namespace the flat parser
    produces, so there is one execution path and no divergence.
    """
    p = argparse.ArgumentParser(
        prog="backtest.verify",
        description="BTC 4h backtest harness (subcommand form; the flag form "
                    "of SPEC sec 3.7 also works).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="load bars (warming the cache) and print coverage")
    _add_source_args(fetch, default_source="coinbase")

    ver = sub.add_parser("verify", help="write the per-bar verify CSV and run the checks")
    _add_source_args(ver, default_source="cache")
    ver.add_argument("--out", dest="verify", metavar="PATH",
                     default="backtest/verify_btc_4h.csv",
                     help="CSV output path (default: %(default)s)")
    ver.add_argument("--check-lookahead", action="store_true")
    ver.add_argument("--tolerance", type=float, default=0.02)

    bt = sub.add_parser("backtest", help="run one arm x one exit model and print metrics")
    _add_source_args(bt, default_source="cache")
    bt.add_argument("--exit-id", default="beyond0.5_R2",
                    help="exit model id from the sec 5.3 grid (default: %(default)s)")
    bt.add_argument("--out", dest="trades_out", metavar="PATH", default=None,
                    help="optional path to write the trades CSV")

    ab = sub.add_parser("ablate", help="run the full grid and write the markdown report")
    _add_source_args(ab, default_source="cache")
    ab.add_argument("--out", dest="report", metavar="PATH", default="backtest/report.md",
                    help="markdown report path (default: %(default)s)")
    ab.add_argument("--top-n", type=int, default=20)
    ab.add_argument("--progress", action="store_true")
    return p


def _load_bars_from_args(args: argparse.Namespace) -> pd.DataFrame:
    """Load a 4h BarFrame according to the parsed source arguments.

    Raises data.DataFetchError / data.BarSchemaError / FileNotFoundError /
    ValueError; `main` turns those into exit code 2.
    """
    source = args.source
    if source == "synthetic":
        return data.synthetic_bars(n=args.n, seed=args.seed, regime=args.regime)
    if source == "fixture":
        return data.load_fixture(args.fixture)

    # coinbase / cache both go through load_bars; only the network flag differs.
    # start/end are passed only when supplied, so data.py's own defaults stand.
    kwargs: dict[str, Any] = {
        "cache_dir": args.cache_dir,
        "allow_network": (source == "coinbase"),
    }
    if getattr(args, "start", None):
        kwargs["start"] = args.start
    if getattr(args, "end", None):
        kwargs["end"] = args.end
    return data.load_bars(args.product, **kwargs)


def _run_verify(args: argparse.Namespace, bars: pd.DataFrame) -> bool:
    """Build and write the verify CSV, then print the three check tables.

    Returns True if every check passed.
    """
    frame = build_verify_frame(bars)
    dump_verify_csv(frame, args.verify)
    print(f"\nwrote {len(frame)} rows to {args.verify}")

    tol = getattr(args, "tolerance", 0.02)
    sig_df = check_signal_log(frame, tolerance=tol)
    anchors_df = check_chart_anchors(frame, tolerance=tol)
    delta_df = check_delta_readings(frame)

    _print_table(f"SIGNAL LOG (U-0, tolerance +/-{tol:g} absolute on osc)", sig_df)
    _print_table("CHART ANCHORS (U-0)", anchors_df)
    _print_table("DELTA READINGS (U-1, cumulative per trend leg)", delta_df)

    sig_ok = bool(sig_df[["osc_ok", "dot_ok", "gates_ok"]].to_numpy().all()) if len(sig_df) else False
    anchor_ok = bool(anchors_df["ok"].to_numpy().all()) if len(anchors_df) else True
    delta_ok = bool(delta_df["ok"].to_numpy().all()) if len(delta_df) else True

    print("")
    if sig_ok and anchor_ok and delta_ok:
        print("ALL CHECKS PASSED. The port reproduces every recorded bot/chart "
              "reading. Now do the human half of U-0: open the CSV beside the "
              "TradingView chart and confirm the OHLC on a handful of rows "
              "lines up bar for bar (U-6).")
    else:
        if not sig_ok:
            print("SIGNAL-LOG CHECK FAILED -- U-0 is unresolved. Stop here. "
                  "No backtest number means anything until the oscillator "
                  "reproduces the four recorded dots within tolerance.")
        if not anchor_ok:
            print("CHART-ANCHOR CHECK FAILED -- see U-0.")
        if not delta_ok:
            print("DELTA CHECK FAILED -- U-1. The fault is the accumulation "
                  "semantics (whole-bar close-vs-open, cumulative since the "
                  "last VIDYA flip) or the leg boundaries (U-3, band break vs "
                  "line cross). It is NOT the +/-20% threshold.")
    return sig_ok and anchor_ok and delta_ok


def _run_backtest(args: argparse.Namespace, bars: pd.DataFrame) -> bool:
    """Run a single exit model over the default (all-gates) arm and print
    metrics, respecting the statistical-honesty floors of SPEC sec 6.

    Returns True (a backtest is not a pass/fail check; failures surface as
    exceptions or as an explicit unknown-exit-id error).
    """
    grid = {em.exit_id: em for em in ablation.build_exit_grid()}
    exit_model = grid.get(args.exit_id)
    if exit_model is None:
        print(f"unknown --exit-id {args.exit_id!r}; available: "
              f"{', '.join(sorted(grid))}", file=sys.stderr)
        return False

    trades = strategy.run_once(bars, exit_model)

    # SPEC sec 3.7: no trade may be generated inside the warmup region. The
    # single-run path enforces the same rule run_grid does, by dropping trades
    # whose SIGNAL bar falls before the warmup boundary.
    if len(bars) > ablation.WARMUP_BARS and len(trades):
        boundary = bars.index[ablation.WARMUP_BARS]
        trades = trades.loc[trades["signal_ts"] >= boundary].reset_index(drop=True)

    metrics = ablation.compute_metrics(trades)
    print(f"\nexit model  : {exit_model.exit_id}")
    print(f"trades      : {metrics['n_trades']} "
          f"({metrics['n_long']} long / {metrics['n_short']} short, "
          f"{metrics['n_end_of_data']} closed at end-of-data)")
    print(f"sufficiency : {metrics['sufficiency']}")

    # SPEC sec 6.1: below the 10-trade floor, print NO statistics at all.
    if metrics["sufficiency"] == "not_reported":
        print(f"\nNOT REPORTED -- {exit_model.exit_id}: {metrics['n_trades']} trades. "
              f"Below the 10-trade floor. No win rate, expectancy, or profit "
              f"factor is shown, because any statistic computed on "
              f"{metrics['n_trades']} trades would be indistinguishable from "
              f"noise. This cell is evidence about nothing.")
    else:
        print(f"expectancy  : {metrics['expectancy_r']:.3f}R "
              f"(95% CI [{metrics['expectancy_r_ci_lo']:.3f}, "
              f"{metrics['expectancy_r_ci_hi']:.3f}])")
        print(f"win rate    : {metrics['win_rate']:.3f}")
        print(f"total ret   : {metrics['total_return_pct']:.2f}% (SUM, not compounded)")
        print(f"profit fac  : {metrics['profit_factor']:.3f}")
        print(f"max dd      : {metrics['max_drawdown_pct']:.2f}%")
        print(f"avg bars    : {metrics['avg_bars_held']:.1f}")
        print(f"stopped     : {metrics['pct_stopped']:.3f}   timed out: "
              f"{metrics['pct_timed_out']:.3f}")
        if metrics["sufficiency"] == "inconclusive":
            print(f"\nINCONCLUSIVE -- {metrics['n_trades']} trades (30 required "
                  f"for a conclusion). This sample cannot distinguish this "
                  f"configuration from the baseline and must not be used to "
                  f"make a trading decision.")

    discarded = trades.attrs.get("discarded_entries")
    if discarded:
        print(f"note        : {discarded} entries discarded while a position "
              f"was already open (allow_pyramiding=False)")

    if getattr(args, "trades_out", None):
        path = pathlib.Path(args.trades_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(path, index=False)
        print(f"wrote {len(trades)} trades to {path}")
    return True


def _run_report(args: argparse.Namespace, bars: pd.DataFrame) -> bool:
    """Run the full ablation grid and write the markdown report."""
    results = ablation.run_grid(bars, progress=getattr(args, "progress", False))
    markdown = ablation.render_report(results, bars, top_n=getattr(args, "top_n", 20))
    path = pathlib.Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    print(f"\nwrote report to {path} ({len(results)} grid cells)")
    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m backtest.verify [args]``.

    Two accepted forms (OPEN QUESTIONS Q1):

      flat (SPEC sec 3.7, the contract):
        python -m backtest.verify --source cache --verify backtest/verify.csv
        python -m backtest.verify --source fixture --report backtest/report.md
        python -m backtest.verify --source synthetic --n 3000 --check-lookahead

      subcommands:
        python -m backtest.verify fetch    --source coinbase --start 2024-01-01
        python -m backtest.verify verify   --out backtest/verify.csv
        python -m backtest.verify backtest --exit-id beyond0.5_R2
        python -m backtest.verify ablate   --out backtest/report.md

    Returns
    -------
    int
        0 -- everything requested ran and every check passed
        1 -- a check failed (signal log, chart anchors, delta, look-ahead)
        2 -- bad arguments, or data that could not be loaded / was too short
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        if argv and argv[0] in SUBCOMMANDS:
            args = _build_subcommand_parser().parse_args(argv)
        else:
            args = _build_flat_parser().parse_args(argv)
            args.command = None
    except SystemExit as exc:  # argparse already printed the message
        return int(exc.code) if exc.code is not None else 2

    # Normalise the namespace so both parser shapes hit one execution path.
    for attr, default in (("verify", None), ("report", None),
                          ("check_lookahead", False), ("top_n", 20),
                          ("tolerance", 0.02), ("progress", False),
                          ("exit_id", None), ("trades_out", None)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    print(PROVISIONAL_BANNER)

    try:
        bars = _load_bars_from_args(args)
    except Exception as exc:  # noqa: BLE001 -- the CLI reports, it does not crash
        print(f"\nfailed to load bars from --source {args.source}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if args.source == "coinbase":
            print("note: Coinbase is egress-blocked in the dev sandbox (403 on "
                  "CONNECT). This path is expected to work only on the VPS.",
                  file=sys.stderr)
        return 2

    print("")
    _print_coverage(bars)

    if len(bars) < args.min_bars:
        print(f"\nABORT: {len(bars)} bars is below --min-bars {args.min_bars}. "
              f"The ATR(200) + VIDYA(34) + SMA(25)/two-pole(20) chain means the "
              f"first {ablation.WARMUP_BARS} bars are untrustworthy, so a run "
              f"needs at least {args.min_bars} 4h bars (~117 days) before any "
              f"result is meaningful. Fetch more history.", file=sys.stderr)
        return 2

    ok = True
    ran_something = False

    if args.command == "fetch":
        ran_something = True  # coverage above is the whole job

    if args.verify:
        ran_something = True
        ok = _run_verify(args, bars) and ok

    if args.check_lookahead:
        ran_something = True
        try:
            check_no_lookahead(bars)
            print("\nLOOK-AHEAD CHECK PASSED: every indicator value at bar i "
                  "depends only on bars <= i.")
        except AssertionError as exc:
            print(f"\n{exc}", file=sys.stderr)
            ok = False

    if args.command == "backtest":
        ran_something = True
        ok = _run_backtest(args, bars) and ok

    if args.report:
        ran_something = True
        ok = _run_report(args, bars) and ok

    if not ran_something:
        print("\nnothing to do -- pass --verify PATH, --report PATH, "
              "--check-lookahead, or use a subcommand "
              f"({'|'.join(SUBCOMMANDS)}).")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
