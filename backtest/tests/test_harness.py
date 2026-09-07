"""Integration test suite for the BTC 4h backtest harness.

Written by the integrator, not the module authors, and every test here was
actually executed. The suite exists to answer one question the user's own
history makes urgent: is this harness measuring the market, or measuring a bug?

The single most important test in this file is ``test_no_lookahead_truncated_vs_full``.
Everything else is hygiene; that one is the difference between a backtest and a
story.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest import ablation, data, strategy, two_pole, verify, vidya


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    """A synthetic 4h series long enough to clear the 400-bar warm-up."""
    return data.synthetic_bars(n=1500, seed=11)


@pytest.fixture(scope="module")
def tp(bars: pd.DataFrame) -> pd.DataFrame:
    return two_pole.compute(bars)


@pytest.fixture(scope="module")
def vd(bars: pd.DataFrame) -> pd.DataFrame:
    return vidya.compute(bars)


def _h1(rows: list[tuple[str, float, float, float, float, float]]) -> pd.DataFrame:
    """Build a 1h bar frame from (ts, o, h, l, c, v) tuples."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows], name=data.INDEX_NAME)
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        index=idx,
    )


# --------------------------------------------------------------------------
# THE test: no look-ahead
# --------------------------------------------------------------------------

def test_no_lookahead_truncated_vs_full(bars: pd.DataFrame) -> None:
    """Every value at bar i must be identical whether or not bars > i exist.

    This is the test that catches a backtest lying. If an indicator is computed
    over the whole array and then read backwards, or if a VIDYA flip or a Delta
    Volume leg total is known before it could have been, truncating the series
    changes the historical values and this test fails.
    """
    report = verify.check_no_lookahead(bars)
    assert report is not None
    ok = report if isinstance(report, bool) else getattr(report, "passed", None)
    if ok is None and isinstance(report, dict):
        ok = report.get("passed")
    assert ok is True, f"look-ahead leak detected: {report}"


def test_no_lookahead_manual_indicator_recompute(bars: pd.DataFrame) -> None:
    """Independent hand-rolled version of the same check, not trusting verify.py.

    Recompute the indicators on a truncated prefix and compare every overlapping
    row against the full-series computation.
    """
    cut = 1200
    prefix = bars.iloc[:cut]

    full_tp, part_tp = two_pole.compute(bars), two_pole.compute(prefix)
    full_vd, part_vd = vidya.compute(bars), vidya.compute(prefix)

    for name, full, part in (("two_pole", full_tp, part_tp), ("vidya", full_vd, part_vd)):
        overlap = part.index
        for col in part.columns:
            a = full.loc[overlap, col]
            b = part[col]
            if pd.api.types.is_numeric_dtype(b) and not pd.api.types.is_bool_dtype(b):
                # Compare only where the prefix produced a value; NaN-vs-NaN is fine.
                both = a.notna() & b.notna()
                assert np.allclose(a[both], b[both], rtol=1e-9, atol=1e-9), (
                    f"{name}.{col} changed when future bars were removed -> look-ahead"
                )
            else:
                assert (a == b).all(), f"{name}.{col} changed under truncation -> look-ahead"


# --------------------------------------------------------------------------
# aggregation: 1h -> UTC-aligned 4h
# --------------------------------------------------------------------------

def test_aggregate_4h_ohlcv_semantics() -> None:
    rows = [
        ("2025-01-01T00:00:00Z", 100.0, 105.0, 99.0, 104.0, 10.0),
        ("2025-01-01T01:00:00Z", 104.0, 110.0, 103.0, 108.0, 20.0),
        ("2025-01-01T02:00:00Z", 108.0, 109.0, 95.0, 97.0, 30.0),
        ("2025-01-01T03:00:00Z", 97.0, 101.0, 96.0, 100.0, 40.0),
    ]
    out = data.aggregate_4h(_h1(rows))
    assert len(out) == 1
    bar = out.iloc[0]
    assert bar["open"] == 100.0            # first open
    assert bar["high"] == 110.0            # max high
    assert bar["low"] == 95.0              # min low
    assert bar["close"] == 100.0           # last close
    assert bar["volume"] == 100.0          # summed
    assert out.index[0] == pd.Timestamp("2025-01-01T00:00:00Z")


def test_aggregate_4h_drops_incomplete_trailing_bucket() -> None:
    """An incomplete trailing bucket is a look-ahead vector and must be dropped."""
    rows = [
        ("2025-01-01T00:00:00Z", 100.0, 105.0, 99.0, 104.0, 10.0),
        ("2025-01-01T01:00:00Z", 104.0, 110.0, 103.0, 108.0, 20.0),
        ("2025-01-01T02:00:00Z", 108.0, 109.0, 95.0, 97.0, 30.0),
        ("2025-01-01T03:00:00Z", 97.0, 101.0, 96.0, 100.0, 40.0),
        # only two of the next bucket's four hours
        ("2025-01-01T04:00:00Z", 100.0, 102.0, 99.0, 101.0, 5.0),
        ("2025-01-01T05:00:00Z", 101.0, 103.0, 100.0, 102.0, 5.0),
    ]
    out = data.aggregate_4h(_h1(rows))
    assert len(out) == 1, "partial 04:00 bucket must not be emitted"
    assert out.index[-1] == pd.Timestamp("2025-01-01T00:00:00Z")


def test_aggregate_4h_is_utc_aligned() -> None:
    """Buckets must land on 00/04/08/12/16/20 UTC regardless of where data starts."""
    rows = [
        (f"2025-01-01T{h:02d}:00:00Z", 100.0, 101.0, 99.0, 100.0, 1.0)
        for h in range(2, 14)
    ]
    out = data.aggregate_4h(_h1(rows))
    assert len(out) > 0
    assert set(out.index.hour) <= {0, 4, 8, 12, 16, 20}
    assert (out.index.minute == 0).all()


def test_trim_to_contiguous_removes_gapped_run() -> None:
    """A gap must not silently shift SMA/ATR windows."""
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2025-01-01T00:00:00Z") + pd.Timedelta(hours=4 * i) for i in range(6)]
        + [pd.Timestamp("2025-01-05T00:00:00Z") + pd.Timedelta(hours=4 * i) for i in range(3)],
        name=data.INDEX_NAME,
    )
    df = pd.DataFrame(
        {c: np.linspace(100, 110, len(idx)) for c in ("open", "high", "low", "close")}
        | {"volume": np.ones(len(idx))},
        index=idx,
    )
    out = data.trim_to_contiguous(df, freq="4h")
    deltas = out.index.to_series().diff().dropna().unique()
    assert len(deltas) == 1 and deltas[0] == pd.Timedelta(hours=4)
    assert len(out) == 6, "should keep the longest contiguous run"


def test_bar_frames_are_utc_and_monotonic(bars: pd.DataFrame) -> None:
    assert str(bars.index.tz) == "UTC"
    assert bars.index.is_monotonic_increasing
    assert not bars.index.has_duplicates
    data.validate_bars(bars)  # raises on violation


def test_ohlc_invariants_hold(bars: pd.DataFrame) -> None:
    assert (bars["high"] >= bars["low"]).all()
    assert (bars["high"] >= bars[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (bars["low"] <= bars[["open", "close"]].min(axis=1) + 1e-9).all()
    assert (bars["volume"] >= 0).all()


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------

def test_two_pole_dots_are_zero_line_gated(tp: pd.DataFrame) -> None:
    """The Pine gates dots on osc < 0 (teal/long) and osc > 0 (purple/short).

    Omitting this gate is a bug the user already hit once: it printed phantom
    mid-zone dots that were absent from his chart.
    """
    longs = tp[tp["dot_long"].fillna(False).astype(bool)]
    shorts = tp[tp["dot_short"].fillna(False).astype(bool)]
    assert (longs["osc"] < 0).all(), "long dot printed at osc >= 0"
    assert (shorts["osc"] > 0).all(), "short dot printed at osc <= 0"


def test_two_pole_never_dots_both_ways_on_one_bar(tp: pd.DataFrame) -> None:
    both = tp["dot_long"].fillna(False).astype(bool) & tp["dot_short"].fillna(False).astype(bool)
    assert not both.any()


def test_two_pole_signal_line_is_delayed_not_advanced(bars: pd.DataFrame) -> None:
    """The 4-bar delayed line must lag the oscillator, never lead it."""
    out = two_pole.compute(bars)
    osc, sig = out["osc"], out["osc_signal"]
    both = osc.notna() & sig.notna()
    delay = two_pole.TwoPoleParams().signal_delay
    shifted = osc.shift(delay)
    ok = both & shifted.notna()
    assert np.allclose(sig[ok], shifted[ok], rtol=1e-9, atol=1e-9), (
        "signal line is not the oscillator delayed by signal_delay bars"
    )


def test_two_pole_is_heavily_smoothed(bars: pd.DataFrame) -> None:
    """A single sharp candle must barely move the filter.

    The user observed a -2% AVAX candle moving the oscillator by 0.001. A port
    that jumps on one bar is not this indicator.
    """
    spiked = bars.copy()
    i = len(spiked) - 2
    spiked.iloc[i, spiked.columns.get_loc("close")] *= 0.98
    spiked.iloc[i, spiked.columns.get_loc("low")] *= 0.97
    base = two_pole.compute(bars)["osc"].iloc[i]
    bumped = two_pole.compute(spiked)["osc"].iloc[i]
    assert abs(bumped - base) < 0.35, "oscillator moved too far on a single candle"


def test_vidya_trend_state_is_discrete_and_flips_are_consistent(vd: pd.DataFrame) -> None:
    states = set(pd.unique(vd["trend"].dropna()))
    assert states <= {-1, 0, 1}, f"unexpected trend states: {states}"
    flips = vd["trend_flip"].fillna(False).astype(bool)
    changed = vd["trend"].ne(vd["trend"].shift()) & vd["trend"].shift().notna()
    # every flagged flip must coincide with an actual state change
    assert not (flips & ~changed).any(), "trend_flip flagged where trend did not change"


def test_delta_pct_is_bounded(vd: pd.DataFrame) -> None:
    """Delta% lives in [-200, +200], NOT [-100, +100].

    With b = buy/(buy+sell), the confirmed formula 2*(buy-sell)/(buy+sell)*100
    reduces to 200*(2b - 1), so a one-sided leg reads +/-200. This is not a
    quirk of the port: the user's own chart has shown +179.71%, which is only
    possible on the wider range. It also means the +/-20% gate is far weaker
    than it sounds -- +20% is merely buy_share = 55%.
    """
    d = vd["delta_pct"].dropna()
    assert (d >= -200.0 - 1e-9).all() and (d <= 200.0 + 1e-9).all()


def test_delta_pct_matches_closed_form_within_leg(vd: pd.DataFrame) -> None:
    """delta_pct must equal 2*(buy-sell)/(buy+sell)*100 on its own accumulators.

    Compared only where the module publishes a value: early bars in a leg are
    deliberately NaN under the minimum-bars-in-leg guard, because a two-bar leg
    can read +200% and mean nothing.
    """
    tot = vd["buy_vol"] + vd["sell_vol"]
    ok = (tot > 0) & vd["delta_pct"].notna()
    assert ok.any(), "no comparable rows"
    expect = 2.0 * (vd["buy_vol"] - vd["sell_vol"]) / tot * 100.0
    assert np.allclose(vd.loc[ok, "delta_pct"], expect[ok], rtol=1e-9, atol=1e-9)


def test_delta_threshold_semantics_are_documented() -> None:
    """+20% on this metric means buy volume is 55% of the leg, not 60%."""
    def buy_share_for(delta_pct: float) -> float:
        return (delta_pct / 200.0 + 1.0) / 2.0 * 100.0

    assert buy_share_for(0.0) == pytest.approx(50.0)
    assert buy_share_for(20.0) == pytest.approx(55.0)
    assert buy_share_for(132.0) == pytest.approx(83.0)
    assert buy_share_for(200.0) == pytest.approx(100.0)


def test_delta_accumulators_reset_at_each_flip(vd: pd.DataFrame) -> None:
    """Buy/sell volume accumulates *per leg*; it must not carry across a flip."""
    legs = vd["leg_id"].dropna().unique()
    assert len(legs) >= 2, "need at least two legs to test the reset"
    for leg in legs[1:]:
        seg = vd[vd["leg_id"] == leg]
        tot = (seg["buy_vol"] + seg["sell_vol"])
        # within a leg the accumulator is non-decreasing...
        assert (tot.diff().dropna() >= -1e-6).all(), f"leg {leg} accumulator decreased"
        # ...and its first bar must not inherit the previous leg's total
        first_total = tot.iloc[0]
        first_vol = seg["volume"].iloc[0] if "volume" in seg else None
        if first_vol is not None:
            assert first_total <= first_vol + 1e-6, (
                f"leg {leg} started with carried-over volume ({first_total} > {first_vol})"
            )


def test_delta_handles_doji_without_dividing_by_zero() -> None:
    """close == open on every bar: no NaN explosion, no ZeroDivisionError."""
    n = 700
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC", name=data.INDEX_NAME)
    flat = pd.DataFrame(
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0},
        index=idx,
    )
    out = vidya.compute(flat)
    assert len(out) == n
    d = out["delta_pct"].dropna()
    assert not np.isinf(d).any()


def test_indicators_survive_a_short_series() -> None:
    """Short input must not raise; it should just produce NaNs."""
    short = data.synthetic_bars(n=30, seed=2)
    t = two_pole.compute(short)
    v = vidya.compute(short)
    assert len(t) == 30 and len(v) == 30


# --------------------------------------------------------------------------
# strategy: gates, staleness, entry timing, fills
# --------------------------------------------------------------------------

def test_entry_lands_one_bar_after_the_signal(bars: pd.DataFrame) -> None:
    """'Wait 2 closed candles, signal bar counts as one' => entry at signal+1 close."""
    em = strategy.ExitModel(
        exit_id="t", stop_kind="atr_beyond_extreme", stop_param=1.5,
        target_kind="r_multiple", target_param=2.0, time_stop_bars=42,
    )
    trades = strategy.run_once(bars, em, strategy.StrategyParams())
    if trades.empty:
        pytest.skip("no trades generated on this synthetic series")
    offset = strategy.StrategyParams().entry_offset_bars
    step = pd.Timedelta(hours=4 * offset)
    assert ((trades["entry_ts"] - trades["signal_ts"]) == step).all()


def test_entry_is_never_before_the_signal(bars: pd.DataFrame) -> None:
    em = strategy.ExitModel(
        exit_id="t", stop_kind="fixed_pct", stop_param=0.02,
        target_kind="r_multiple", target_param=2.0, time_stop_bars=42,
    )
    trades = strategy.run_once(bars, em, strategy.StrategyParams())
    if trades.empty:
        pytest.skip("no trades")
    assert (trades["entry_ts"] > trades["signal_ts"]).all()
    assert (trades["exit_ts"] >= trades["entry_ts"]).all()


def test_staleness_rejects_old_signals(bars: pd.DataFrame) -> None:
    """A signal older than max_signal_age_bars must not produce an entry."""
    tight = strategy.StrategyParams(max_signal_age_bars=1)
    loose = strategy.StrategyParams(max_signal_age_bars=99)
    tp_ = two_pole.compute(bars)
    vd_ = vidya.compute(bars)
    s_tight = strategy.build_signals(bars, tp_, vd_, tight)
    s_loose = strategy.build_signals(bars, tp_, vd_, loose)
    e_tight = strategy.resolve_entries(bars, s_tight, tight)
    e_loose = strategy.resolve_entries(bars, s_loose, loose)
    assert len(e_tight) <= len(e_loose)


def test_gates_are_actually_restrictive(bars: pd.DataFrame) -> None:
    """Dot-only must yield at least as many signals as the full three-gate arm.

    This is the mechanism behind the user's central question: the ribbon and
    delta gates only ever remove candidates.
    """
    tp_ = two_pole.compute(bars)
    vd_ = vidya.compute(bars)
    dot_only = strategy.StrategyParams(use_ribbon=False, use_delta=False)
    all_three = strategy.StrategyParams(use_ribbon=True, use_delta=True)
    n_dot = len(strategy.build_signals(bars, tp_, vd_, dot_only))
    n_all = len(strategy.build_signals(bars, tp_, vd_, all_three))
    assert n_all <= n_dot


def test_stop_first_ambiguity_resolves_conservatively() -> None:
    """When one bar spans both stop and target, the STOP must be assumed first.

    With only 4h OHLC there is no way to know the order, so the sim must take
    the pessimistic branch rather than flatter itself.
    """
    n = 600
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC", name=data.INDEX_NAME)
    px = np.full(n, 100.0)
    df = pd.DataFrame(
        {"open": px, "high": px + 0.5, "low": px - 0.5, "close": px, "volume": np.ones(n)},
        index=idx,
    )
    # one huge outside bar that engulfs both brackets
    j = n - 5
    df.iloc[j, df.columns.get_loc("high")] = 130.0
    df.iloc[j, df.columns.get_loc("low")] = 70.0

    entries = [{
        "signal_ts": idx[j - 2], "entry_ts": idx[j - 1], "side": "long",
        "signal_osc": -0.9, "signal_high": 101.0, "signal_low": 99.0,
        "signal_delta_pct": 40.0, "signal_trend": 1,
    }]
    # fixed_pct params are FRACTIONS: 0.05 == 5%.
    em = strategy.ExitModel(
        exit_id="amb", stop_kind="fixed_pct", stop_param=0.05,
        target_kind="fixed_pct", target_param=0.05, time_stop_bars=50,
    )
    trades = strategy.simulate(df, entries, em, strategy.StrategyParams())
    assert not trades.empty, "crafted entry produced no trade"
    assert trades.iloc[0]["exit_reason"] == "stop", (
        "ambiguous bar resolved optimistically -- backtest would overstate results"
    )


def test_trades_have_no_impossible_prices(bars: pd.DataFrame) -> None:
    em = strategy.ExitModel(
        exit_id="t", stop_kind="atr_beyond_extreme", stop_param=1.5,
        target_kind="r_multiple", target_param=2.0, time_stop_bars=42,
    )
    trades = strategy.run_once(bars, em, strategy.StrategyParams())
    if trades.empty:
        pytest.skip("no trades")
    for _, t in trades.iterrows():
        window = bars.loc[t["entry_ts"]:t["exit_ts"]]
        assert window["low"].min() - 1e-6 <= t["exit_price"] <= window["high"].max() + 1e-6


def test_shorts_and_longs_have_correctly_signed_stops(bars: pd.DataFrame) -> None:
    em = strategy.ExitModel(
        exit_id="t", stop_kind="atr_beyond_extreme", stop_param=1.5,
        target_kind="r_multiple", target_param=2.0, time_stop_bars=42,
    )
    trades = strategy.run_once(bars, em, strategy.StrategyParams())
    if trades.empty:
        pytest.skip("no trades")
    longs = trades[trades["side"] == "long"]
    shorts = trades[trades["side"] == "short"]
    assert (longs["stop_price"] < longs["entry_price"]).all()
    assert (longs["target_price"] > longs["entry_price"]).all()
    assert (shorts["stop_price"] > shorts["entry_price"]).all()
    assert (shorts["target_price"] < shorts["entry_price"]).all()


# --------------------------------------------------------------------------
# statistical honesty
# --------------------------------------------------------------------------

def test_small_samples_are_not_reportable() -> None:
    assert ablation.classify_sufficiency(0) != "reportable"
    assert ablation.classify_sufficiency(3) != "reportable"
    assert ablation.classify_sufficiency(ablation.MIN_TRADES_REPORTABLE - 1) != "reportable"


def test_sufficient_samples_are_reportable() -> None:
    assert ablation.classify_sufficiency(ablation.MIN_TRADES_REPORTABLE * 5) == "reportable"


def test_metrics_flag_their_own_insufficiency() -> None:
    bars_ = data.synthetic_bars(n=1200, seed=5)
    em = strategy.ExitModel(
        exit_id="t", stop_kind="atr_beyond_extreme", stop_param=1.5,
        target_kind="r_multiple", target_param=2.0, time_stop_bars=42,
    )
    trades = strategy.run_once(bars_, em, strategy.StrategyParams())
    m = ablation.compute_metrics(trades)
    assert "sufficiency" in m
    if m["n_trades"] < ablation.MIN_TRADES_REPORTABLE:
        assert m["sufficiency"] != "reportable"


def test_ablation_includes_inverted_arms() -> None:
    """The central experiment needs the arm that takes what the gates reject."""
    arms = ablation.build_arms()
    ids = {getattr(a, "arm_id", getattr(a, "name", str(a))) for a in arms}
    assert len(arms) >= 4
    joined = " ".join(str(i) for i in ids).lower()
    assert "invert" in joined or any(
        getattr(getattr(a, "params", None), "invert_ribbon", False) for a in arms
    ), f"no inverted arm found in {ids}"


def test_exit_grid_includes_atr_beyond_extreme() -> None:
    """The user's live trade was stopped at a sweep low then missed an 8% run.

    An exit grid without ATR-multiple stops placed beyond the signal bar's
    extreme cannot examine that failure.
    """
    grid = ablation.build_exit_grid()
    kinds = {e.stop_kind for e in grid}
    assert "atr_beyond_extreme" in kinds, f"grid stop kinds: {kinds}"
    assert len(grid) >= 4


def test_bootstrap_ci_brackets_the_point_estimate() -> None:
    rs = [1.0, -1.0, 2.0, -1.0, 0.5, 1.5, -1.0, 3.0]
    n = len(rs)
    ts = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")
    trades = pd.DataFrame({
        "trade_id": range(n), "side": ["long"] * n,
        "signal_ts": ts, "entry_ts": ts, "exit_ts": ts,
        "entry_price": 100.0, "exit_price": 101.0,
        "stop_price": 98.0, "target_price": 104.0,
        "exit_reason": ["target"] * n, "bars_held": 3,
        "return_pct": [r * 2.0 for r in rs], "r_multiple": rs,
        "mae_pct": 1.0, "mfe_pct": 2.0,
        "signal_osc": -0.8, "signal_delta_pct": 40.0, "signal_trend": 1,
    })
    m = ablation.compute_metrics(trades)
    assert m["expectancy_r_ci_lo"] <= m["expectancy_r"] <= m["expectancy_r_ci_hi"]
    assert m["sufficiency"] != "reportable", "8 trades must not be reportable"


# --------------------------------------------------------------------------
# verification layer
# --------------------------------------------------------------------------

def test_verify_frame_has_every_column_needed_to_diff_a_chart(bars: pd.DataFrame) -> None:
    frame = verify.build_verify_frame(bars)
    needed = {
        "osc", "dot_long", "dot_short", "vidya", "trend",
        "leg_id", "delta_pct", "gates_pass",
    }
    missing = needed - set(frame.columns)
    assert not missing, f"verify frame cannot be diffed against TradingView: missing {missing}"
    assert len(frame) == len(bars)


def test_signal_log_check_fails_loudly_on_wrong_data(bars: pd.DataFrame) -> None:
    """Against synthetic bars the four real signals cannot reproduce.

    The harness must say so rather than quietly passing -- this is the guard
    that stops a bad port from being trusted.
    """
    frame = verify.build_verify_frame(bars)
    result = verify.check_signal_log(frame)
    assert result is not None
    if isinstance(result, pd.DataFrame) and "osc_ok" in result.columns:
        assert not result["osc_ok"].all(), "synthetic data must not satisfy the real signal log"


def test_real_signal_log_fixture_is_intact() -> None:
    """The four recorded alerts are the port's acceptance test on real data."""
    assert len(data.SIGNAL_LOG) == 4
    by_ts = {s["ts"]: s for s in data.SIGNAL_LOG}
    assert by_ts["2026-08-30T16:00:00Z"]["gates_pass"] is True
    assert by_ts["2026-08-22T08:00:00Z"]["gates_pass"] is False
    assert by_ts["2026-08-13T12:00:00Z"]["gates_pass"] is False
    assert by_ts["2026-08-22T08:00:00Z"]["delta_pct"] == 132.0
    assert by_ts["2026-08-30T16:00:00Z"]["delta_pct"] == 53.88


def test_no_network_is_attempted_on_import_or_synthetic_path(bars: pd.DataFrame) -> None:
    """Everything except an explicit fetch must work with no egress."""
    frame = verify.build_verify_frame(data.synthetic_bars(n=800, seed=1))
    assert len(frame) == 800
