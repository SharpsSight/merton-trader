"""
indicators.py — trend indicator engine.

Pure functions over an OHLC(V) pandas DataFrame with columns:
    open, high, low, close   (volume optional)
Index is a DatetimeIndex of bar timestamps (ascending).

Each function returns pandas Series/DataFrames aligned to df.index.
No I/O, no state — so the backtest and the live runner call the SAME code
(anti-drift discipline). Correctness is validated in tests/test_indicators.py.

Wilder-style smoothing is done with ewm(alpha=1/period, adjust=False), the
standard convergent form of the original recursive RMA.
"""

from __future__ import annotations
import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RMA (running moving average)."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    return _wilder(true_range(df), period)


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------
def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> pd.DataFrame:
    """MACD line, signal line, histogram."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    ADX with directional indicators.
    Returns columns: adx, plus_di, minus_di.
    ADX measures trend STRENGTH (not direction); +DI/-DI give direction.
    """
    up = df["high"].diff()
    down = -df["low"].diff()

    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    atr_ = _wilder(true_range(df), period)
    plus_di = 100 * _wilder(plus_dm, period) / atr_
    minus_di = 100 * _wilder(minus_dm, period) / atr_

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = _wilder(dx.fillna(0.0), period)

    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
               ) -> pd.DataFrame:
    """
    Supertrend. Returns columns:
        supertrend : the trailing line (usable directly as a stop level)
        direction  : +1 uptrend (bullish), -1 downtrend (bearish)
    """
    hl2 = (df["high"] + df["low"]) / 2.0
    atr_ = atr(df, period)
    upper = hl2 + multiplier * atr_
    lower = hl2 - multiplier * atr_

    n = len(df)
    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = pd.Series(1, index=df.index)   # start assuming up
    st = pd.Series(np.nan, index=df.index)

    close = df["close"]
    for i in range(1, n):
        # carry-forward band logic
        if close.iloc[i - 1] <= final_upper.iloc[i - 1]:
            final_upper.iloc[i] = min(upper.iloc[i], final_upper.iloc[i - 1])
        else:
            final_upper.iloc[i] = upper.iloc[i]

        if close.iloc[i - 1] >= final_lower.iloc[i - 1]:
            final_lower.iloc[i] = max(lower.iloc[i], final_lower.iloc[i - 1])
        else:
            final_lower.iloc[i] = lower.iloc[i]

        # direction flip
        if close.iloc[i] > final_upper.iloc[i - 1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < final_lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]

        st.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]

    return pd.DataFrame({"supertrend": st, "direction": direction})


def parabolic_sar(df: pd.DataFrame, af_step: float = 0.02, af_max: float = 0.20
                  ) -> pd.DataFrame:
    """
    Parabolic SAR (Wilder). Returns columns:
        sar       : the SAR level (usable as a stop-and-reverse stop)
        direction : +1 long/bullish, -1 short/bearish
    """
    high = df["high"].values
    low = df["low"].values
    n = len(df)
    sar = np.zeros(n)
    direction = np.ones(n, dtype=int)

    if n == 0:
        return pd.DataFrame({"sar": [], "direction": []}, index=df.index)

    # init
    trend_up = True
    af = af_step
    ep = high[0]
    sar[0] = low[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]
        if trend_up:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = min(sar[i], low[i - 1], low[max(i - 2, 0)])
            if high[i] > ep:
                ep = high[i]
                af = min(af + af_step, af_max)
            if low[i] < sar[i]:            # flip to down
                trend_up = False
                sar[i] = ep
                ep = low[i]
                af = af_step
        else:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], high[i - 1], high[max(i - 2, 0)])
            if low[i] < ep:
                ep = low[i]
                af = min(af + af_step, af_max)
            if high[i] > sar[i]:           # flip to up
                trend_up = True
                sar[i] = ep
                ep = high[i]
                af = af_step
        direction[i] = 1 if trend_up else -1

    return pd.DataFrame({"sar": sar, "direction": direction}, index=df.index)
