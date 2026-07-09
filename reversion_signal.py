"""
reversion_signal.py — intraday VWAP reversion. Pure functions, no I/O.

THE STRATEGY, IN THE USER'S WORDS
--------------------------------
  "find stocks undervalued"            -> z = (close - VWAP)/sd(dev) <= -z_enter
  "quickly react to market trends"     -> require an up-tick before entering
  "sell when profit is at or close"    -> take profit when z recovers to z_take
  "or starts to aggressively trend down" -> stop out when z falls to z_stop

Plus a trailing stop, a hard max-hold, and a flat-by-close constraint.

VWAP is anchored at each session open and is causal by construction (it only
ever sees bars <= i). The deviation's standard deviation is an EXPANDING
within-session estimate with a warmup, so the first bars of the day never trade
on a two-observation variance.

Entry fills at open[i+1]. Exit at open[j+1] for signal exits; the trailing stop
fills at min(open[j], level) for longs -- a bar that gaps through the stop fills
at the open, not at the untouched level.

THE TRAP THIS FILE CANNOT ESCAPE
--------------------------------
Measured intraday "reversion" at 5-minute resolution is substantially bid-ask
bounce. Consecutive prints alternate between bid and offer, which manufactures
mean reversion that is not tradeable: you buy at the offer and sell at the bid,
and the measured edge is exactly the spread you just paid. This is the single
most common way intraday reversion research fools its author.

Two defences, both partial:
  1. Charge a wider spread than the trend system does. Reversion entries take
     liquidity precisely when it is scarce -- you are buying a falling stock.
  2. Note that these are IEX bars. IEX prints are a thin subset of consolidated
     prints, so their bar-to-bar noise is LARGER than the true tape's. That
     inflates apparent reversion. The honest fix is the SIP feed.

If a cell only clears the bar at low assumed cost, it has not cleared the bar.
"""

from __future__ import annotations
import numpy as np
import pandas as pd


def session_zscore(df: pd.DataFrame, warmup: int = 8) -> pd.DataFrame:
    """Per-session anchored VWAP and the z-score of price's deviation from it.

    RTH bars all fall inside one UTC date (13:30-19:55 UTC in EDT, 14:30-20:55
    in EST), so a UTC normalize recovers the session boundary without a tz hop.
    """
    out = df.copy()
    session = out.index.normalize()
    tp = (out["high"] + out["low"] + out["close"]) / 3.0
    pv = tp * out["volume"]

    g = pv.groupby(session).cumsum()
    v = out["volume"].groupby(session).cumsum()
    out["vwap"] = g / v.replace(0, np.nan)

    dev = out["close"] - out["vwap"]
    # expanding, within-session, causal. warmup bars produce NaN -> no trade.
    sd = dev.groupby(session).transform(
        lambda s: s.expanding(min_periods=warmup).std())
    out["z"] = dev / sd.replace(0, np.nan)
    out["bar_of_session"] = out.groupby(session).cumcount()
    out["session"] = session
    return out


def extract_trades(sf: pd.DataFrame,
                   z_enter: float,
                   z_take: float,
                   z_stop: float,
                   require_reversal: bool,
                   max_hold: int,
                   trail: float,
                   side: int,
                   cost_per_side: float,
                   no_new_bars: int = 6) -> list[dict]:
    """
    side : +1 long only (buy the dip), -1 short only (fade the spike)

    Long logic (short is the mirror):
      enter when  z <= -z_enter  and (close > prev close if require_reversal)
      exit on the FIRST of:
        take   : z >= z_take          (reverted to fair value)
        stretch: z <= z_stop          (dip deepened -- aggressive downtrend)
        trail  : retrace `trail` from the high-water mark since entry
        hold   : max_hold bars elapsed
        eod    : last tradeable bar of the session
    """
    o = sf["open"].values
    h = sf["high"].values
    lo = sf["low"].values
    c = sf["close"].values
    z = sf["z"].values
    bos = sf["bar_of_session"].values
    sess = sf["session"].values
    times = sf.index.values
    n = len(sf)

    # last usable bar of each session (need i+1 for the fill)
    last_of_session = np.zeros(n, dtype=bool)
    last_of_session[:-1] = sess[1:] != sess[:-1]
    last_of_session[-1] = True

    trades = []
    i = 0
    while i < n - 2:
        if not np.isfinite(z[i]) or bos[i] < no_new_bars or last_of_session[i] \
                or last_of_session[i + 1]:
            i += 1
            continue

        stretched = (z[i] <= -z_enter) if side == 1 else (z[i] >= z_enter)
        if not stretched:
            i += 1
            continue
        if require_reversal:
            turned = (c[i] > c[i - 1]) if side == 1 else (c[i] < c[i - 1])
            if not turned:
                i += 1
                continue

        entry_i = i + 1
        entry_px = o[entry_i] * (1 + side * cost_per_side)
        hwm = entry_px
        exit_i = exit_px = None
        kind = None

        for j in range(entry_i, min(entry_i + max_hold + 1, n - 1)):
            # trailing stop, tested against the water mark as of bar j-1
            if trail:
                if side == 1:
                    lvl = hwm * (1 - trail)
                    if lo[j] <= lvl:
                        exit_i, exit_px, kind = j, min(o[j], lvl) * (1 - cost_per_side), "trail"
                        break
                else:
                    lvl = hwm * (1 + trail)
                    if h[j] >= lvl:
                        exit_i, exit_px, kind = j, max(o[j], lvl) * (1 + cost_per_side), "trail"
                        break
                hwm = max(hwm, h[j]) if side == 1 else min(hwm, lo[j])

            if last_of_session[j]:
                exit_i, exit_px, kind = j, c[j] * (1 - side * cost_per_side), "eod"
                break

            zj = z[j]
            if np.isfinite(zj):
                took = (zj >= z_take) if side == 1 else (zj <= -z_take)
                blew = (zj <= z_stop) if side == 1 else (zj >= -z_stop)
                if took or blew:
                    exit_i = j + 1
                    exit_px = o[exit_i] * (1 - side * cost_per_side)
                    kind = "take" if took else "stretch"
                    break

            if j - entry_i >= max_hold:
                exit_i = j + 1
                exit_px = o[exit_i] * (1 - side * cost_per_side)
                kind = "max_hold"
                break

        if exit_i is None or exit_px is None or entry_px <= 0:
            i += 1
            continue

        net = side * (exit_px - entry_px) / entry_px
        gross = side * (o[exit_i] - o[entry_i]) / o[entry_i] if o[entry_i] > 0 else 0.0
        trades.append({
            "signal_time": times[i], "entry_time": times[entry_i],
            "exit_time": times[exit_i], "direction": side,
            "z_entry": float(z[i]), "bars_held": int(exit_i - entry_i),
            "exit_kind": kind, "gross_ret": float(gross), "ret": float(net),
        })
        i = exit_i + 1          # no overlapping positions in one symbol

    return trades
