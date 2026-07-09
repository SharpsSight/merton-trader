"""
backtest.py — event-driven backtest, no look-ahead, pessimistic fills.

Purpose is NOT a pretty equity curve. It is SAMPLE COMPRESSION: run the exact
signal logic over history to estimate, per signal-score bucket, the per-trade
return distribution (mu, sigma, n) that the Merton sizer needs. Without this the
sizer has no edge estimate and correctly sizes everything to zero.

No look-ahead:
  - signal at bar i uses only data through i
  - entry fills at bar i+1 open
  - higher-timeframe context uses the last CLOSED higher-TF bar (merge_asof back)
Pessimistic fills: SLIPPAGE_BPS + SPREAD_BPS applied adversely on entry AND exit.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
import config
import indicators as ind


# --------------------------------------------------------------------------
# Vectorised gated score per timeframe (same indicator math as live signals)
# --------------------------------------------------------------------------
def _score_series(df: pd.DataFrame, adx_threshold: float = config.ADX_THRESHOLD
                  ) -> pd.DataFrame:
    e9, e21 = ind.ema(df["close"], 9), ind.ema(df["close"], 21)
    m = ind.macd(df["close"])
    a = ind.adx(df)
    s = ind.supertrend(df)
    p = ind.parabolic_sar(df)

    votes = (
        np.sign(e9 - e21)
        + np.sign(m["hist"])
        + np.sign(a["plus_di"] - a["minus_di"])
        + s["direction"]
        + p["direction"]
    ) / 5.0
    strength = (a["adx"] / adx_threshold).clip(upper=1.0).fillna(0.0)
    score = (votes * strength).fillna(0.0)
    return pd.DataFrame({"score": score, "st": s["supertrend"]})


def _resample(df, rule):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df:
        agg["volume"] = "sum"
    return df.resample(rule, label="right", closed="right").agg(agg).dropna()


def build_signal_frame(df15: pd.DataFrame,
                       weights: dict = None) -> pd.DataFrame:
    """Per-base-bar combined confluence score with no look-ahead. Base timeframe
    is the FIRST key in the weights; the rest are resampled up from it."""
    weights = weights or config.TIMEFRAME_WEIGHTS
    tfs = list(weights.keys())
    base_tf = tfs[0]

    base = _score_series(df15)
    out = df15.join(base[["st"]])
    out["base_score"] = base["score"].values     # base-TF score (for sensitive exit)
    combined = weights[base_tf] * base["score"].fillna(0)
    wsum = weights[base_tf]

    for tf in tfs[1:]:
        tf_df = _resample(df15, tf)
        sc = _score_series(tf_df)["score"]
        # align last CLOSED higher-TF bar onto each base timestamp (no look-ahead)
        aligned = sc.reindex(out.index, method="ffill").fillna(0)
        combined = combined + weights[tf] * aligned
        wsum += weights[tf]

    out["score"] = combined / wsum if wsum else 0.0
    return out


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------
def _simulate(sf: pd.DataFrame, entry_threshold: float, flatten_eod: bool,
              trail: float, sensitive_exit: bool) -> list:
    """Run the trade loop on a prebuilt signal frame. Cheap (no indicators), so
    many exit variants can be compared without recomputing signals.

    trail          : trailing-stop distance as a fraction (e.g. 0.025 = 2.5%).
                     Models the LIVE trailing stop: tracks the best price since
                     entry and exits when price retraces `trail` from it.
    sensitive_exit : True = exit as soon as the BASE (5m) timeframe flips against
                     the position; False = exit on the blended-score flip/decay.
    """
    cost = (config.SLIPPAGE_BPS + config.SPREAD_BPS) / 1e4
    o = sf["open"].values
    h = sf["high"].values
    lo = sf["low"].values
    c = sf["close"].values
    score = sf["score"].values
    base = sf["base_score"].values
    dates = sf.index.normalize().values
    times = sf.index.values
    n = len(sf)

    trades = []
    pos = 0
    entry_px = entry_score = 0.0
    entry_i = 0
    hwm = 0.0            # best price since entry (high-water for long, low for short)

    for i in range(1, n - 1):
        if flatten_eod and pos != 0 and dates[i] != dates[i - 1]:
            exit_px = c[i - 1] * (1 - pos * cost)
            trades.append({"entry_score": entry_score, "direction": pos,
                           "ret": pos * (exit_px - entry_px) / entry_px,
                           "entry_time": times[entry_i], "exit_time": times[i - 1],
                           "bars_held": i - 1 - entry_i, "exit_kind": "eod"})
            pos = 0

        if pos == 0:
            if abs(score[i]) >= entry_threshold:
                pos = 1 if score[i] > 0 else -1
                entry_px = o[i + 1] * (1 + pos * cost)
                entry_score = score[i]
                entry_i = i
                hwm = entry_px
        else:
            exit_now, exit_px = False, None

            # --- trailing stop -------------------------------------------------
            # The stop level for bar i is derived from the high-water mark as of
            # the CLOSE OF BAR i-1. Using h[i] to raise the HWM before testing
            # lo[i] against it is intrabar look-ahead: you cannot know a bar's
            # high before its low. The old ordering ratcheted the stop up before
            # measuring the drawdown against it -- a free win on every bar.
            #
            # Fills use min(open, level) for longs / max(open, level) for shorts:
            # a bar that GAPS THROUGH the stop fills at the open, not at the
            # level. With FLATTEN_EOD=False every overnight gap hits this path,
            # and the old code silently assumed a fill at the untouched level.
            kind = None
            if pos == 1:
                lvl = hwm * (1 - trail)
                if lo[i] <= lvl:
                    fill = min(o[i], lvl)
                    exit_now, exit_px, kind = True, fill * (1 - cost), "stop"
            else:
                lvl = hwm * (1 + trail)
                if h[i] >= lvl:
                    fill = max(o[i], lvl)
                    exit_now, exit_px, kind = True, fill * (1 + cost), "stop"

            # update the water-mark only AFTER the stop test for this bar
            if not exit_now:
                hwm = max(hwm, h[i]) if pos == 1 else min(hwm, lo[i])

            # max holding time -> exit at next open, regardless of signal
            if not exit_now and config.MAX_HOLD_BARS and \
                    (i - entry_i) >= config.MAX_HOLD_BARS:
                exit_now, exit_px, kind = True, o[i + 1] * (1 - pos * cost), "max_hold"

            # signal exit
            if not exit_now:
                if sensitive_exit:
                    flip = base[i] != 0 and np.sign(base[i]) != pos
                else:
                    flip = np.sign(score[i]) != pos or abs(score[i]) < entry_threshold
                if flip:
                    exit_now, exit_px, kind = True, o[i + 1] * (1 - pos * cost), "signal"

            if exit_now:
                trades.append({"entry_score": entry_score, "direction": pos,
                               "ret": pos * (exit_px - entry_px) / entry_px,
                               "entry_time": times[entry_i], "exit_time": times[i],
                               "bars_held": i - entry_i, "exit_kind": kind})
                pos = 0

    return trades


def run_backtest(df15: pd.DataFrame,
                 entry_threshold: float = config.ENTRY_THRESHOLD,
                 flatten_eod: bool = False,
                 trail_pct: float = None,
                 sensitive_exit: bool = None) -> dict:
    sf = build_signal_frame(df15)
    trail = trail_pct if trail_pct is not None else config.TRAIL_PERCENT / 100.0
    sens = sensitive_exit if sensitive_exit is not None else config.SENSITIVE_EXIT
    trades = _simulate(sf, entry_threshold, flatten_eod, trail, sens)
    return {"trades": trades, "stats": _bucket_stats(trades),
            "metrics": _metrics(trades)}


def _bucket(score: float) -> str:
    """Bucket by |score| band (direction handled separately)."""
    a = abs(score)
    if a < 0.30:
        return "weak"
    if a < 0.50:
        return "b30_50"
    if a < 0.70:
        return "b50_70"
    return "b70_100"


def _bucket_stats(trades: list) -> dict:
    """Per-bucket mu/sigma/n — the payload the Merton sizer consumes."""
    buckets = {}
    for t in trades:
        buckets.setdefault(_bucket(t["entry_score"]), []).append(t["ret"])
    stats = {}
    for k, rets in buckets.items():
        arr = np.array(rets)
        stats[k] = {"mu": float(arr.mean()),
                    "sigma": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
                    "n": int(len(arr))}
    return stats


def _metrics(trades: list) -> dict:
    if not trades:
        return {"n_trades": 0, "total_ret": 0.0, "win_rate": 0.0, "avg_ret": 0.0}
    rets = np.array([t["ret"] for t in trades])
    return {"n_trades": len(trades),
            "total_ret": float(rets.sum()),
            "win_rate": float((rets > 0).mean()),
            "avg_ret": float(rets.mean())}
