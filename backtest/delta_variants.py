"""U-1/U-3 diagnostic: recompute cumulative Delta% at chart anchors under
alternative leg/volume rules (VIDYA SMA-15 smoothing, band vs line trend,
flip-bar volume skipped or counted). Usage:
    python3 -m backtest.delta_variants verify_btc_4h.csv [ts ...]
"""
import pandas as pd, numpy as np, sys
from backtest import vidya as V
d = pd.read_csv(sys.argv[1], parse_dates=['timestamp']).set_index('timestamp')
bars = d[['open','high','low','close','volume']].astype(float)
anchors = sys.argv[2:] or ['2026-08-13 12:00','2026-08-22 08:00','2026-08-30 16:00']
close, open_, vol = bars['close'], bars['open'], bars['volume']
line = V.vidya_line(close, 34, 20)
atr = V.wilder_atr(bars['high'], bars['low'], close, 200)
print("expect: neg +132 +53.9")
for smooth in (0, 15):
    vl = line.rolling(smooth).mean() if smooth else line
    up, lo = vl + 2 * atr, vl - 2 * atr
    for rule in ('band', 'line', 'xband'):
        if rule == 'xband':
            # reset on EVERY band crossing, including a re-cross within the same trend
            xu = (close > up) & (close.shift(1) <= up.shift(1))
            xd = (close < lo) & (close.shift(1) >= lo.shift(1))
            flip = xu | xd
        else:
            trend = V.trend_state(close, vl, up, lo, rule)
            flip = (trend != trend.shift(1).fillna(0)) & (trend != 0)
        leg = flip.cumsum()
        for skip in (False, True):
            b = vol.where(close > open_, 0.0); s = vol.where(close < open_, 0.0)
            if skip:
                b = b.where(~flip, 0.0); s = s.where(~flip, 0.0)
            B, S = b.groupby(leg).cumsum(), s.groupby(leg).cumsum()
            delta = 2 * (B - S) / (B + S) * 100
            vals = [delta.get(pd.Timestamp(t, tz='UTC'), np.nan) for t in anchors]
            print(f"sma{smooth:>2} {rule:5} skipflip={str(skip):5}", " ".join(f"{v:+7.1f}" for v in vals))
