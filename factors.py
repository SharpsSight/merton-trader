"""
factors.py — orthogonal confirmation factors.

The trend layer (trend_signals.py) decides DIRECTION and strength. These factors
measure DIFFERENT things and are used only to CONFIRM or ATTENUATE that trend —
never to add independent direction (that would double-count correlated info and
invite RSI-fighting-the-trend errors).

Factor families (deliberately orthogonal to trend):
  - RSI        : momentum / exhaustion  (is the move overextended?)
  - Bollinger  : volatility regime      (stretched vs squeeze)
  - MFI        : volume-weighted flow    (is volume backing the move?)

Design rule: `confirmation()` returns a multiplier in [0.5, 1.0]. It can only
SHRINK the trend score, never enlarge it — factors reduce risk, they don't
manufacture conviction. This keeps the loss-minimizing bias baked in.
"""

from __future__ import annotations
import pandas as pd
from indicators import _wilder


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0
              ) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    pctb = (close - lower) / (upper - lower).replace(0, 1e-9)   # 0..1 inside bands
    bandwidth = (upper - lower) / mid.replace(0, 1e-9)          # squeeze gauge
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower,
                         "pctb": pctb, "bandwidth": bandwidth})


def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    rmf = tp * df["volume"]
    pos = rmf.where(tp.diff() > 0, 0.0)
    neg = rmf.where(tp.diff() < 0, 0.0)
    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum().replace(0, 1e-9)
    mfr = pos_sum / neg_sum
    return 100 - 100 / (1 + mfr)


def confirmation(df: pd.DataFrame, direction: int) -> dict:
    """
    Given the trend DIRECTION (+1/-1/0), return how much to TRUST it.

    Returns {'multiplier': float in [0.5,1.0], 'flags': {...}, 'metrics': {...}}
    multiplier == 1.0  -> factors fully confirm; use full trend score
    multiplier  < 1.0  -> stretched / weak volume / no confirmation -> shrink size
    """
    flags, mult = {}, 1.0
    if direction == 0 or len(df) < 25:
        return {"multiplier": 1.0, "flags": {}, "metrics": {}}

    r = float(rsi(df["close"]).iloc[-1])
    bb = bollinger(df["close"]).iloc[-1]
    m = float(mfi(df).iloc[-1])
    pctb, bw = float(bb["pctb"]), float(bb["bandwidth"])

    if direction == 1:   # long
        if pctb > 1.0:          # price above upper band -> stretched
            mult *= 0.70; flags["stretched_above_band"] = True
        if r > 80:              # overbought
            mult *= 0.85; flags["overbought"] = True
        if m < 50:              # volume not confirming upside
            mult *= 0.80; flags["weak_volume"] = True
    else:                # short
        if pctb < 0.0:
            mult *= 0.70; flags["stretched_below_band"] = True
        if r < 20:
            mult *= 0.85; flags["oversold"] = True
        if m > 50:
            mult *= 0.80; flags["weak_volume"] = True

    # squeeze is informational (breakout pending), not a penalty
    if bw < 0.02:
        flags["squeeze"] = True

    mult = max(0.5, min(1.0, mult))
    return {"multiplier": mult, "flags": flags,
            "metrics": {"rsi": round(r, 1), "pctb": round(pctb, 2),
                        "mfi": round(m, 1), "bandwidth": round(bw, 4)}}
