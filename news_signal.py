"""
news_signal.py — abnormal-news-arrival event signal. Pure functions, no I/O.

HYPOTHESIS
----------
When an abnormal cluster of headlines lands on a symbol, the market's own price
reaction inside the news bar carries the sign. We never read the headline. We
read the response to it, then bet on continuation (momentum) or against it
(reversal). This is post-announcement drift at intraday scale.

Deliberately no sentiment model. A lexicon or an LLM classifier trained on
anything after the trade date is a look-ahead channel that is nearly impossible
to audit. The market is the classifier, and its timestamp is unambiguous.

CAUSALITY (the whole ballgame)
------------------------------
A headline published at time t is ACTIONABLE at the close of the first bar whose
close >= t. Not the bar containing t -- that bar may have closed before t.

    news 10:02  ->  bar [10:00,10:05) closes 10:05  ->  known at 10:05
    news 22:00  ->  next session's [09:30,09:35) bar closes 09:35 -> known 09:35

The 22:00 case is the important one: overnight news is first tradeable on the
opening bar, and the reaction we measure is the opening gap. That is exactly
what a live runner sees, and it is why the mapping uses bar CLOSES, not starts.

Signal at bar i uses only bars <= i. Entry fills at open[i+1]. Exit at
open[i+1+H]. One position per symbol at a time (no overlapping events), which
removes the serial dependence that would otherwise inflate every t-statistic.
"""

from __future__ import annotations
import numpy as np
import pandas as pd


def actionable_bar_index(bar_closes: np.ndarray, news_times: np.ndarray) -> np.ndarray:
    """First bar index whose CLOSE is at or after each news timestamp.

    Returns -1 for news after the last bar. Both inputs must be sorted, tz-aware
    or both naive, and comparable as numpy datetime64.
    """
    if len(news_times) == 0:
        return np.array([], dtype=int)
    ix = np.searchsorted(bar_closes, news_times, side="left")
    ix = np.where(ix >= len(bar_closes), -1, ix)
    return ix


def arrival_counts(n_bars: int, bar_ix: np.ndarray) -> np.ndarray:
    """Headlines becoming actionable at each bar."""
    counts = np.zeros(n_bars, dtype=float)
    valid = bar_ix[bar_ix >= 0]
    if len(valid):
        np.add.at(counts, valid, 1.0)
    return counts


def arrival_features(counts: np.ndarray, window: int, baseline_bars: int
                     ) -> tuple[np.ndarray, np.ndarray]:
    """(window_count, expected_in_window) at each bar, both strictly causal.

    The baseline is the trailing mean arrival rate measured over bars that end
    BEFORE the current window opens -- shifted by `window` so the spike being
    tested never contributes to its own baseline.
    """
    s = pd.Series(counts)
    w = s.rolling(window, min_periods=1).sum().values
    base_rate = s.shift(window).rolling(baseline_bars, min_periods=baseline_bars // 4).mean()
    expected = (base_rate * window).fillna(np.inf).values   # unknown baseline -> no shock
    return w, expected


def extract_events(df: pd.DataFrame,
                   counts: np.ndarray,
                   window: int,
                   baseline_bars: int,
                   min_count: float,
                   shock_mult: float,
                   react_thresh: float,
                   horizon: int,
                   mode: int,
                   cost_per_side: float) -> list[dict]:
    """
    Walk bars forward, emit one trade per qualifying event.

    df       : RTH OHLC bars, ascending DatetimeIndex
    counts   : headlines actionable at each bar (from arrival_counts)
    window   : bars over which arrivals are aggregated and reaction is measured
    mode     : +1 momentum (trade with the reaction), -1 reversal (against it)
    horizon  : bars held, exit at open[entry_bar + horizon]

    Entry at open[i+1]. No position is opened while one is open (no overlap).
    """
    o = df["open"].values
    c = df["close"].values
    n = len(df)
    times = df.index.values

    w, expected = arrival_features(counts, window, baseline_bars)

    trades = []
    busy_until = -1
    for i in range(window, n - horizon - 1):
        if i <= busy_until:
            continue
        if w[i] < min_count or w[i] < shock_mult * expected[i]:
            continue

        reaction = c[i] / c[i - window] - 1.0        # known at close of bar i
        if abs(reaction) < react_thresh:
            continue

        direction = int(np.sign(reaction)) * mode
        if direction == 0:
            continue

        entry_i = i + 1
        exit_i = entry_i + horizon
        entry_px = o[entry_i]
        exit_px = o[exit_i]
        if not (np.isfinite(entry_px) and np.isfinite(exit_px)) or entry_px <= 0:
            continue

        gross = direction * (exit_px - entry_px) / entry_px
        trades.append({
            "signal_time": times[i],
            "entry_time": times[entry_i],
            "exit_time": times[exit_i],
            "direction": direction,
            "reaction": float(reaction),
            "n_headlines": float(w[i]),
            "expected": float(expected[i]),
            "gross_ret": float(gross),
            "ret": float(gross - 2.0 * cost_per_side),
        })
        busy_until = exit_i

    return trades


def pooled_stats(rets: np.ndarray) -> dict:
    """mu, sigma, n, se, t for a pooled set of per-trade returns."""
    n = len(rets)
    if n < 2:
        return {"n": n, "mu": 0.0, "sigma": 0.0, "se": 0.0, "t": 0.0}
    mu = float(rets.mean())
    sig = float(rets.std(ddof=1))
    se = sig / np.sqrt(n) if sig > 0 else 0.0
    return {"n": n, "mu": mu, "sigma": sig, "se": se,
            "t": (mu / se) if se > 0 else 0.0}
